from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, TypeVar, cast

from app.modules.usage.credit_attribution import CreditAttributionEngine

logger = logging.getLogger(__name__)

ATTRIBUTION_INTERVAL_SECONDS = 60

_T = TypeVar("_T")


class _LeaderElectionLike(Protocol):
    async def run_if_leader(self, fn: Callable[[], Awaitable[_T]]) -> _T | None: ...


def _get_leader_election() -> _LeaderElectionLike:
    module = importlib.import_module("app.core.scheduling.leader_election")
    return cast(_LeaderElectionLike, module.get_leader_election())


@dataclass(slots=True)
class CreditAttributionScheduler:
    """Leader-gated periodic pass that differences snapshots into credits.

    Leader-gated so multi-replica deployments attribute each snapshot chain
    exactly once; followers stay idle and take over after a leadership change
    (safe, because the pass is idempotent on the unique request+window key).
    """

    interval_seconds: int
    engine: CreditAttributionEngine = field(default_factory=CreditAttributionEngine)
    _task: asyncio.Task[None] | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if not self._task:
            return
        self._stop.set()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            await self._tick()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def _tick(self) -> None:
        await _get_leader_election().run_if_leader(self._tick_as_leader)

    async def _tick_as_leader(self) -> None:
        async with self._lock:
            try:
                await self.engine.run_pass()
            except Exception:
                logger.exception("Credit attribution pass failed")


def build_credit_attribution_scheduler() -> CreditAttributionScheduler:
    return CreditAttributionScheduler(interval_seconds=ATTRIBUTION_INTERVAL_SECONDS)
