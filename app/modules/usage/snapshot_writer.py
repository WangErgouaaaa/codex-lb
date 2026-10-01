from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.core import usage as usage_core
from app.core.usage.live_snapshots import LiveRateLimitSnapshot, LiveUsageWindow
from app.core.utils.time import naive_utc_to_epoch, utcnow
from app.db.models import Account, UsageWindowSnapshot
from app.db.session import get_background_session, sqlite_writer_section

logger = logging.getLogger(__name__)

# Snapshot writes ride the serving path behind the live-usage publisher, so
# enqueueing must stay O(1) and never raise; a single consumer task owns its
# own background session and inserts coalesced batches.
_QUEUE_SIZE = 512
_BATCH_SIZE = 128

_RESOLUTION_TTL_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class _QueuedSnapshot:
    account_id: str | None
    chatgpt_account_id: str | None
    snapshot: LiveRateLimitSnapshot
    captured_at: datetime


def _fingerprint_from_snapshot(snapshot: LiveRateLimitSnapshot) -> tuple[object, ...]:
    def window_key(window: LiveUsageWindow | None) -> tuple[object, ...] | None:
        if window is None:
            return None
        return (round(window.used_percent, 2), window.window_minutes, window.reset_at)

    return (window_key(snapshot.primary), window_key(snapshot.secondary))


def _fingerprint_from_row(row: UsageWindowSnapshot) -> tuple[object, ...]:
    def window_key(
        used_percent: float | None,
        window_minutes: int | None,
        reset_at: datetime | None,
    ) -> tuple[object, ...] | None:
        if used_percent is None:
            return None
        reset_epoch = naive_utc_to_epoch(reset_at) if reset_at is not None else None
        return (round(used_percent, 2), window_minutes, reset_epoch)

    return (
        window_key(row.primary_used_percent, row.primary_window_minutes, row.primary_reset_at),
        window_key(row.secondary_used_percent, row.secondary_window_minutes, row.secondary_reset_at),
    )


def _epoch_to_naive_utc(reset_at: int | None) -> datetime | None:
    if reset_at is None:
        return None
    return datetime.fromtimestamp(reset_at, tz=timezone.utc).replace(tzinfo=None)


