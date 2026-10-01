from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.usage import capacity_for_plan
from app.core.utils.time import utcnow
from app.db.models import Account, RequestCreditAttribution, RequestLog, UsageWindowSnapshot
from app.db.session import get_background_session, sqlite_writer_section

logger = logging.getLogger(__name__)

# Restarts forget the in-memory watermark, so the first pass after startup
# reprocesses this much snapshot history. Idempotency (unique request+window
# with INSERT OR IGNORE) makes the reprocessing safe; the bound keeps the
# restart pass O(recent traffic) instead of O(all snapshots).
REPROCESS_WINDOW = timedelta(hours=2)
# Request completion time is approximated by requested_at (request START):
# per-response latency data is uneven, so a small backdate widens each
# interval to catch requests that completed just before the newer snapshot.
INTERVAL_BACKDATE = timedelta(seconds=30)
_WINDOW_KEYS = ("primary", "secondary")
# Upper bound on snapshot rows loaded per pass; a pass that hits the bound
# leaves the remainder to the next tick (watermark only advances through
# processed rows, so nothing is skipped).
_MAX_SNAPSHOTS_PER_PASS = 5000


def window_delta_percent(
    prev_used_percent: float | None,
    cur_used_percent: float | None,
    prev_reset_at: datetime | None,
    cur_reset_at: datetime | None,
) -> float | None:
    """Usable percent increase between consecutive snapshots of one window.

    Returns ``None`` (skip: no attribution) unless the pair is comparable:
    both resets known and UNCHANGED (a reset means the window rolled over, so
    the raw difference is meaningless), and usage strictly increased (a drop
    is an upstream correction, not consumption).
    """
    if prev_used_percent is None or cur_used_percent is None:
        return None
    if prev_reset_at is None or cur_reset_at is None or prev_reset_at != cur_reset_at:
        return None
    delta = float(cur_used_percent) - float(prev_used_percent)
    if delta <= 0:
        return None
    return delta


def delta_percent_to_credits(delta_percent: float, capacity_credits: float | None) -> float | None:
    """Convert a window percent delta into credits via the window capacity.

    Capacity is resolved at attribution time from the account's current plan
    (history keeps only percents), so a plan change re-prices unprocessed
    history but never rewrites attributed rows.
    """
    if capacity_credits is None or capacity_credits <= 0:
        return None
    return (delta_percent / 100.0) * capacity_credits


def request_token_weight(
    input_tokens: int | None,
    output_tokens: int | None,
    reasoning_tokens: int | None,
    cached_input_tokens: int | None,
) -> int:
    """Token footprint used to split a shared delta across concurrent requests."""
    output = output_tokens if output_tokens is not None else reasoning_tokens
    return (input_tokens or 0) + (output or 0) + (cached_input_tokens or 0)


def split_credits(delta_credits: float, weights: list[int]) -> list[float]:
    """Split one window's delta across the requests observed in its interval.

    Proportional to each request's token weight; when every weight is zero
    (token counts unknown) but requests exist, the delta splits equally. The
    caller guarantees ``weights`` is non-empty.
    """
    total = sum(weights)
    if total <= 0:
        count = len(weights)
        share = delta_credits / count
        return [share] * count
    return [delta_credits * weight / total for weight in weights]


@dataclass(frozen=True, slots=True)
class _AttributionRow:
    request_log_id: int
    account_id: str
    window: str
    credits: float
    snapshot_id: int


@dataclass(frozen=True, slots=True)
class _IntervalDelta:
    interval_start: datetime  # exclusive
    interval_end: datetime  # inclusive
    window: str
    delta_credits: float
    snapshot_id: int


