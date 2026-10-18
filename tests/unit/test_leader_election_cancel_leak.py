from __future__ import annotations

import asyncio
import importlib
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.dml import Delete

leader_election_module = importlib.import_module("app.core.scheduling.leader_election")

pytestmark = pytest.mark.unit


class _FakeResult:
    """Mimics a RETURNING result: one row carrying the remaining lease, or none."""

    def __init__(self, rowcount: int, remaining: float) -> None:
        self.rowcount = rowcount
        self._remaining = remaining

    def first(self) -> tuple[float] | None:
        if self.rowcount >= 1:
            return (self._remaining,)
        return None


class _GatedSession:
    """Fake session whose execute/commit can be gated and externally released.

    The gates model a database call that is in flight (the statement already
    issued, the future not yet resolved) so a cancellation can be delivered
    exactly while the session block is suspended inside database work — the
    greenlet window of the 2026-10-14/18 write-lock leaks.
    """

    def __init__(self, *, ttl: float = 30.0, gate_execute: bool = False, gate_commit: bool = False) -> None:
        self._ttl = ttl
        self._gate_execute = gate_execute
        self._gate_commit = gate_commit
        self.execute_entered = asyncio.Event()
        self.commit_entered = asyncio.Event()
        self.release_execute = asyncio.Event()
        self.release_commit = asyncio.Event()
        self.exited = asyncio.Event()
        self.executes = 0
        self.commits = 0
        self.statements: list[Any] = []

    def get_bind(self) -> SimpleNamespace:
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    async def execute(self, statement: Any, params: Any = None) -> _FakeResult:
        self.executes += 1
        self.statements.append(statement)
        self.execute_entered.set()
        if self._gate_execute:
            await self.release_execute.wait()
        return _FakeResult(1, self._ttl)

    async def commit(self) -> None:
        self.commit_entered.set()
        if self._gate_commit:
            await self.release_commit.wait()
        self.commits += 1


class _HangingRenewSession:
    """Fake session whose acquires succeed and every renewal blocks until released."""

    def __init__(self, ttl: float, acquires: int = 1) -> None:
        self._ttl = ttl
        self._acquires = acquires
        self.calls = 0
        self.renew_calls = 0
        self.renew_dispatched = asyncio.Event()
        self.unblock = asyncio.Event()
        self.commits = 0

    def get_bind(self) -> SimpleNamespace:
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    async def execute(self, statement: Any, params: Any = None) -> _FakeResult:
        self.calls += 1
        if self.calls <= self._acquires:
            return _FakeResult(1, self._ttl)
        self.renew_calls += 1
        self.renew_dispatched.set()
        await self.unblock.wait()
        return _FakeResult(1, self._ttl)

    async def commit(self) -> None:
        self.commits += 1


def _install(
    monkeypatch: pytest.MonkeyPatch,
    session: Any,
    *,
    enabled: bool = True,
    ttl: int = 30,
) -> None:
    settings = SimpleNamespace(leader_election_enabled=enabled, leader_election_ttl_seconds=ttl)
    monkeypatch.setattr(leader_election_module, "get_settings", lambda: settings)

    @asynccontextmanager
    async def _session_cm():
        try:
            yield session
        finally:
            exited = getattr(session, "exited", None)
            if exited is not None:
                exited.set()

    monkeypatch.setattr(leader_election_module, "get_background_session", _session_cm)


class _InstrumentedSession:
    """Wraps a real AsyncSession with deterministic in-statement events."""

    def __init__(self, session: AsyncSession, issued: asyncio.Event, committed: asyncio.Event) -> None:
        self._session = session
        self._issued = issued
        self._committed = committed

    def get_bind(self) -> Any:
        return self._session.get_bind()

    async def execute(self, statement: Any, params: Any = None) -> Any:
        self._issued.set()
        return await self._session.execute(statement, params)

    async def commit(self) -> None:
        await self._session.commit()
        self._committed.set()