class UsageWindowSnapshotWriter:
    """Durable per-response usage-window snapshot sink.

    Chained behind the live-usage publisher: every parsed upstream rate-limit
    snapshot is persisted to ``usage_window_snapshots`` so the credit
    attribution pass can difference consecutive snapshots per account. The
    writer is additive — it never replaces or blocks the existing usage
    history ingestor.
    """

    def __init__(self, *, queue_size: int = _QUEUE_SIZE) -> None:
        self._queue: asyncio.Queue[_QueuedSnapshot] = asyncio.Queue(maxsize=max(1, queue_size))
        # Last seen fingerprint per account identity (account_id when known,
        # else the raw chatgpt_account_id) — unchanged window percents AND
        # reset timestamps are not persisted again.
        self._last_fingerprint: dict[str, tuple[object, ...]] = {}
        self._resolution_cache: dict[str, tuple[str | None, float]] = {}
        self._consumer: asyncio.Task[None] | None = None
        self._dropped = 0

    def publish(
        self,
        snapshot: LiveRateLimitSnapshot,
        *,
        account_id: str | None = None,
        chatgpt_account_id: str | None = None,
    ) -> None:
        if not snapshot.has_windows:
            return
        if not account_id and not chatgpt_account_id:
            return
        dedupe_key = account_id or chatgpt_account_id
        assert dedupe_key is not None
        fingerprint = _fingerprint_from_snapshot(snapshot)
        if self._last_fingerprint.get(dedupe_key) == fingerprint:
            return
        self._last_fingerprint[dedupe_key] = fingerprint
        item = _QueuedSnapshot(
            account_id=account_id,
            chatgpt_account_id=chatgpt_account_id,
            snapshot=snapshot,
            captured_at=utcnow(),
        )
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._dropped += 1
            if self._dropped % 100 == 1:
                logger.warning("Usage snapshot queue full; dropped_total=%d", self._dropped)

    def start(self) -> None:
        if self._consumer is None or self._consumer.done():
            self._consumer = asyncio.create_task(self._run(), name="usage-window-snapshot-writer")

    async def stop(self) -> None:
        consumer = self._consumer
        self._consumer = None
        if consumer is not None:
            consumer.cancel()
            try:
                await consumer
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        await self._hydrate_last_state()
        while True:
            item = await self._queue.get()
            batch = [item]
            while len(batch) < _BATCH_SIZE:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await self._persist_batch(batch)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Usage snapshot batch persist failed batch_size=%d", len(batch), exc_info=True)

    async def _persist_batch(self, batch: list[_QueuedSnapshot]) -> None:
        rows: list[UsageWindowSnapshot] = []
        for item in batch:
            account_id = item.account_id or await self._resolve_account_id(item.chatgpt_account_id)
            if account_id is None:
                # Unresolvable identities (ambiguous workspace slots) are
                # dropped rather than guessed, matching the live ingestor.
                continue
            normalized = _normalize_windows(item.snapshot)
            if normalized is None:
                # Monthly-only free-plan shape: the snapshot schema has no
                # monthly slot, so there is nothing to difference later.
                continue
            primary, secondary = normalized
            rows.append(
                UsageWindowSnapshot(
                    account_id=account_id,
                    chatgpt_account_id=item.chatgpt_account_id,
                    captured_at=item.captured_at,
                    request_log_id=None,
                    source="response",
                    primary_used_percent=primary.used_percent if primary is not None else None,
                    primary_window_minutes=primary.window_minutes if primary is not None else None,
                    primary_reset_at=_epoch_to_naive_utc(primary.reset_at) if primary is not None else None,
                    secondary_used_percent=secondary.used_percent if secondary is not None else None,
                    secondary_window_minutes=secondary.window_minutes if secondary is not None else None,
                    secondary_reset_at=_epoch_to_naive_utc(secondary.reset_at) if secondary is not None else None,
                )
            )
        if not rows:
            return
        async with get_background_session() as session:
            session.add_all(rows)
            async with sqlite_writer_section():
                await session.commit()

    async def _hydrate_last_state(self) -> None:
        """Seed dedupe state from the latest persisted snapshot per account.

        Without this, the first snapshot after a restart would duplicate the
        last pre-restart row (harmless — it differences to a zero delta — but
        noisy).
        """
        fingerprints: dict[str, tuple[object, ...]] = {}
        try:
            async with get_background_session() as session:
                latest_ids = (
                    select(func.max(UsageWindowSnapshot.id)).group_by(UsageWindowSnapshot.account_id).scalar_subquery()
                )
                rows = (
                    (await session.execute(select(UsageWindowSnapshot).where(UsageWindowSnapshot.id.in_(latest_ids))))
                    .scalars()
                    .all()
                )
                # Materialize inside the session: the context's rollback
                # expires loaded rows, so detached attribute access after the
                # block would fail.
                fingerprints = {row.account_id: _fingerprint_from_row(row) for row in rows}
        except Exception:
            logger.warning("Usage snapshot dedupe hydration failed; starting cold", exc_info=True)
            return
        self._last_fingerprint.update(fingerprints)

    async def _resolve_account_id(self, chatgpt_account_id: str | None) -> str | None:
        if not chatgpt_account_id:
            return None
        cached = self._resolution_cache.get(chatgpt_account_id)
        now = time.monotonic()
        if cached is not None and now - cached[1] < _RESOLUTION_TTL_SECONDS:
            return cached[0]
        async with get_background_session() as session:
            rows = (
                (await session.execute(select(Account.id).where(Account.chatgpt_account_id == chatgpt_account_id)))
                .scalars()
                .all()
            )
        # Ambiguous identities (multiple workspace slots) are dropped rather
        # than guessed; the poller stays authoritative for them.
        resolved = rows[0] if len(rows) == 1 else None
        self._resolution_cache[chatgpt_account_id] = (resolved, now)
        return resolved


def _normalize_windows(
    snapshot: LiveRateLimitSnapshot,
) -> tuple[LiveUsageWindow | None, LiveUsageWindow | None] | None:
    """Map the live snapshot onto the snapshot table's primary/secondary slots.

    Mirrors the live ingestor's write-time normalization: a weekly window
    reported in the primary slot over an empty secondary placeholder belongs
    in the secondary slot; a lone monthly-duration primary is the free-plan
    monthly-only shape, which the snapshot schema has no slot for.
    """
    primary = snapshot.primary
    secondary = snapshot.secondary
    if (
        primary is not None
        and secondary is not None
        and usage_core.is_weekly_window_minutes(primary.window_minutes)
        and usage_core.is_empty_quota_placeholder(
            window_duration=secondary.window_minutes,
            reset_at=secondary.reset_at,
        )
    ):
        return None, primary
    if primary is not None and secondary is None and usage_core.is_monthly_window_minutes(primary.window_minutes):
        return None
    return primary, secondary