class CreditAttributionEngine:
    """Differences consecutive per-account snapshots into per-request credits.

    The engine is stateful only through ``_watermark`` (the highest snapshot
    id already processed in this process); persistence-side idempotency comes
    from the unique ``(request_log_id, window)`` constraint, so a restart that
    reprocesses the last :data:`REPROCESS_WINDOW` never double-counts.
    """

    def __init__(self) -> None:
        self._watermark: int | None = None

    async def run_pass(self, *, now: datetime | None = None) -> dict[str, int]:
        now = now or utcnow()
        scanned = 0
        unattributed_windows = 0
        rows: list[_AttributionRow] = []
        max_snapshot_id: int | None = None
        async with get_background_session() as session:
            async with sqlite_writer_section():
                grouped = await self._load_snapshot_groups(session, now=now)
                for snapshots in grouped.values():
                    for snapshot in snapshots:
                        if max_snapshot_id is None or snapshot.id > max_snapshot_id:
                            max_snapshot_id = snapshot.id
                plan_types = await _load_plan_types(session, grouped.keys())
                request_rows: dict[str, list[tuple[datetime, int, int]]] = {}
                for account_id, snapshots in grouped.items():
                    scanned += len(snapshots)
                    intervals: list[tuple[datetime, datetime]] = []
                    deltas: list[_IntervalDelta] = []
                    capacity: dict[str, float | None] = {}
                    for prev, cur in zip(snapshots, snapshots[1:], strict=False):
                        if self._watermark is not None and cur.id <= self._watermark:
                            continue
                        interval_start = prev.captured_at - INTERVAL_BACKDATE
                        interval_end = cur.captured_at
                        pair_has_delta = False
                        for window in _WINDOW_KEYS:
                            delta_percent = window_delta_percent(
                                _used_percent(prev, window),
                                _used_percent(cur, window),
                                _reset_at(prev, window),
                                _reset_at(cur, window),
                            )
                            if delta_percent is None:
                                continue
                            if window not in capacity:
                                raw_capacity = capacity_for_plan(plan_types.get(account_id), window)
                                capacity[window] = (
                                    raw_capacity if raw_capacity is not None and raw_capacity > 0 else None
                                )
                            window_capacity = capacity[window]
                            if window_capacity is None:
                                continue
                            delta_credits = delta_percent_to_credits(delta_percent, window_capacity)
                            if delta_credits is None or delta_credits <= 0:
                                continue
                            deltas.append(
                                _IntervalDelta(
                                    interval_start=interval_start,
                                    interval_end=interval_end,
                                    window=window,
                                    delta_credits=delta_credits,
                                    snapshot_id=cur.id,
                                )
                            )
                            pair_has_delta = True
                        if pair_has_delta:
                            intervals.append((interval_start, interval_end))
                    if not deltas:
                        continue
                    if account_id not in request_rows:
                        request_rows[account_id] = await _load_account_requests(
                            session, account_id=account_id, intervals=intervals
                        )
                    account_requests = request_rows[account_id]
                    for delta in deltas:
                        matched = [
                            (requested_at, request_id, weight)
                            for requested_at, request_id, weight in account_requests
                            if delta.interval_start < requested_at <= delta.interval_end
                        ]
                        if not matched:
                            # Unattributable delta: no observed requests in
                            # the interval (warmup-only traffic, another
                            # session's usage, or a log-retention tail).
                            unattributed_windows += 1
                            continue
                        weights = [weight for _, _, weight in matched]
                        shares = split_credits(delta.delta_credits, weights)
                        for (requested_at, request_id, _), credits in zip(matched, shares, strict=True):
                            rows.append(
                                _AttributionRow(
                                    request_log_id=request_id,
                                    account_id=account_id,
                                    window=delta.window,
                                    credits=credits,
                                    snapshot_id=delta.snapshot_id,
                                )
                            )
                inserted = await _insert_attributions(session, rows)
                await session.commit()
                # The watermark advances only after the pass committed: a
                # failed insert keeps the pairs eligible so the next tick
                # retries them (idempotency makes the retry safe).
                if max_snapshot_id is not None and (self._watermark is None or max_snapshot_id > self._watermark):
                    self._watermark = max_snapshot_id
        if unattributed_windows:
            logger.debug(
                "Credit attribution skipped %d window deltas without requests in their intervals",
                unattributed_windows,
            )
        return {
            "snapshots_scanned": scanned,
            "attributions": inserted,
            "unattributed_windows": unattributed_windows,
        }

    async def _load_snapshot_groups(
        self, session: AsyncSession, *, now: datetime
    ) -> dict[str, list[UsageWindowSnapshot]]:
        stmt = (
            select(UsageWindowSnapshot)
            .order_by(UsageWindowSnapshot.account_id, UsageWindowSnapshot.captured_at, UsageWindowSnapshot.id)
            .limit(_MAX_SNAPSHOTS_PER_PASS)
        )
        if self._watermark is None:
            stmt = stmt.where(UsageWindowSnapshot.captured_at >= now - REPROCESS_WINDOW)
        else:
            stmt = stmt.where(UsageWindowSnapshot.id > self._watermark)
        rows = list((await session.execute(stmt)).scalars().all())
        grouped: dict[str, list[UsageWindowSnapshot]] = {}
        for row in rows:
            grouped.setdefault(row.account_id, []).append(row)
        # Each account's chain needs one predecessor older than the candidate
        # window to form its first pair (otherwise every restart would lose
        # the delta spanning the watermark boundary).
        predecessors = await _load_predecessors(session, grouped)
        for account_id, predecessor in predecessors.items():
            if predecessor is not None:
                grouped[account_id].insert(0, predecessor)
        return grouped


