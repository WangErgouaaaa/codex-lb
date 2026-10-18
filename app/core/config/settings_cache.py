from __future__ import annotations

import asyncio
import logging
from contextlib import aclosing
from time import monotonic

import anyio

from app.core.cache.invalidation import NAMESPACE_SETTINGS, get_cache_invalidation_poller
from app.db.models import DashboardSettings
from app.db.session import detach_session_objects, get_session
from app.modules.settings.repository import SettingsRepository

logger = logging.getLogger(__name__)


class SettingsCache:
    def __init__(
        self,
        *,
        ttl_seconds: float = 5.0,
        refresh_timeout_seconds: float = 2.0,
        max_stale_seconds: float = 60.0,
        retry_backoff_seconds: float = 5.0,
    ) -> None:
        if min(ttl_seconds, refresh_timeout_seconds, max_stale_seconds, retry_backoff_seconds) <= 0:
            raise ValueError("cache timing values must be positive")
        self._ttl_seconds = ttl_seconds
        self._refresh_timeout_seconds = refresh_timeout_seconds
        self._max_stale_seconds = max_stale_seconds
        self._retry_backoff_seconds = retry_backoff_seconds
        self._cached_settings: DashboardSettings | None = None
        self._cached_at = 0.0
        self._retry_after = 0.0
        self._lock = anyio.Lock()
        self._refresh_task: asyncio.Task[DashboardSettings] | None = None
        self._generation = 0

    async def get(self) -> DashboardSettings:
        now = monotonic()
        cached_age = now - self._cached_at
        if self._cached_settings is not None and (
            cached_age < self._ttl_seconds
            or (cached_age <= self._max_stale_seconds and now < self._retry_after)
        ):
            return self._cached_settings

        async with self._lock:
            now = monotonic()
            cached_age = now - self._cached_at
            if self._cached_settings is not None and (
                cached_age < self._ttl_seconds
                or (cached_age <= self._max_stale_seconds and now < self._retry_after)
            ):
                return self._cached_settings

            # Single-flight: join the in-flight refresh instead of stacking a
            # second DB read under concurrent gets or after a timeout.
            task = self._refresh_task
            if task is None or task.done():
                task = asyncio.ensure_future(self._refresh(self._generation))
                self._refresh_task = task
                task.add_done_callback(self._on_refresh_done)

            try:
                # The timeout bounds only the wait: the shield keeps the refresh
                # (including its session cleanup) running to completion in the
                # background instead of cancelling DB work mid-flight.
                settings = await asyncio.wait_for(asyncio.shield(task), timeout=self._refresh_timeout_seconds)
            except TimeoutError:
                now = monotonic()
                if self._cached_settings is None or now - self._cached_at > self._max_stale_seconds:
                    raise
                self._retry_after = now + self._retry_backoff_seconds
                logger.warning(
                    "serving stale settings after refresh timeout; age_seconds=%.3f max_stale_seconds=%.3f",
                    now - self._cached_at,
                    self._max_stale_seconds,
                )
                return self._cached_settings

            return settings

    async def _refresh(self, generation: int) -> DashboardSettings:
        settings = await self._read_settings()
        # An invalidate() that ran while this refresh was in flight bumped the
        # generation; the row snapshot predates it, so drop the result instead
        # of resurrecting stale data in the cache.
        if generation == self._generation:
            self._cached_settings = settings
            self._cached_at = monotonic()
            self._retry_after = 0.0
        return settings

    async def _read_settings(self) -> DashboardSettings:
        async with aclosing(get_session()) as sessions:
            async for session in sessions:
                settings = await SettingsRepository(session).get_or_create()
                # The cached row must outlive the session boundary; detaching
                # keeps its loaded state intact across the session's rollback.
                detach_session_objects(session)
                return settings
        raise AssertionError("get_session() produced no session")

    def _on_refresh_done(self, task: asyncio.Task[DashboardSettings]) -> None:
        if self._refresh_task is task:
            self._refresh_task = None
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            # Retrieve the exception so an unawaited background failure never
            # surfaces as "Task exception was never retrieved"; callers that
            # waited on the task already propagated it themselves.
            logger.debug("background settings refresh failed", exc_info=error)

    async def invalidate(self, *, propagate: bool = True) -> None:
        """Drop the cached settings row and, unless ``propagate`` is False, durably
        bump the cross-replica ``settings`` namespace before returning.

        Settings mutations are security-bearing (password hash, guest access, TOTP,
        API-key auth toggle), so the bump is awaited rather than coalesced. The
        cache-invalidation poller callback registers ``propagate=False`` so a remote
        bump never re-bumps (feedback-loop prevention).
        """
        async with self._lock:
            self._cached_settings = None
            self._cached_at = 0.0
            self._retry_after = 0.0
            # Detach any in-flight refresh: its row snapshot predates this
            # invalidation (the generation check drops the result), but the task
            # itself keeps running -- DB work is never cancelled here.
            self._generation += 1
            self._refresh_task = None
        if propagate:
            poller = get_cache_invalidation_poller()
            if poller is not None:
                await poller.bump(NAMESPACE_SETTINGS)


_settings_cache = SettingsCache()


def get_settings_cache() -> SettingsCache:
    return _settings_cache
