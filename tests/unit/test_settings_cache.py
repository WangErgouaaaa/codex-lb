from __future__ import annotations

import logging
from types import SimpleNamespace

import anyio
import pytest

import app.core.config.settings_cache as settings_cache_module
from app.core.config.settings_cache import SettingsCache

pytestmark = pytest.mark.unit


class _FakeSessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_settings_cache_ttl_and_invalidate(monkeypatch) -> None:
    state = {"now": 100.0, "calls": 0}

    class _FakeRepository:
        def __init__(self, _session) -> None:
            pass

        async def get_or_create(self):
            state["calls"] += 1
            return SimpleNamespace(version=state["calls"])

    monkeypatch.setattr(settings_cache_module, "SessionLocal", lambda: _FakeSessionContext())
    monkeypatch.setattr(settings_cache_module, "SettingsRepository", _FakeRepository)
    monkeypatch.setattr(settings_cache_module, "monotonic", lambda: state["now"])

    cache = SettingsCache(ttl_seconds=5.0)

    first = await cache.get()
    second = await cache.get()
    assert first is second
    assert state["calls"] == 1

    state["now"] = 106.0
    third = await cache.get()
    assert third is not first
    assert state["calls"] == 2

    await cache.invalidate()
    fourth = await cache.get()
    assert fourth is not third
    assert state["calls"] == 3


@pytest.mark.asyncio
async def test_settings_cache_returns_stale_value_when_refresh_times_out(monkeypatch, caplog) -> None:
    state = {"now": 100.0, "calls": 0, "block_refresh": False}

    class _FakeRepository:
        def __init__(self, _session) -> None:
            pass

        async def get_or_create(self):
            state["calls"] += 1
            if state["block_refresh"]:
                await anyio.sleep_forever()
            return SimpleNamespace(version=state["calls"])

    monkeypatch.setattr(settings_cache_module, "SessionLocal", lambda: _FakeSessionContext())
    monkeypatch.setattr(settings_cache_module, "SettingsRepository", _FakeRepository)
    monkeypatch.setattr(settings_cache_module, "monotonic", lambda: state["now"])

    cache = SettingsCache(
        ttl_seconds=5.0,
        refresh_timeout_seconds=0.01,
        max_stale_seconds=60.0,
        retry_backoff_seconds=5.0,
    )
    first = await cache.get()

    state["now"] = 106.0
    state["block_refresh"] = True
    with caplog.at_level(logging.WARNING), anyio.fail_after(0.2):
        stale = await cache.get()

    assert stale is first
    assert state["calls"] == 2
    assert "serving stale settings" in caplog.text


@pytest.mark.asyncio
async def test_settings_cache_cold_refresh_timeout_is_not_hidden(monkeypatch) -> None:
    class _BlockingRepository:
        def __init__(self, _session) -> None:
            pass

        async def get_or_create(self):
            await anyio.sleep_forever()

    monkeypatch.setattr(settings_cache_module, "SessionLocal", lambda: _FakeSessionContext())
    monkeypatch.setattr(settings_cache_module, "SettingsRepository", _BlockingRepository)

    cache = SettingsCache(
        ttl_seconds=5.0,
        refresh_timeout_seconds=0.01,
        max_stale_seconds=60.0,
        retry_backoff_seconds=5.0,
    )

    with pytest.raises(TimeoutError):
        await cache.get()


@pytest.mark.asyncio
async def test_settings_cache_refresh_timeout_uses_backoff_for_waiting_callers(monkeypatch) -> None:
    state = {"now": 100.0, "calls": 0, "block_refresh": False}

    class _FakeRepository:
        def __init__(self, _session) -> None:
            pass

        async def get_or_create(self):
            state["calls"] += 1
            if state["block_refresh"]:
                await anyio.sleep_forever()
            return SimpleNamespace(version=state["calls"])

    monkeypatch.setattr(settings_cache_module, "SessionLocal", lambda: _FakeSessionContext())
    monkeypatch.setattr(settings_cache_module, "SettingsRepository", _FakeRepository)
    monkeypatch.setattr(settings_cache_module, "monotonic", lambda: state["now"])

    cache = SettingsCache(
        ttl_seconds=5.0,
        refresh_timeout_seconds=0.01,
        max_stale_seconds=60.0,
        retry_backoff_seconds=5.0,
    )
    first = await cache.get()

    state["now"] = 106.0
    state["block_refresh"] = True
    results: list[object] = []

    async def _read_cache() -> None:
        results.append(await cache.get())

    async with anyio.create_task_group() as task_group:
        for _ in range(5):
            task_group.start_soon(_read_cache)

    assert results == [first] * 5
    assert state["calls"] == 2


@pytest.mark.asyncio
async def test_settings_cache_does_not_serve_beyond_max_stale_during_backoff(monkeypatch) -> None:
    state = {"now": 100.0, "calls": 0, "block_refresh": False}

    class _FakeRepository:
        def __init__(self, _session) -> None:
            pass

        async def get_or_create(self):
            state["calls"] += 1
            if state["block_refresh"]:
                await anyio.sleep_forever()
            return SimpleNamespace(version=state["calls"])

    monkeypatch.setattr(settings_cache_module, "SessionLocal", lambda: _FakeSessionContext())
    monkeypatch.setattr(settings_cache_module, "SettingsRepository", _FakeRepository)
    monkeypatch.setattr(settings_cache_module, "monotonic", lambda: state["now"])

    cache = SettingsCache(
        ttl_seconds=5.0,
        refresh_timeout_seconds=0.01,
        max_stale_seconds=10.0,
        retry_backoff_seconds=5.0,
    )
    await cache.get()

    state["now"] = 109.0
    state["block_refresh"] = True
    await cache.get()

    state["now"] = 111.0
    with pytest.raises(TimeoutError):
        await cache.get()

    assert state["calls"] == 3