def _used_percent(snapshot: UsageWindowSnapshot, window: str) -> float | None:
    return snapshot.primary_used_percent if window == "primary" else snapshot.secondary_used_percent


def _reset_at(snapshot: UsageWindowSnapshot, window: str) -> datetime | None:
    return snapshot.primary_reset_at if window == "primary" else snapshot.secondary_reset_at


async def _load_plan_types(session: AsyncSession, account_ids) -> dict[str, str | None]:
    account_ids = list(account_ids)
    if not account_ids:
        return {}
    rows = (await session.execute(select(Account.id, Account.plan_type).where(Account.id.in_(account_ids)))).all()
    return {row.id: row.plan_type for row in rows}


async def _load_predecessors(
    session: AsyncSession, grouped: dict[str, list[UsageWindowSnapshot]]
) -> dict[str, UsageWindowSnapshot | None]:
    predecessors: dict[str, UsageWindowSnapshot | None] = {}
    for account_id, snapshots in grouped.items():
        first = snapshots[0]
        predecessors[account_id] = (
            (
                await session.execute(
                    select(UsageWindowSnapshot)
                    .where(
                        UsageWindowSnapshot.account_id == account_id,
                        or_(
                            UsageWindowSnapshot.captured_at < first.captured_at,
                            and_(
                                UsageWindowSnapshot.captured_at == first.captured_at,
                                UsageWindowSnapshot.id < first.id,
                            ),
                        ),
                    )
                    .order_by(UsageWindowSnapshot.captured_at.desc(), UsageWindowSnapshot.id.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
    return predecessors


async def _load_account_requests(
    session: AsyncSession,
    *,
    account_id: str,
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, int, int]]:
    """Requests plausibly inside any of the account's delta intervals.

    Returns ``(requested_at, request_log_id, token_weight)`` rows, unsorted;
    every request of the account is attributed (warmups included): the delta
    is real consumption, so it must land somewhere rather than be dropped.
    """
    clause = or_(*(and_(RequestLog.requested_at > start, RequestLog.requested_at <= end) for start, end in intervals))
    rows = (
        await session.execute(
            select(
                RequestLog.id,
                RequestLog.requested_at,
                RequestLog.input_tokens,
                RequestLog.output_tokens,
                RequestLog.reasoning_tokens,
                RequestLog.cached_input_tokens,
            ).where(RequestLog.account_id == account_id, clause)
        )
    ).all()
    return [
        (
            row.requested_at,
            row.id,
            request_token_weight(row.input_tokens, row.output_tokens, row.reasoning_tokens, row.cached_input_tokens),
        )
        for row in rows
    ]


def _insert_fn(session: AsyncSession):
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        return pg_insert
    if dialect == "sqlite":
        return sqlite_insert
    raise RuntimeError(f"Credit attribution insert unsupported for dialect={dialect!r}")


async def _insert_attributions(session: AsyncSession, rows: list[_AttributionRow]) -> int:
    if not rows:
        return 0
    stmt = _insert_fn(session)(RequestCreditAttribution).values(
        [
            {
                "request_log_id": row.request_log_id,
                "account_id": row.account_id,
                "window": row.window,
                "credits": row.credits,
                "snapshot_id": row.snapshot_id,
                "attributed_at": utcnow(),
            }
            for row in rows
        ]
    )
    # Unique (request_log_id, window) makes reprocessing idempotent: interval
    # backdating can nominate the same request from two consecutive pairs, and
    # the restart reprocess window overlaps previously attributed pairs — the
    # first attribution wins, the conflicting insert is a no-op.
    result = await session.execute(stmt.on_conflict_do_nothing(index_elements=["request_log_id", "window"]))
    return len(rows) if result.rowcount in (None, -1) else result.rowcount
