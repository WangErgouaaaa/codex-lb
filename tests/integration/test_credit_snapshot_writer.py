from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from app.core.crypto import TokenEncryptor
from app.core.usage.live_hub import publish_live_usage
from app.core.usage.live_snapshots import LiveRateLimitSnapshot, LiveUsageWindow
from app.core.utils.time import utcnow
from app.db.models import Account, AccountStatus, UsageWindowSnapshot
from app.db.session import SessionLocal
from app.modules.accounts.repository import AccountsRepository
from app.modules.usage import live_ingest
from app.modules.usage.snapshot_writer import UsageWindowSnapshotWriter

pytestmark = pytest.mark.integration


def _make_account(account_id: str, email: str, *, chatgpt_account_id: str | None = None) -> Account:
    encryptor = TokenEncryptor()
    return Account(
        id=account_id,
        chatgpt_account_id=chatgpt_account_id,
        email=email,
        plan_type="plus",
        access_token_encrypted=encryptor.encrypt("access"),
        refresh_token_encrypted=encryptor.encrypt("refresh"),
        id_token_encrypted=encryptor.encrypt("id"),
        last_refresh=utcnow(),
        status=AccountStatus.ACTIVE,
    )


def _snapshot(
    *,
    primary_used: float = 33.0,
    secondary_used: float = 44.0,
    primary_reset_offset: int = 300,
) -> LiveRateLimitSnapshot:
    now_epoch = int(utcnow().timestamp())
    return LiveRateLimitSnapshot(
        primary=LiveUsageWindow(
            used_percent=primary_used,
            window_minutes=300,
            reset_at=now_epoch + primary_reset_offset,
        ),
        secondary=LiveUsageWindow(
            used_percent=secondary_used,
            window_minutes=10080,
            reset_at=now_epoch + 5 * 24 * 3600,
        ),
        credits_has=True,
        credits_unlimited=False,
        credits_balance=7.5,
    )


async def _snapshot_count(account_id: str) -> int:
    async with SessionLocal() as session:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(UsageWindowSnapshot)
                    .where(UsageWindowSnapshot.account_id == account_id)
                )
            ).scalar_one()
        )


async def _wait_for_count(account_id: str, expected: int, *, timeout: float = 5.0) -> int:
    deadline = asyncio.get_event_loop().time() + timeout
    count = await _snapshot_count(account_id)
    while count < expected and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)
        count = await _snapshot_count(account_id)
    return count


async def test_writer_persists_snapshot_rows(db_setup) -> None:
    del db_setup
    writer = UsageWindowSnapshotWriter(queue_size=8)
    writer.start()
    try:
        writer.publish(_snapshot(), account_id="acc_snap_basic")
        assert await _wait_for_count("acc_snap_basic", 1) == 1
    finally:
        await writer.stop()

    async with SessionLocal() as session:
        row = (
            (
                await session.execute(
                    select(UsageWindowSnapshot).where(UsageWindowSnapshot.account_id == "acc_snap_basic")
                )
            )
            .scalars()
            .one()
        )
    assert row.primary_used_percent == pytest.approx(33.0)
    assert row.primary_window_minutes == 300
    assert row.primary_reset_at is not None
    assert row.secondary_used_percent == pytest.approx(44.0)
    assert row.secondary_window_minutes == 10080
    assert row.secondary_reset_at is not None
    assert row.source == "response"
    assert row.request_log_id is None


async def test_writer_dedupes_unchanged_window_state(db_setup) -> None:
    del db_setup
    writer = UsageWindowSnapshotWriter(queue_size=8)
    writer.start()
    try:
        writer.publish(_snapshot(), account_id="acc_snap_dedupe")
        writer.publish(_snapshot(), account_id="acc_snap_dedupe")  # identical: dropped
        writer.publish(_snapshot(primary_used=40.0), account_id="acc_snap_dedupe")  # changed: kept
        assert await _wait_for_count("acc_snap_dedupe", 2) == 2
    finally:
        await writer.stop()


async def test_writer_skips_snapshots_without_identity_or_windows(db_setup) -> None:
    del db_setup
    writer = UsageWindowSnapshotWriter(queue_size=8)
    writer.start()
    try:
        writer.publish(_snapshot())  # neither account_id nor chatgpt_account_id
        no_windows = LiveRateLimitSnapshot(
            primary=None,
            secondary=None,
            credits_has=None,
            credits_unlimited=None,
            credits_balance=None,
        )
        writer.publish(no_windows, account_id="acc_snap_nowin")
        await asyncio.sleep(0.2)
        assert await _snapshot_count("acc_snap_nowin") == 0
    finally:
        await writer.stop()