async def _make_real_sqlite_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ttl: int,
    busy_timeout_seconds: float,
    instrumented: bool = False,
) -> tuple[Any, Path, asyncio.Event | None, asyncio.Event | None]:
    """Create a file-backed aiosqlite engine wired in as the election's DB.

    The temp file keeps the test off the production database while exercising
    the real async driver stack (aiosqlite worker threads, WAL, single-writer
    lock, busy_timeout) that the cancel-leak incident involved.
    """
    from app.db.models import Base

    db_path = tmp_path / "leader-election-cancel-leak.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    engine = create_async_engine(url, poolclass=NullPool, connect_args={"timeout": busy_timeout_seconds})
    async with engine.begin() as conn:
        await conn.execute(sa.text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    issued = asyncio.Event() if instrumented else None
    committed = asyncio.Event() if instrumented else None

    settings = SimpleNamespace(leader_election_enabled=True, leader_election_ttl_seconds=ttl)
    monkeypatch.setattr(leader_election_module, "get_settings", lambda: settings)

    @asynccontextmanager
    async def _session_cm():
        async with factory() as session:
            if issued is None:
                yield session
            else:
                yield _InstrumentedSession(session, issued, committed)  # type: ignore[arg-type]

    monkeypatch.setattr(leader_election_module, "get_background_session", _session_cm)
    return engine, db_path, issued, committed


def _hold_write_lock(db_path: Path, busy_seconds: float) -> sqlite3.Connection:
    """Hold SQLite's single write lock from an independent connection."""
    holder = sqlite3.connect(str(db_path), timeout=busy_seconds, isolation_level=None)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE scheduler_leader SET leader_id = leader_id")
    return holder


def _db_remaining_seconds(db_path: Path) -> float:
    probe = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        row = probe.execute(
            "SELECT (julianday(expires_at) - julianday('now')) * 86400.0 FROM scheduler_leader"
        ).fetchone()
        assert row is not None
        return float(row[0])
    finally:
        probe.close()


async def _probe_write_succeeds(db_path: Path) -> float:
    """A fresh write transaction must succeed promptly (no leaked write lock)."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    probe = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    try:
        probe.execute("BEGIN IMMEDIATE")
        probe.execute("UPDATE scheduler_leader SET leader_id = leader_id")
        probe.execute("COMMIT")
    finally:
        probe.close()
    return loop.time() - start


@pytest.mark.asyncio
async def test_renew_commit_survives_outer_cancel_during_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    # A2: cancelling the renewal's WAITER while its statement is in flight must
    # not interrupt the session work. The commit still happens once the
    # statement resolves; pre-fix the cancellation unwound the session block
    # mid-execute and the commit never ran.
    session = _GatedSession(ttl=30.0, gate_execute=True)
    _install(monkeypatch, session, ttl=30)

    election = leader_election_module.LeaderElection(leader_id="node-a")
    election._is_leader = True
    loop = asyncio.get_running_loop()

    renewal = asyncio.ensure_future(election.renew())
    await session.execute_entered.wait()  # deterministic: statement in flight

    loop_time_before_cancel = loop.time()
    renewal.cancel()
    with pytest.raises(asyncio.CancelledError):
        await renewal
    # The waiter returned on its own schedule, before the DB work finished.
    assert loop.time() - loop_time_before_cancel < 1.0
    assert not session.release_execute.is_set()
    assert session.commits == 0

    session.release_execute.set()
    await asyncio.wait_for(session.exited.wait(), timeout=2)
    assert session.commits == 1
    # The cancelled waiter never adopted the outcome: no deadline was recorded.
    assert election._lease_deadline is None


@pytest.mark.asyncio
async def test_renew_commit_survives_outer_cancel_during_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    # A2: the same contract with the cancellation landing between execute and
    # the commit's completion — the exact window where a cancelled coroutine
    # strands an aiosqlite worker thread that already wrote the WAL.
    session = _GatedSession(ttl=30.0, gate_commit=True)
    _install(monkeypatch, session, ttl=30)

    election = leader_election_module.LeaderElection(leader_id="node-a")
    election._is_leader = True

    renewal = asyncio.ensure_future(election.renew())
    await session.commit_entered.wait()  # deterministic: commit in flight

    renewal.cancel()
    with pytest.raises(asyncio.CancelledError):
        await renewal
    assert session.commits == 0

    session.release_commit.set()
    await asyncio.wait_for(session.exited.wait(), timeout=2)
    assert session.commits == 1


@pytest.mark.asyncio
async def test_try_acquire_session_work_completes_after_outer_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A2 for the acquire upsert: a cancelled waiter must not abort the write;
    # a cancelled acquire also promotes nobody.
    session = _GatedSession(ttl=30.0, gate_execute=True)
    _install(monkeypatch, session, ttl=30)

    election = leader_election_module.LeaderElection(leader_id="node-a")

    acquire = asyncio.ensure_future(election.try_acquire())
    await session.execute_entered.wait()

    acquire.cancel()
    with pytest.raises(asyncio.CancelledError):
        await acquire
    assert session.commits == 0
    assert election.is_leader is False

    session.release_execute.set()
    await asyncio.wait_for(session.exited.wait(), timeout=2)
    assert session.commits == 1
    # The cancelled waiter never observed the outcome: no promotion from a
    # cancelled acquire.
    assert election.is_leader is False
    assert election._lease_deadline is None


@pytest.mark.asyncio
async def test_release_delete_completes_after_outer_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    # A2 for the release DELETE (the L458 session block): the shutdown task
    # sweep must not cancel the delete between execute and commit.
    session = _GatedSession(ttl=30.0, gate_execute=True)
    _install(monkeypatch, session, ttl=30)

    election = leader_election_module.LeaderElection(leader_id="node-a")
    election._is_leader = True

    release = asyncio.ensure_future(election.release())
    await session.execute_entered.wait()

    release.cancel()
    with pytest.raises(asyncio.CancelledError):
        await release
    assert session.commits == 0

    session.release_execute.set()
    await asyncio.wait_for(session.exited.wait(), timeout=2)
    assert session.commits == 1
    # The lease-row delete ran and committed despite the cancelled waiter.
    assert isinstance(session.statements[-1], Delete)


@pytest.mark.asyncio
async def test_release_keeper_stop_leaves_renewal_completing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A2 for _renew_lease_row: _stop_release_keeper cancels the keeper task
    # outright, so the drain/keeper renewal's session work must survive that
    # cancellation and still commit.
    session = _GatedSession(ttl=3.0, gate_execute=True)
    _install(monkeypatch, session, ttl=3)

    election = leader_election_module.LeaderElection(leader_id="node-a")
    loop = asyncio.get_running_loop()

    election.start_release_keeper()
    await session.execute_entered.wait()  # keeper's first renewal is in flight

    start = loop.time()
    await election._stop_release_keeper()
    # Shutdown was not pinned by the still-running database call.
    assert loop.time() - start < 1.0
    assert election._release_keeper is None
    assert session.commits == 0

    session.release_execute.set()
    await asyncio.wait_for(session.exited.wait(), timeout=2)
    assert session.commits == 1


@pytest.mark.asyncio
async def test_watchdog_detached_renewals_stay_uncancelled_and_demote(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A1: with the database wedged, the watchdog's two strikes dispatch two
    # real attempts (fail-fast contract), each timed-out renewal is DETACHED —
    # never cancelled — and the demotion still happens within the lease TTL.
    session = _HangingRenewSession(ttl=3.0)
    _install(monkeypatch, session, ttl=3)

    election = leader_election_module.LeaderElection(leader_id="node-a")
    body_cancelled = asyncio.Event()

    async def _body() -> str:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            body_cancelled.set()
            raise
        return "ran"

    loop = asyncio.get_running_loop()
    start = loop.time()
    with caplog.at_level(logging.WARNING, logger="app.core.scheduling.leader_election"):
        result = await election.run_if_leader(_body)

    assert result is None
    assert election.is_leader is False
    assert election._lease_deadline is None
    assert body_cancelled.is_set()
    # Fail-fast survived A1: demotion within the TTL plus scheduling slack.
    assert loop.time() - start < 6.0
    assert any("consecutive_errors=" in record.getMessage() for record in caplog.records)

    # Two strikes -> two real renewal attempts, both detached and un-cancelled.
    assert session.renew_calls == 2
    detached = [task for task in election._abandoned_renewals]
    assert len(detached) == 2
    for task in detached:
        assert not task.done()
        assert not task.cancelled()

    # Once the wedge clears the detached renewals finish untouched: they run
    # their commits (the acquire's commit makes three) and are consumed by
    # their done callbacks.
    session.unblock.set()
    await asyncio.wait(set(detached), timeout=2)
    for task in detached:
        assert not task.cancelled()
        assert task.exception() is None
    assert session.commits == 3
    assert election._abandoned_renewals == set()


@pytest.mark.asyncio
async def test_sibling_heartbeats_join_one_renewal_single_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Single-flight guard: two gated bodies heartbeat the SAME shared
    # LeaderElection concurrently. While the first heartbeat's renewal is
    # still in flight, the sibling's dispatch JOINS that task instead of
    # stacking a second concurrent writer; a dispatcher that already timed
    # out on the running task retries with a fresh renewal, and both joined
    # waiters abandoning the same task register it exactly once.
    session = _HangingRenewSession(ttl=3.0, acquires=2)
    _install(monkeypatch, session, ttl=3)

    election = leader_election_module.LeaderElection(leader_id="node-a")

    async def _body() -> str:
        await asyncio.sleep(60)
        return "ran"

    run_a: asyncio.Task[Any] = asyncio.ensure_future(election.run_if_leader(_body))
    run_b: asyncio.Task[Any] = asyncio.ensure_future(election.run_if_leader(_body))

    # Both heartbeats reach their renewal dispatch inside the first time-box
    # window (ttl=3 -> renew_interval 1s, renew_timeout 1s); the sibling joins
    # the first heartbeat's renewal, so only ONE renewal execute is in flight.
    await session.renew_dispatched.wait()
    await asyncio.sleep(0.4)
    assert session.renew_calls == 1
    shared = election._renewal_inflight
    assert shared is not None and not shared.done()

    # Let both waiters time out on the shared renewal (strike 1 each): the
    # double abandon of the SAME task must register it exactly once.
    await asyncio.sleep(1.2)
    assert election._abandoned_renewals == {shared}
    assert not shared.cancelled()
    # Strike 2: each dispatcher retries with a FRESH renewal (skip), and the
    # second dispatcher joins the first retry — still no stacking beyond one
    # extra retry writer.
    assert session.renew_calls == 2
    both_done, _ = await asyncio.wait({run_a, run_b}, timeout=6)
    assert both_done == {run_a, run_b}
    for task in (run_a, run_b):
        assert task.cancelled() is False
        assert task.result() is None  # both demoted (fail-fast preserved)
    assert election.is_leader is False

    # The wedge clears: every detached renewal finishes untouched.
    session.unblock.set()
    await asyncio.wait({shared, election._renewal_inflight}, timeout=3)
    for task in {shared, election._renewal_inflight}:
        if task is None:
            continue
        assert not task.cancelled()
        assert task.exception() is None
    assert election._abandoned_renewals == set()


@pytest.mark.asyncio
async def test_heartbeat_cancel_detaches_inflight_renewal(monkeypatch: pytest.MonkeyPatch) -> None:
    # A1 (heartbeat finally path): an external cancellation of run_if_leader
    # (scheduler stop) while a renewal is in flight must detach it — the
    # database work keeps running and commits, and nothing is cancelled.
    session = _HangingRenewSession(ttl=3.0)
    _install(monkeypatch, session, ttl=3)

    election = leader_election_module.LeaderElection(leader_id="node-a")

    async def _body() -> str:
        await asyncio.sleep(60)
        return "ran"

    run_task: asyncio.Task[Any] = asyncio.ensure_future(election.run_if_leader(_body))
    await session.renew_dispatched.wait()  # deterministic: a renewal is in flight

    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    detached = election._renewal_inflight
    assert detached is not None
    assert not detached.done()
    assert not detached.cancelled()
    assert election._abandoned_renewals == {detached}

    session.unblock.set()
    await asyncio.wait({detached}, timeout=2)
    assert not detached.cancelled()
    assert detached.exception() is None
    assert session.commits == 2  # the acquire's commit plus the renewal's
    assert election._abandoned_renewals == set()


@pytest.mark.asyncio
async def test_stuck_writer_demotes_and_leaks_no_lock_real_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # REQUIRED regression (plan test item 2): a write-lock holder that never
    # releases -> renewals time out -> the holder still demotes (counts
    # consecutive errors, drops leadership, cancels the gated body) within the
    # lease TTL. After the wedge clears, the detached renewal completes on its
    # own and a probe write succeeds promptly — no write lock is leaked by the
    # election's own renewals.
    ttl = 3
    engine, db_path, _, _ = await _make_real_sqlite_env(tmp_path, monkeypatch, ttl=ttl, busy_timeout_seconds=2.0)
    try:
        election = leader_election_module.LeaderElection(leader_id="node-a")
        loop = asyncio.get_running_loop()
        assert await election.try_acquire() is True

        holder = _hold_write_lock(db_path, busy_seconds=2.0)
        body_cancelled = asyncio.Event()

        async def _body() -> str:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                body_cancelled.set()
                raise
            return "ran"

        start = loop.time()
        with caplog.at_level(logging.WARNING, logger="app.core.scheduling.leader_election"):
            result = await election.run_if_leader(_body)
        elapsed = loop.time() - start

        assert result is None
        assert election.is_leader is False
        assert election._lease_deadline is None
        assert body_cancelled.is_set()
        # Demotion no later than ~the lease TTL (the 2s busy-timeout acquire
        # wait under the wedge is part of the window).
        assert elapsed < 8.0
        assert any("consecutive_errors=" in record.getMessage() for record in caplog.records)

        detached = election._renewal_inflight
        assert detached is not None
        assert not detached.cancelled()

        # Clear the wedge: the detached renewal finishes untouched (either it
        # finally runs — the lease has expired, so the guarded UPDATE matches
        # 0 rows — or it surfaces the transient locked error; either way it
        # must terminate on its own and release everything it held).
        holder.rollback()
        holder.close()
        await asyncio.wait({detached}, timeout=8)
        assert not detached.cancelled()
        assert election._abandoned_renewals == set()

        # No write lock leaked by the process: a fresh write wins the lock and
        # commits within its busy timeout.
        probe_elapsed = await _probe_write_succeeds(db_path)
        assert probe_elapsed < 5.0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cancel_during_real_write_lock_wait_still_commits_renewal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Deterministic reproduction of the incident window on the real async
    # driver stack (plan test item 1): an independent connection holds
    # SQLite's write lock, the renewal's upsert is issued and sits inside the
    # aiosqlite worker's busy wait, and the cancellation lands exactly there.
    # Post-fix the cancellation only detaches the waiter; the renewal then
    # acquires the lock, extends the lease, and commits — proven by the
    # DB-side remaining lease and a prompt probe write.
    ttl = 30
    engine, db_path, issued, committed = await _make_real_sqlite_env(
        tmp_path, monkeypatch, ttl=ttl, busy_timeout_seconds=15.0, instrumented=True
    )
    assert issued is not None and committed is not None
    try:
        election = leader_election_module.LeaderElection(leader_id="node-a")
        loop = asyncio.get_running_loop()
        assert await election.try_acquire() is True
        remaining_before = _db_remaining_seconds(db_path)

        holder = _hold_write_lock(db_path, busy_seconds=15.0)

        renewal = asyncio.ensure_future(election.renew())
        await issued.wait()  # deterministic: the upsert is inside the busy wait
        issued.clear()

        cancel_start = loop.time()
        renewal.cancel()
        with pytest.raises(asyncio.CancelledError):
            await renewal
        # The waiter returned promptly while the statement stayed in flight.
        assert loop.time() - cancel_start < 1.0

        # Clear the wedge; the detached session work must finish and commit.
        holder.rollback()
        holder.close()
        await asyncio.wait_for(committed.wait(), timeout=5)

        # The renewal genuinely extended the DB row (the commit landed).
        remaining_after = _db_remaining_seconds(db_path)
        assert remaining_after > max(remaining_before - 1.0, ttl - 5.0)

        # And no write lock leaked: a fresh write commits promptly.
        probe_elapsed = await _probe_write_succeeds(db_path)
        assert probe_elapsed < 5.0
    finally:
        await engine.dispose()
