"""Observability of DB session cleanup failures.

Batch B of the SQLite write-lock leak fix (2026-10-14 incident): cleanup
failures in ``app.db.session`` must leave an ERROR trace. The swallow
semantics of ``_safe_rollback`` / ``_safe_close`` are unchanged — failures
are logged, never re-raised — and a cancelled caller whose cleanup hangs
must not hang the cleanup coroutine forever.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, cast

import pytest

import app.db.session as session_module


def _session_error_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "app.db.session" and r.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_safe_rollback_swallows_and_logs_error_when_rollback_fails(caplog) -> None:
    caplog.set_level(logging.ERROR)

    class FakeSession:
        def in_transaction(self) -> bool:
            return True

        async def rollback(self) -> None:
            raise RuntimeError("rollback exploded")

    await session_module._safe_rollback(cast(Any, FakeSession()))

    matching = [r for r in _session_error_records(caplog) if "session cleanup failed" in r.getMessage()]
    assert len(matching) == 1
    record = matching[0]
    assert "rollback" in record.getMessage()
    assert "in_transaction=True" in record.getMessage()
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], RuntimeError)


@pytest.mark.asyncio
async def test_safe_close_swallows_and_logs_error_when_close_fails(caplog) -> None:
    caplog.set_level(logging.ERROR)

    class FakeSession:
        def in_transaction(self) -> bool:
            return False

        async def close(self) -> None:
            raise RuntimeError("close exploded")

    await session_module._safe_close(cast(Any, FakeSession()))

    matching = [r for r in _session_error_records(caplog) if "session cleanup failed" in r.getMessage()]
    assert len(matching) == 1
    record = matching[0]
    assert "close" in record.getMessage()
    assert "in_transaction=False" in record.getMessage()
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], RuntimeError)


@pytest.mark.asyncio
async def test_safe_close_reports_unknown_transaction_state_when_session_is_broken(caplog) -> None:
    caplog.set_level(logging.ERROR)

    class BrokenSession:
        def in_transaction(self) -> bool:
            raise RuntimeError("session unusable")

        async def close(self) -> None:
            raise RuntimeError("close exploded")

    await session_module._safe_close(cast(Any, BrokenSession()))

    matching = [r for r in _session_error_records(caplog) if "session cleanup failed" in r.getMessage()]
    assert len(matching) == 1
    assert "in_transaction=unknown" in matching[0].getMessage()


@pytest.mark.asyncio
async def test_safe_rollback_stays_silent_when_only_caller_was_cancelled(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    started = asyncio.Event()
    release = asyncio.Event()
    rolled_back = asyncio.Event()

    class FakeSession:
        def in_transaction(self) -> bool:
            return True

        async def rollback(self) -> None:
            started.set()
            await release.wait()
            rolled_back.set()

    async def run_cleanup() -> None:
        await session_module._safe_rollback(cast(Any, FakeSession()))

    task = asyncio.create_task(run_cleanup())
    await started.wait()
    task.cancel()
    release.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert rolled_back.is_set()
    assert _session_error_records(caplog) == []


@pytest.mark.asyncio
async def test_safe_close_stays_silent_when_only_caller_was_cancelled(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    started = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()

    class FakeSession:
        async def close(self) -> None:
            started.set()
            await release.wait()
            closed.set()

    async def run_cleanup() -> None:
        await session_module._safe_close(cast(Any, FakeSession()))

    task = asyncio.create_task(run_cleanup())
    await started.wait()
    task.cancel()
    release.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert closed.is_set()
    assert _session_error_records(caplog) == []


@pytest.mark.asyncio
async def test_shielded_returns_normally_when_cleanup_completes() -> None:
    ran = asyncio.Event()

    async def cleanup() -> None:
        ran.set()

    await session_module._shielded(cleanup(), hung_timeout=0.1)

    assert ran.is_set()


@pytest.mark.asyncio
async def test_shielded_propagates_cleanup_exception_without_cancellation() -> None:
    async def failing_cleanup() -> None:
        raise RuntimeError("cleanup exploded")

    with pytest.raises(RuntimeError, match="cleanup exploded"):
        await session_module._shielded(failing_cleanup(), hung_timeout=0.1)


@pytest.mark.asyncio
async def test_shielded_propagates_cleanup_exception_after_caller_cancellation() -> None:
    started = asyncio.Event()

    async def failing_cleanup() -> None:
        started.set()
        await asyncio.sleep(0)
        raise RuntimeError("cleanup exploded")

    async def cancelled_caller() -> None:
        await session_module._shielded(failing_cleanup(), hung_timeout=1.0)

    task = asyncio.create_task(cancelled_caller())
    await started.wait()
    task.cancel()

    with pytest.raises(RuntimeError, match="cleanup exploded"):
        await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_shielded_abandons_hung_cleanup_after_timeout_and_logs(caplog) -> None:
    caplog.set_level(logging.ERROR)
    release = asyncio.Event()
    completed = asyncio.Event()

    async def hung_cleanup() -> None:
        await release.wait()
        completed.set()

    async def cancelled_caller() -> None:
        await session_module._shielded(hung_cleanup(), hung_timeout=0.05)

    task = asyncio.create_task(cancelled_caller())
    await asyncio.sleep(0)
    task.cancel()

    start = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0
    hung = [r for r in _session_error_records(caplog) if "session cleanup hung" in r.getMessage()]
    assert len(hung) == 1
    assert "timeout=0.05" in hung[0].getMessage()

    # The abandoned cleanup task is left running, not cancelled.
    release.set()
    for _ in range(10):
        await asyncio.sleep(0)

    assert completed.is_set()
    assert not [r for r in _session_error_records(caplog) if "session cleanup failed" in r.getMessage()]


@pytest.mark.asyncio
async def test_shielded_logs_late_failure_of_abandoned_hung_cleanup(caplog) -> None:
    caplog.set_level(logging.ERROR)
    release = asyncio.Event()

    async def hung_then_failing_cleanup() -> None:
        await release.wait()
        raise RuntimeError("late greenlet death")

    async def cancelled_caller() -> None:
        await session_module._shielded(hung_then_failing_cleanup(), hung_timeout=0.05)

    task = asyncio.create_task(cancelled_caller())
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)

    assert [r for r in _session_error_records(caplog) if "session cleanup hung" in r.getMessage()]

    release.set()
    for _ in range(10):
        await asyncio.sleep(0)

    late = [r for r in _session_error_records(caplog) if "session cleanup failed" in r.getMessage()]
    assert len(late) == 1
    assert late[0].exc_info is not None
    assert isinstance(late[0].exc_info[1], RuntimeError)
