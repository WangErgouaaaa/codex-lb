"""Unit tests for the usage-limit rejection triggered usage refresh."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Collection
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from app.core.clients.usage import UsageFetchError
from app.core.crypto import TokenEncryptor
from app.core.usage.models import RateLimitPayload, UsagePayload, UsageWindow
from app.db.models import Account, AccountStatus, UsageHistory
from app.modules.usage import rejection_refresh as rejection_refresh_module
from app.modules.usage import updater as usage_updater_module
from app.modules.usage.repository import UsageWindowWrite

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_refresh_state():
    """Clear module-level refresh state between tests."""
    usage_updater_module._clear_usage_refresh_state()
    rejection_refresh_module._clear_rejection_refresh_state()
    yield
    usage_updater_module._clear_usage_refresh_state()
    rejection_refresh_module._clear_rejection_refresh_state()


@dataclass(frozen=True, slots=True)
class _Settings:
    usage_refresh_enabled: bool = True
    usage_refresh_interval_seconds: int = 60
    usage_refresh_auth_failure_cooldown_seconds: int = 0


def _make_account(account_id: str, *, status: AccountStatus = AccountStatus.ACTIVE) -> Account:
    encryptor = TokenEncryptor()
    return Account(
        id=account_id,
        chatgpt_account_id=f"chatgpt_{account_id}",
        email=f"{account_id}@example.com",
        plan_type="plus",
        access_token_encrypted=encryptor.encrypt("access"),
        refresh_token_encrypted=encryptor.encrypt("refresh"),
        id_token_encrypted=encryptor.encrypt("id"),
        last_refresh=datetime.now(tz=timezone.utc),
        status=status,
        deactivation_reason=None,
    )


def _make_rate_limited_account(account_id: str, *, reset_at: int | None) -> Account:
    now = int(time.time())
    account = _make_account(account_id, status=AccountStatus.RATE_LIMITED)
    account.reset_at = reset_at if reset_at is not None else now + 30
    account.blocked_at = now
    return account


class StubUsageRepository:
    def __init__(self) -> None:
        self._latest: dict[str, UsageHistory] = {}

    async def latest_entry_for_account(self, account_id: str, *, window: str | None = None) -> UsageHistory | None:
        return self._latest.get(window or "primary")

    async def add_account_snapshot(
        self,
        account_id: str,
        windows: Collection[UsageWindowWrite],
        *,
        recorded_at: datetime | None = None,
    ) -> list[UsageHistory]:
        captured_at = recorded_at or datetime.now(tz=timezone.utc)
        entries: list[UsageHistory] = []
        for window in windows:
            entry = UsageHistory(
                account_id=account_id,
                used_percent=window.used_percent,
                window=window.window,
                reset_at=window.reset_at,
                window_minutes=window.window_minutes,
                recorded_at=captured_at,
            )
            self._latest[window.window] = entry
            entries.append(entry)
        return entries


class StubAdditionalUsageRepository:
    pass


class StubAccountsRepository:
    def __init__(self, account: Account | None) -> None:
        self.account = account
        self.cas_calls: list[dict[str, Any]] = []
        self.cas_result = True

    async def get_by_id(self, account_id: str) -> Account | None:
        if self.account is not None and account_id == self.account.id:
            return self.account
        return None

    async def update_status_if_current(
        self,
        account_id: str,
        status: AccountStatus,
        deactivation_reason: str | None = None,
        reset_at: int | None = None,
        blocked_at: int | None | object = ...,
        **kwargs: Any,
    ) -> bool:
        self.cas_calls.append(
            {
                "account_id": account_id,
                "status": status,
                "deactivation_reason": deactivation_reason,
                "reset_at": reset_at,
                **kwargs,
            }
        )
        return self.cas_result


class StubSelectionCache:
    def __init__(self) -> None:
        self.invalidate_calls = 0

    def invalidate(self) -> None:
        self.invalidate_calls += 1


def _exhausted_payload(*, primary_reset_at: int, secondary_used: float = 40.0) -> UsagePayload:
    return UsagePayload(
        plan_type="plus",
        rate_limit=RateLimitPayload(
            primary_window=UsageWindow(used_percent=100.0, reset_at=primary_reset_at),
            secondary_window=UsageWindow(used_percent=secondary_used, reset_at=primary_reset_at + 86400),
        ),
    )


def _available_payload(*, used_percent: float = 40.0) -> UsagePayload:
    return UsagePayload(
        plan_type="plus",
        rate_limit=RateLimitPayload(
            primary_window=UsageWindow(used_percent=used_percent, reset_at=int(time.time()) + 600),
        ),
    )


def _install_harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    account: Account | None,
    payload: UsagePayload | None,
    fetch_calls: list[str] | None = None,
) -> tuple[StubAccountsRepository, StubUsageRepository, StubSelectionCache]:
    stub_accounts = StubAccountsRepository(account)
    stub_usage = StubUsageRepository()
    stub_cache = StubSelectionCache()

    @asynccontextmanager
    async def fake_background_session():
        yield object()

    async def fake_fetch_usage(**kwargs: Any):
        if fetch_calls is not None:
            fetch_calls.append(str(kwargs.get("account_id")))
        if isinstance(payload, UsageFetchError):
            raise payload
        return payload

    async def fake_resolve_route(account: Account, *, operation: str):
        return None

    monkeypatch.setattr(rejection_refresh_module, "get_background_session", fake_background_session)
    monkeypatch.setattr(rejection_refresh_module, "AccountsRepository", lambda session: stub_accounts)
    monkeypatch.setattr(rejection_refresh_module, "UsageRepository", lambda session: stub_usage)
    monkeypatch.setattr(
        rejection_refresh_module, "AdditionalUsageRepository", lambda session: StubAdditionalUsageRepository()
    )
    monkeypatch.setattr(rejection_refresh_module, "get_account_selection_cache", lambda: stub_cache)
    monkeypatch.setattr(usage_updater_module, "get_settings", _Settings)
    monkeypatch.setattr(usage_updater_module, "fetch_usage", fake_fetch_usage)
    monkeypatch.setattr(usage_updater_module, "_resolve_upstream_route_for_account", fake_resolve_route)
    return stub_accounts, stub_usage, stub_cache


async def _drain_pending_tasks() -> None:
    pending = list(rejection_refresh_module._pending_tasks)
    if pending:
        await asyncio.gather(*pending)


# ---------------------------------------------------------------------------
# Gating and debounce
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_only_fires_for_usage_limit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    executed: list[str] = []

    async def fake_run(account_id: str) -> None:
        executed.append(account_id)

    monkeypatch.setattr(rejection_refresh_module, "_run_refresh", fake_run)

    rejection_refresh_module.maybe_trigger_usage_limit_refresh("acc_code_hit", "usage_limit_reached")
    rejection_refresh_module.maybe_trigger_usage_limit_refresh("acc_code_quota", "quota_exceeded")
    rejection_refresh_module.maybe_trigger_usage_limit_refresh("acc_code_not_included", "usage_not_included")
    rejection_refresh_module.maybe_trigger_usage_limit_refresh("acc_code_insufficient", "insufficient_quota")
    await _drain_pending_tasks()
    assert sorted(executed) == [
        "acc_code_hit",
        "acc_code_insufficient",
        "acc_code_not_included",
        "acc_code_quota",
    ]

    # Burst-style rate limit, missing and unknown codes never trigger.
    for account_id, code in (
        ("acc_code_burst", "rate_limit_exceeded"),
        ("acc_code_none", None),
        ("acc_code_unknown", "some_other_code"),
    ):
        rejection_refresh_module.maybe_trigger_usage_limit_refresh(account_id, code)
    await _drain_pending_tasks()
    assert sorted(executed) == [
        "acc_code_hit",
        "acc_code_insufficient",
        "acc_code_not_included",
        "acc_code_quota",
    ]


@pytest.mark.asyncio
async def test_trigger_debounces_within_window_and_allows_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed: list[str] = []

    async def fake_run(account_id: str) -> None:
        executed.append(account_id)

    monkeypatch.setattr(rejection_refresh_module, "_run_refresh", fake_run)

    real_monotonic = time.monotonic
    real_time = time.time
    clock_offset = 0.0
    clock = type(
        "Clock",
        (),
        {
            "monotonic": staticmethod(lambda: real_monotonic() + clock_offset),
            "time": staticmethod(lambda: real_time() + clock_offset),
        },
    )
    monkeypatch.setattr(rejection_refresh_module, "time", clock)

    rejection_refresh_module.maybe_trigger_usage_limit_refresh("acc_debounce", "usage_limit_reached")
    await _drain_pending_tasks()
    assert executed == ["acc_debounce"]

    # Inside the 15s debounce window: suppressed (recorded not-before wins
    # even though the task already finished).
    clock_offset = 5.0
    rejection_refresh_module.maybe_trigger_usage_limit_refresh("acc_debounce", "usage_limit_reached")
    await _drain_pending_tasks()
    assert executed == ["acc_debounce"]

    # Past the debounce window: a new refresh is allowed.
    clock_offset = 20.0
    rejection_refresh_module.maybe_trigger_usage_limit_refresh("acc_debounce", "usage_limit_reached")
    await _drain_pending_tasks()
    assert executed == ["acc_debounce", "acc_debounce"]


@pytest.mark.asyncio
async def test_trigger_is_debounced_across_accounts_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed: list[str] = []

    async def fake_run(account_id: str) -> None:
        executed.append(account_id)

    monkeypatch.setattr(rejection_refresh_module, "_run_refresh", fake_run)

    rejection_refresh_module.maybe_trigger_usage_limit_refresh("acc_a", "usage_limit_reached")
    rejection_refresh_module.maybe_trigger_usage_limit_refresh("acc_b", "usage_limit_reached")
    rejection_refresh_module.maybe_trigger_usage_limit_refresh("acc_a", "usage_limit_reached")
    await _drain_pending_tasks()
    assert executed == ["acc_a", "acc_b"]


# ---------------------------------------------------------------------------
# Refresh and deadline extension flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_extends_deadline_from_exhausted_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    account = _make_rate_limited_account("acc_extend", reset_at=now + 30)
    fetch_calls: list[str] = []
    stub_accounts, _stub_usage, stub_cache = _install_harness(
        monkeypatch,
        account=account,
        payload=_exhausted_payload(primary_reset_at=now + 18000),
        fetch_calls=fetch_calls,
    )

    rejection_refresh_module.maybe_trigger_usage_limit_refresh(account.id, "usage_limit_reached")
    await _drain_pending_tasks()

    assert fetch_calls == [account.chatgpt_account_id]
    assert len(stub_accounts.cas_calls) == 1
    call = stub_accounts.cas_calls[0]
    assert call["account_id"] == account.id
    assert call["status"] == AccountStatus.RATE_LIMITED
    assert call["reset_at"] == now + 18000
    # Window-anchor provenance is written atomically with the deadline and
    # must equal reset_at (recovery-generation escape invariant).
    assert call["expected_status"] == AccountStatus.RATE_LIMITED
    assert call["expected_reset_at"] == now + 30
    assert stub_cache.invalidate_calls == 1


@pytest.mark.asyncio
async def test_refresh_never_shortens_existing_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    account = _make_rate_limited_account("acc_no_shorten", reset_at=now + 36000)
    stub_accounts, _stub_usage, stub_cache = _install_harness(
        monkeypatch,
        account=account,
        payload=_exhausted_payload(primary_reset_at=now + 18000),
    )

    rejection_refresh_module.maybe_trigger_usage_limit_refresh(account.id, "usage_limit_reached")
    await _drain_pending_tasks()

    assert stub_accounts.cas_calls == []
    assert stub_cache.invalidate_calls == 0


@pytest.mark.asyncio
async def test_refresh_without_exhausted_window_keeps_fallback_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    account = _make_rate_limited_account("acc_not_exhausted", reset_at=now + 30)
    stub_accounts, _stub_usage, stub_cache = _install_harness(
        monkeypatch,
        account=account,
        payload=_available_payload(used_percent=40.0),
    )

    rejection_refresh_module.maybe_trigger_usage_limit_refresh(account.id, "usage_limit_reached")
    await _drain_pending_tasks()

    assert stub_accounts.cas_calls == []
    assert stub_cache.invalidate_calls == 0


@pytest.mark.asyncio
async def test_refresh_with_fetch_failure_does_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    account = _make_rate_limited_account("acc_fetch_fail", reset_at=now + 30)
    stub_accounts, _stub_usage, stub_cache = _install_harness(
        monkeypatch,
        account=account,
        payload=UsageFetchError(500, "upstream down"),
    )

    rejection_refresh_module.maybe_trigger_usage_limit_refresh(account.id, "usage_limit_reached")
    await _drain_pending_tasks()

    assert stub_accounts.cas_calls == []
    assert stub_cache.invalidate_calls == 0


@pytest.mark.asyncio
async def test_refresh_skips_account_not_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _make_account("acc_active", status=AccountStatus.ACTIVE)
    fetch_calls: list[str] = []
    stub_accounts, _stub_usage, stub_cache = _install_harness(
        monkeypatch,
        account=account,
        payload=_exhausted_payload(primary_reset_at=int(time.time()) + 18000),
        fetch_calls=fetch_calls,
    )

    rejection_refresh_module.maybe_trigger_usage_limit_refresh(account.id, "usage_limit_reached")
    await _drain_pending_tasks()

    assert fetch_calls == []
    assert stub_accounts.cas_calls == []
    assert stub_cache.invalidate_calls == 0


@pytest.mark.asyncio
async def test_refresh_tolerates_missing_account(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_accounts, _stub_usage, stub_cache = _install_harness(
        monkeypatch,
        account=None,
        payload=_exhausted_payload(primary_reset_at=int(time.time()) + 18000),
    )

    rejection_refresh_module.maybe_trigger_usage_limit_refresh("acc_ghost", "usage_limit_reached")
    await _drain_pending_tasks()

    assert stub_accounts.cas_calls == []
    assert stub_cache.invalidate_calls == 0


@pytest.mark.asyncio
async def test_refresh_cas_miss_keeps_selection_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    account = _make_rate_limited_account("acc_cas_miss", reset_at=now + 30)
    stub_accounts, _stub_usage, stub_cache = _install_harness(
        monkeypatch,
        account=account,
        payload=_exhausted_payload(primary_reset_at=now + 18000),
    )
    stub_accounts.cas_result = False

    rejection_refresh_module.maybe_trigger_usage_limit_refresh(account.id, "usage_limit_reached")
    await _drain_pending_tasks()

    assert len(stub_accounts.cas_calls) == 1
    assert stub_cache.invalidate_calls == 0


@pytest.mark.asyncio
async def test_refresh_logs_warning_when_refresh_raises(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = int(time.time())
    account = _make_rate_limited_account("acc_boom", reset_at=now + 30)

    @asynccontextmanager
    async def exploding_background_session():
        raise RuntimeError("session exploded")
        yield

    _install_harness(monkeypatch, account=account, payload=None)
    monkeypatch.setattr(rejection_refresh_module, "get_background_session", exploding_background_session)

    with caplog.at_level("WARNING", logger=rejection_refresh_module.logger.name):
        rejection_refresh_module.maybe_trigger_usage_limit_refresh(account.id, "usage_limit_reached")
        await _drain_pending_tasks()

    assert any("Usage limit rejection refresh failed" in record.getMessage() for record in caplog.records)
