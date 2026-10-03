"""Pause accounts that trip upstream content-moderation in bursts.

A single flagged request is normal noise, and the generic transient-error
backoff never catches moderation bursts: interleaved successes reset
``error_count`` (observed in production: 168 ``bio_policy`` flags in 32
minutes with roughly half the interleaved requests succeeding, zero
routing impact, and the account banned by upstream hours later).

This guard keeps a per-account rolling window of moderation error codes;
when the window fills to ``bio_policy_burst_threshold`` flags, the account
is moved to ``PAUSED`` with an explanatory ``deactivation_reason`` so a
human reviews the traffic before the account is re-enabled. The pause is
deliberately manual: content that keeps tripping moderation would do so
again right after any automatic cooldown expires.

Counting uses a rolling window, not consecutive flags, because production
bursts interleave successes between the flags (2026-10-03: 6 flags in 4
minutes with a success inside the same second as every flag; the 168-flag
ban burst had roughly half its interleaved requests succeeding), so a
reset-on-success counter would never reach its threshold.

Real moderation rejections arrive as terminal SSE ``response.failed``
events. Those are not account-health-penalizable codes, so the settlement
consumption path never routes them through ``_handle_stream_error``; both
that exception-shaped path and the stream-event path funnel into
``handle_moderation_stream_error`` so counting is independent of the
failure shape.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from typing import TYPE_CHECKING

from app.core.audit.service import AuditService
from app.core.config.settings import get_settings

if TYPE_CHECKING:
    from app.db.models import Account
    from app.modules.proxy.load_balancer import LoadBalancer

logger = logging.getLogger(__name__)

_FLAGS_MAX_ACCOUNTS = 4096

# account_id -> timestamps (time.time()) of moderation flags inside the window.
_flags: dict[str, deque[float]] = defaultdict(deque)


def record_moderation_flag(account_id: str, error_code: str | None) -> bool:
    """Append one moderation flag; return True when the burst threshold is met."""
    if not error_code:
        return False
    settings = get_settings()
    if error_code not in settings.bio_policy_burst_error_codes:
        return False
    now = time.time()
    horizon = now - float(settings.bio_policy_burst_window_seconds)
    window = _flags[account_id]
    window.append(now)
    while window and window[0] <= horizon:
        window.popleft()
    _prune_stale_accounts(now, horizon)
    return len(window) >= settings.bio_policy_burst_threshold


async def pause_account_for_moderation_burst(
    load_balancer: LoadBalancer,
    account: Account,
    *,
    error_code: str,
) -> bool:
    """Pause *account* after a moderation burst and record the audit trail.

    Never raises: the caller is the streaming error path and a guard failure
    must not mask the original upstream error handling.
    """
    try:
        flags = len(_flags.get(account.id, ()))
        window_minutes = get_settings().bio_policy_burst_window_seconds // 60
        reason = f"Content moderation burst: {flags} '{error_code}' flags in {window_minutes}m; paused pending review"
        paused = await load_balancer.mark_bio_policy_pause(account, reason)
        if paused:
            # Restart counting from zero after a confirmed pause so a later
            # manual re-enable needs a fresh burst (not one leftover flag) to
            # trip the guard again.
            _flags.pop(account.id, None)
            logger.warning(
                "bio_policy_burst_paused account_id=%s flags=%d window_minutes=%d error_code=%s",
                account.id,
                flags,
                window_minutes,
                error_code,
            )
            AuditService.log_async(
                "account.bio_policy_pause",
                details={
                    "account_id": account.id,
                    "email": account.email or "",
                    "flags": flags,
                    "window_minutes": window_minutes,
                    "error_code": error_code,
                },
            )
        return paused
    except Exception:
        logger.warning(
            "bio_policy burst pause failed account_id=%s error_code=%s",
            account.id,
            error_code,
            exc_info=True,
        )
        return False


async def handle_moderation_stream_error(
    load_balancer: LoadBalancer,
    account: Account,
    *,
    error_code: str | None,
) -> None:
    """Count one moderation flag from a terminal upstream error; pause on burst.

    No-op for non-moderation codes; never raises, so both call sites (the
    ``_handle_stream_error`` transient branch and the streaming settlement
    path) can invoke it unconditionally.
    """
    settings = get_settings()
    if not error_code or error_code not in settings.bio_policy_burst_error_codes:
        return
    tripped = record_moderation_flag(account.id, error_code)
    logger.info(
        "moderation_flag_counted account_id=%s error_code=%s window_count=%d threshold=%d",
        account.id,
        error_code,
        len(_flags.get(account.id, ())),
        settings.bio_policy_burst_threshold,
    )
    if tripped:
        await pause_account_for_moderation_burst(load_balancer, account, error_code=error_code)


def _prune_stale_accounts(now: float, horizon: float) -> None:
    if len(_flags) < _FLAGS_MAX_ACCOUNTS:
        return
    stale = [account_id for account_id, window in _flags.items() if not window or window[-1] <= horizon]
    for account_id in stale:
        _flags.pop(account_id, None)


def _clear_bio_policy_guard_state() -> None:
    _flags.clear()


__all__ = [
    "handle_moderation_stream_error",
    "pause_account_for_moderation_burst",
    "record_moderation_flag",
]
