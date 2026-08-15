from __future__ import annotations

import logging
from time import monotonic

import anyio

from app.core.cache.invalidation import NAMESPACE_SETTINGS, get_cache_invalidation_poller
from app.db.models import DashboardSettings
from app.db.session import SessionLocal
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

            try:
                with anyio.fail_after(self._refresh_timeout_seconds):
                    async with SessionLocal() as session:
                        settings = await SettingsRepository(session).get_or_create()
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

            self._cached_settings = settings
            self._cached_at = monotonic()
            self._retry_after = 0.0
            return settings

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
        if propagate:
            poller = get_cache_invalidation_poller()
            if poller is not None:
                await poller.bump(NAMESPACE_SETTINGS)


_settings_cache = SettingsCache()


def get_settings_cache() -> SettingsCache:
    return _settings_cache
