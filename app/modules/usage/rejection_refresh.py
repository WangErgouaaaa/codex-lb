"""Fire-and-forget usage refresh triggered by usage-limit rejections.

When a request is rejected with a usage-limit style 429 (the account was just
marked ``RATE_LIMITED`` without upstream ``resets_at`` metadata), the persisted
cooldown is only the 30-second backoff floor, so the account returns to the
rotation and hits the same rejection again. This module schedules one
debounced, in-process forced usage fetch for that account; the real exhausted
window ``reset_at`` from the fetched usage snapshot is then CAS-extended onto
``accounts.reset_at`` so selection cools down until the true window ends.

The path is strictly extend-only (never shortens an existing deadline), never
raises, and merges concurrent rejections of the same account into a single
refresh per debounce window.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time

from app.core.balancer import plausible_rate_limit_reset_at
from app.core.balancer.logic import USAGE_EXHAUSTED_USED_PERCENT_THRESHOLD
from app.db.models import AccountStatus
from app.db.session import get_background_session
from app.modules.accounts.auth_manager import AuthManager
from app.modules.accounts.repository import AccountsRepository
from app.modules.proxy.account_cache import get_account_selection_cache
from app.modules.usage.repository import AdditionalUsageRepository, UsageRepository
from app.modules.usage.updater import UsageUpdater

logger = logging.getLogger(__name__)

# Window-exhaustion rejections only. ``rate_limit_exceeded`` is a burst
# "too fast" signal, not a window exhaustion signal, so the short backoff
# floor remains the right cooldown for it.
_USAGE_LIMIT_TRIGGER_CODES = frozenset(
    {"usage_limit_reached", "insufficient_quota", "usage_not_included", "quota_exceeded"}
)
_TRIGGER_DEBOUNCE_SECONDS = 15.0
# Shared with the balancer's exhausted-evidence deadline floor
# (app/core/balancer/logic.py) so both paths agree on where "exhausted"
# begins; kept as a module-level alias for existing callers/tests.
_EXHAUSTED_USED_PERCENT_THRESHOLD = USAGE_EXHAUSTED_USED_PERCENT_THRESHOLD
_NOT_BEFORE_MAX_ENTRIES = 512

# Mirrors updater._MAIN_USAGE_WINDOWS: the forced refresh writes one
# usage_history row per reported window.
_MAIN_USAGE_WINDOWS = ("primary", "secondary", "monthly")

# account_id -> earliest monotonic time a new refresh may be triggered.
_not_before: dict[str, float] = {}

# Prevents the fire-and-forget wrapper tasks from being garbage-collected
# while still running (asyncio only keeps weak references to tasks).
_pending_tasks: set[asyncio.Task[None]] = set()


def maybe_trigger_usage_limit_refresh(account_id: str, error_code: str | None) -> None:
    """Schedule a debounced forced usage refresh after a usage-limit rejection.

    Synchronous and never raises: callers invoke it right after
    ``mark_rate_limit`` on the request path, and any failure is logged, never
    propagated.
    """
    if error_code not in _USAGE_LIMIT_TRIGGER_CODES:
        return
    now = time.monotonic()
    earliest = _not_before.get(account_id)
    if earliest is not None and now < earliest:
        return
    # Record the debounce window BEFORE creating the task so rejections that
    # arrive while the task has not started yet cannot re-trigger.
    _not_before[account_id] = now + _TRIGGER_DEBOUNCE_SECONDS
    _prune_not_before(now)
    try:
        task = asyncio.create_task(_run_refresh(account_id))
    except RuntimeError:
        # No running event loop: the entry point must never raise.
        _not_before.pop(account_id, None)
        logger.warning(
            "Usage limit rejection refresh could not be scheduled account_id=%s",
            account_id,
        )
        return
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)


async def _run_refresh(account_id: str) -> None:
    try:
        await _refresh_and_extend(account_id)
    except Exception as exc:
        logger.warning(
            "Usage limit rejection refresh failed account_id=%s error=%s",
            account_id,
            exc,
            exc_info=True,
        )


async def _refresh_and_extend(account_id: str) -> None:
    async with get_background_session() as session:
        accounts_repo = AccountsRepository(session)
        usage_repo = UsageRepository(session)
        additional_usage_repo = AdditionalUsageRepository(session)
        current = await accounts_repo.get_by_id(account_id)
        if current is None or current.status != AccountStatus.RATE_LIMITED:
            # Nothing to refresh: the account was not benched by this
            # rejection (or was already repaired by another path).
            return
        result = await UsageUpdater(
            usage_repo,
            accounts_repo,
            additional_usage_repo,
            auth_manager=AuthManager(accounts_repo),
        ).force_refresh_result(current, ignore_refresh_disabled=True)
        if not result.fetch_succeeded:
            logger.info(
                "Usage limit rejection refresh fetch failed account_id=%s",
                account_id,
            )
            return
        new_deadline = await _latest_exhausted_window_deadline(usage_repo, account_id)
        if new_deadline is None:
            # No exhausted window in the fetched snapshot: the 429 may have
            # been a burst/race, keep the existing short fallback cooldown.
            return
        # Extend-only: never shorten a deadline a peer replica may rely on.
        if new_deadline <= float(current.reset_at or 0):
            return
        persisted_deadline = int(math.ceil(new_deadline))
        # Provenance invariant: the window-anchor the bench derived from is
        # recorded atomically with the deadline itself and must equal
        # ``reset_at``. The recovery escape hatch compares this anchor against
        # fresh usage anchors to detect that upstream replaced (early-rolled)
        # the benched window generation.
        updated = await accounts_repo.update_status_if_current(
            account_id,
            AccountStatus.RATE_LIMITED,
            reset_at=persisted_deadline,
            rate_limit_window_reset_at=persisted_deadline,
            expected_status=AccountStatus.RATE_LIMITED,
            expected_reset_at=current.reset_at,
        )
        if not updated:
            # A concurrent writer changed the row; the next usage-limit
            # rejection will retry with fresh expectations.
            logger.debug(
                "Usage limit rejection refresh lost CAS race account_id=%s",
                account_id,
            )
            return
        get_account_selection_cache().invalidate()
        logger.info(
            "usage_limit_refresh_extended_deadline account_id=%s reset_at=%s",
            account_id,
            persisted_deadline,
        )


async def _latest_exhausted_window_deadline(usage_repo: UsageRepository, account_id: str) -> float | None:
    """Return the latest valid reset deadline across exhausted usage windows.

    Reads the newest usage_history row per standard window and keeps only
    windows at/above the exhaustion threshold whose ``reset_at`` passes the
    shared rate-limit plausibility bounds.
    """
    now = time.time()
    deadline: float | None = None
    for window in _MAIN_USAGE_WINDOWS:
        entry = await usage_repo.latest_entry_for_account(account_id, window=window)
        if entry is None:
            continue
        if entry.used_percent is None or float(entry.used_percent) < _EXHAUSTED_USED_PERCENT_THRESHOLD:
            continue
        if entry.reset_at is None:
            continue
        plausible = plausible_rate_limit_reset_at(entry.reset_at, now=now)
        if plausible is None:
            continue
        if deadline is None or plausible > deadline:
            deadline = plausible
    return deadline


def _prune_not_before(now: float) -> None:
    if len(_not_before) < _NOT_BEFORE_MAX_ENTRIES:
        return
    expired = [account_id for account_id, earliest in _not_before.items() if earliest <= now]
    for account_id in expired:
        _not_before.pop(account_id, None)


def _clear_rejection_refresh_state() -> None:
    _not_before.clear()
    _pending_tasks.clear()