async def test_writer_normalizes_weekly_primary_slot(db_setup) -> None:
    del db_setup
    now_epoch = int(utcnow().timestamp())
    snapshot = LiveRateLimitSnapshot(
        primary=LiveUsageWindow(used_percent=64.0, window_minutes=10080, reset_at=now_epoch + 5 * 24 * 3600),
        secondary=LiveUsageWindow(used_percent=17.0, window_minutes=0, reset_at=None),
        credits_has=True,
        credits_unlimited=False,
        credits_balance=None,
    )
    writer = UsageWindowSnapshotWriter(queue_size=8)
    writer.start()
    try:
        writer.publish(snapshot, account_id="acc_snap_weekly")
        assert await _wait_for_count("acc_snap_weekly", 1) == 1
    finally:
        await writer.stop()

    async with SessionLocal() as session:
        row = (
            (
                await session.execute(
                    select(UsageWindowSnapshot).where(UsageWindowSnapshot.account_id == "acc_snap_weekly")
                )
            )
            .scalars()
            .one()
        )
    # The weekly window reported in the primary slot belongs in the secondary
    # slot, mirroring the usage-history ingestor's normalization.
    assert row.primary_used_percent is None
    assert row.secondary_used_percent == pytest.approx(64.0)
    assert row.secondary_window_minutes == 10080


async def test_writer_resolves_chatgpt_account_id(db_setup) -> None:
    del db_setup
    async with SessionLocal() as session:
        await AccountsRepository(session).upsert(
            _make_account("acc_snap_resolved", "snap-resolved@example.com", chatgpt_account_id="workspace-snap-1")
        )

    writer = UsageWindowSnapshotWriter(queue_size=8)
    writer.start()
    try:
        writer.publish(_snapshot(), chatgpt_account_id="workspace-snap-1")
        assert await _wait_for_count("acc_snap_resolved", 1) == 1
    finally:
        await writer.stop()

    async with SessionLocal() as session:
        row = (
            (
                await session.execute(
                    select(UsageWindowSnapshot).where(UsageWindowSnapshot.account_id == "acc_snap_resolved")
                )
            )
            .scalars()
            .one()
        )
    assert row.chatgpt_account_id == "workspace-snap-1"


async def test_writer_hydrates_last_state_from_db_across_restart(db_setup) -> None:
    del db_setup
    from datetime import datetime, timezone

    # Pre-seed what a previous process instance persisted, using explicit
    # epochs so the restarted writer's first publish fingerprint matches.
    now_epoch = int(utcnow().timestamp())
    primary_reset = now_epoch + 300
    secondary_reset = now_epoch + 5 * 24 * 3600

    def _naive(epoch: int) -> datetime:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)

    async with SessionLocal() as session:
        session.add(
            UsageWindowSnapshot(
                account_id="acc_snap_hydrate",
                captured_at=utcnow(),
                source="response",
                primary_used_percent=33.0,
                primary_window_minutes=300,
                primary_reset_at=_naive(primary_reset),
                secondary_used_percent=44.0,
                secondary_window_minutes=10080,
                secondary_reset_at=_naive(secondary_reset),
            )
        )
        await session.commit()

    writer = UsageWindowSnapshotWriter(queue_size=8)
    writer.start()
    try:
        # Wait for the consumer's hydration pass to seed dedupe state.
        deadline = asyncio.get_event_loop().time() + 5.0
        while "acc_snap_hydrate" not in writer._last_fingerprint and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)
        writer.publish(
            LiveRateLimitSnapshot(
                primary=LiveUsageWindow(used_percent=33.0, window_minutes=300, reset_at=primary_reset),
                secondary=LiveUsageWindow(used_percent=44.0, window_minutes=10080, reset_at=secondary_reset),
                credits_has=None,
                credits_unlimited=None,
                credits_balance=None,
            ),
            account_id="acc_snap_hydrate",
        )
        await asyncio.sleep(0.3)
        assert await _snapshot_count("acc_snap_hydrate") == 1
    finally:
        await writer.stop()


async def test_start_live_usage_ingestor_persists_snapshots_through_hub(db_setup) -> None:
    """The production hook: the chained hub publisher must feed BOTH consumers."""
    del db_setup
    async with SessionLocal() as session:
        await AccountsRepository(session).upsert(_make_account("acc_snap_hub", "snap-hub@example.com"))

    assert live_ingest.start_live_usage_ingestor() is not None
    try:
        publish_live_usage(_snapshot(), account_id="acc_snap_hub")
        assert await _wait_for_count("acc_snap_hub", 1) == 1
    finally:
        await live_ingest.stop_live_usage_ingestor()

    # Stopping must unregister the hub publisher AND stop the writer.
    assert live_ingest._ingestor is None
    assert live_ingest._snapshot_writer is None
