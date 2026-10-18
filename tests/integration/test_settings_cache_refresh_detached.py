"""SettingsCache refresh detach regression tests (SQLite write-lock leak fix, batch C).

The refresh timeout must bound only the *wait*, never the DB work: a timed-out
or externally cancelled refresh keeps running in the background through the
hardened ``get_session()`` cleanup, its result is served by the next ``get()``,
and concurrent gets share a single in-flight refresh.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from time import monotonic

import pytest
from sqlalchemy import text

from app.core.config.settings_cache import SettingsCache
from app.db.session import SessionLocal, engine
from app.modules.settings.repository import SettingsRepository

pytestmark = pytest.mark.integration

_REAL_GET_OR_CREATE = SettingsRepository.get_or_create


class _SlowSettingsReads:
    """Patch ``SettingsRepository.get_or_create`` to sleep before the real read."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        counter = self
        delay = self.delay
        real = _REAL_GET_OR_CREATE

        async def slow_get_or_create(repo: SettingsRepository):
            counter.calls += 1
            await asyncio.sleep(delay)
            return await real(repo)

        monkeypatch.setattr(SettingsRepository, "get_or_create", slow_get_or_create)


async def _set_warmup_model(value: str) -> None:
    async with SessionLocal() as session:
        await SettingsRepository(session).get_or_create()
        await session.execute(text("UPDATE dashboard_settings SET warmup_model = :model"), {"model": value})
        await session.commit()


@pytest.mark.asyncio
async def test_timeout_serves_stale_then_background_refresh_completes(db_setup, monkeypatch, caplog) -> None:
    await _set_warmup_model("old-model")
    cache = SettingsCache(
        ttl_seconds=0.05,
        refresh_timeout_seconds=5.0,
        max_stale_seconds=60.0,
        retry_backoff_seconds=0.05,
    )
    primed = await cache.get()
    assert primed.warmup_model == "old-model"
    await _set_warmup_model("new-model")

    injector = _SlowSettingsReads(delay=1.0)
    injector.install(monkeypatch)
    cache._refresh_timeout_seconds = 0.15
    # Age the cached row past the TTL (but well within max-stale) so the next
    # get() must refresh and is still allowed to serve the stale value.
    cache._cached_at = monotonic() - 1.0

    with caplog.at_level(logging.WARNING, logger="app.core.config.settings_cache"):
        stale = await cache.get()

    assert stale is primed
    assert stale.warmup_model == "old-model"
    assert injector.calls == 1
    assert any("serving stale settings after refresh timeout" in record.getMessage() for record in caplog.records)

    # The timed-out refresh was not cancelled; it is still running detached.
    task = cache._refresh_task
    assert task is not None and not task.done() and not task.cancelled()

    settings = await task
    assert settings.warmup_model == "new-model"
    assert cache._cached_settings is settings
    # The background completion is what the next get() serves: no second read.
    cache._cached_at = monotonic()
    fresh = await cache.get()
    assert fresh is settings
    assert injector.calls == 1


@pytest.mark.asyncio
async def test_concurrent_gets_share_one_inflight_refresh(db_setup, monkeypatch) -> None:
    await _set_warmup_model("old-model")
    cache = SettingsCache(
        ttl_seconds=0.05,
        refresh_timeout_seconds=5.0,
        max_stale_seconds=60.0,
        retry_backoff_seconds=0.05,
    )
    primed = await cache.get()
    await _set_warmup_model("new-model")

    injector = _SlowSettingsReads(delay=1.0)
    injector.install(monkeypatch)
    cache._refresh_timeout_seconds = 0.15
    cache._cached_at = monotonic() - 1.0

    results = await asyncio.gather(cache.get(), cache.get(), cache.get())
    assert all(result is primed for result in results)
    assert injector.calls == 1

    first_task = cache._refresh_task
    assert first_task is not None and not first_task.done()

    # After the retry backoff expires the refresh is still in flight: the next
    # get() must join that task instead of stacking a second one.
    await asyncio.sleep(0.15)
    again = await cache.get()
    assert again is primed
    assert cache._refresh_task is first_task
    assert injector.calls == 1

    settings = await first_task
    assert settings.warmup_model == "new-model"
    assert cache._cached_settings is settings
    cache._cached_at = monotonic()
    final = await cache.get()
    assert final is settings
    assert injector.calls == 1


@pytest.mark.asyncio
async def test_external_write_lock_times_out_without_leaking(db_setup) -> None:
    """Cold refresh blocked on an external write lock raises on timeout, the
    blocked INSERT keeps running detached, and once the lock is released the
    session closes cleanly (an independent write succeeds promptly)."""
    cache = SettingsCache(
        ttl_seconds=5.0,
        refresh_timeout_seconds=0.2,
        max_stale_seconds=60.0,
        retry_backoff_seconds=5.0,
    )
    db_path = Path(str(engine.sync_engine.url.database))
    assert db_path.exists()

    holder = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(TimeoutError):
            await cache.get()

        task = cache._refresh_task
        assert task is not None and not task.done() and not task.cancelled()
        holder.execute("ROLLBACK")
    finally:
        holder.close()

    settings = await asyncio.wait_for(task, timeout=10.0)
    assert settings.id == 1
    assert cache._cached_settings is settings
    fresh = await cache.get()
    assert fresh is settings

    # Probe write: proves the detached task's session released the SQLite write
    # lock instead of leaking it behind a busy_timeout stall.
    async with SessionLocal() as session:
        await asyncio.wait_for(
            session.execute(text("UPDATE dashboard_settings SET warmup_model = warmup_model")),
            timeout=5.0,
        )
        await asyncio.wait_for(session.commit(), timeout=5.0)


@pytest.mark.asyncio
async def test_caller_cancellation_does_not_cancel_refresh(db_setup, monkeypatch) -> None:
    injector = _SlowSettingsReads(delay=0.5)
    injector.install(monkeypatch)

    cache = SettingsCache(
        ttl_seconds=5.0,
        refresh_timeout_seconds=5.0,
        max_stale_seconds=60.0,
        retry_backoff_seconds=5.0,
    )
    getter = asyncio.ensure_future(cache.get())
    await asyncio.sleep(0.1)
    getter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await getter

    task = cache._refresh_task
    assert task is not None and not task.cancelled()
    settings = await asyncio.wait_for(task, timeout=5.0)
    assert settings.id == 1
    assert injector.calls == 1

    # The cancelled caller's refresh populated the cache; the next get() reuses
    # it without touching the DB again.
    assert cache._cached_settings is settings
    fresh = await cache.get()
    assert fresh is settings
    assert injector.calls == 1
