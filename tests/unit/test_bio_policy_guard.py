"""Unit tests for the content-moderation (bio_policy) burst pause guard."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.crypto import TokenEncryptor
from app.db.models import Account, AccountStatus
from app.modules.proxy import bio_policy_guard as guard_module
from app.modules.proxy import load_balancer as lb_module
from app.modules.proxy.bio_policy_guard import (
    handle_moderation_stream_error,
    pause_account_for_moderation_burst,
    record_moderation_flag,
)
from app.modules.proxy.load_balancer import LoadBalancer

pytestmark = pytest.mark.unit


@dataclass(frozen=True, slots=True)
class _Settings:
    bio_policy_burst_error_codes: frozenset[str] = frozenset({"bio_policy"})
    bio_policy_burst_window_seconds: int = 900
    bio_policy_burst_threshold: int = 5


@pytest.fixture(autouse=True)
def _install_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(guard_module, "get_settings", lambda: _Settings())
    guard_module._clear_bio_policy_guard_state()
    yield
    guard_module._clear_bio_policy_guard_state()


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


@dataclass
class _FakeClock:
    now: float = 1_000_000.0

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _install_clock(monkeypatch: pytest.MonkeyPatch, clock: _FakeClock) -> None:
    monkeypatch.setattr(guard_module, "time", SimpleNamespace(time=clock.time))


class _StubLoadBalancer:
    def __init__(self, *, result: bool = True, exc: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._result = result
        self._exc = exc

    async def mark_bio_policy_pause(self, account: Any, reason: str) -> bool:
        self.calls.append((account.id, reason))
        if self._exc is not None:
            raise self._exc
        return self._result


@dataclass
class _AuditRecorder:
    entries: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def __call__(self, action: str, actor_ip: str | None = None, details: Any = None, request_id: Any = None) -> None:
        self.entries.append((action, dict(details or {})))


@pytest.fixture
def audit_recorder(monkeypatch: pytest.MonkeyPatch) -> _AuditRecorder:
    recorder = _AuditRecorder()
    monkeypatch.setattr(guard_module.AuditService, "log_async", recorder)
    return recorder


# --- record_moderation_flag -------------------------------------------------


def test_unrelated_error_codes_are_ignored():
    assert record_moderation_flag("acct", "upstream_error") is False
    assert record_moderation_flag("acct", None) is False
    assert record_moderation_flag("acct", "") is False
    assert guard_module._flags == {}


def test_threshold_trips_exactly_on_nth_flag():
    for _ in range(4):
        assert record_moderation_flag("acct", "bio_policy") is False
    assert record_moderation_flag("acct", "bio_policy") is True


def test_flags_outside_window_expire(monkeypatch: pytest.MonkeyPatch):
    clock = _FakeClock()
    _install_clock(monkeypatch, clock)

    for _ in range(4):
        record_moderation_flag("acct", "bio_policy")

    clock.advance(901)
    # Four stale flags plus one fresh flag stay below the threshold.
    assert record_moderation_flag("acct", "bio_policy") is False
    for _ in range(3):
        record_moderation_flag("acct", "bio_policy")
    assert record_moderation_flag("acct", "bio_policy") is True


def test_interleaved_successes_do_not_reset_the_window():
    # The 2026-10-03 burst had a success inside the same second as every
    # flag; the window must fill regardless (no success hook exists).
    for _ in range(4):
        record_moderation_flag("acct", "bio_policy")
    assert record_moderation_flag("acct", "bio_policy") is True


def test_windows_are_per_account():
    for _ in range(4):
        record_moderation_flag("acct-a", "bio_policy")
    assert record_moderation_flag("acct-b", "bio_policy") is False
    assert guard_module._flags.get("acct-b") is not None
    assert len(guard_module._flags["acct-a"]) == 4


# --- handle_moderation_stream_error -------------------------------------------


@pytest.mark.asyncio
async def test_stream_error_handler_ignores_non_moderation_codes():
    lb = _StubLoadBalancer()
    account = _make_account("acct")

    await handle_moderation_stream_error(lb, account, error_code="server_error")
    await handle_moderation_stream_error(lb, account, error_code=None)

    assert lb.calls == []
    assert guard_module._flags == {}


@pytest.mark.asyncio
async def test_stream_error_handler_counts_and_pauses_on_burst(audit_recorder: _AuditRecorder):
    lb = _StubLoadBalancer()
    account = _make_account("acct")

    for _ in range(4):
        await handle_moderation_stream_error(lb, account, error_code="bio_policy")
    assert lb.calls == []

    await handle_moderation_stream_error(lb, account, error_code="bio_policy")
    assert len(lb.calls) == 1
    assert "5 'bio_policy' flags in 15m" in lb.calls[0][1]
    # Window restarts after the confirmed pause.
    assert "acct" not in guard_module._flags


@pytest.mark.asyncio
async def test_stream_error_handler_never_raises_on_lb_failure():
    lb = _StubLoadBalancer(exc=RuntimeError("boom"))
    account = _make_account("acct")

    for _ in range(4):
        await handle_moderation_stream_error(lb, account, error_code="bio_policy")
    # Fifth flag trips the pause, which raises internally and is swallowed.
    await handle_moderation_stream_error(lb, account, error_code="bio_policy")
    # Counting is preserved so a later burst can still trip the guard.
    assert len(guard_module._flags["acct"]) == 5


# --- pause_account_for_moderation_burst --------------------------------------


@pytest.mark.asyncio
async def test_burst_pause_pauses_and_records_audit(audit_recorder: _AuditRecorder):
    account = _make_account("acct")
    lb = _StubLoadBalancer()
    for _ in range(5):
        record_moderation_flag("acct", "bio_policy")

    assert await pause_account_for_moderation_burst(lb, account, error_code="bio_policy") is True

    assert len(lb.calls) == 1
    account_id, reason = lb.calls[0]
    assert account_id == "acct"
    assert "5 'bio_policy' flags in 15m" in reason
    assert "paused pending review" in reason
    # The window restarts from zero after a confirmed pause.
    assert "acct" not in guard_module._flags
    assert len(audit_recorder.entries) == 1
    action, details = audit_recorder.entries[0]
    assert action == "account.bio_policy_pause"
    assert details["account_id"] == "acct"
    assert details["email"] == "acct@example.com"
    assert details["flags"] == 5
    assert details["error_code"] == "bio_policy"


@pytest.mark.asyncio
async def test_burst_pause_keeps_window_when_pause_rejected():
    account = _make_account("acct")
    lb = _StubLoadBalancer(result=False)
    for _ in range(5):
        record_moderation_flag("acct", "bio_policy")

    assert await pause_account_for_moderation_burst(lb, account, error_code="bio_policy") is False
    # CAS miss / already paused: keep counting so a re-enabled burst trips again.
    assert len(guard_module._flags["acct"]) == 5


@pytest.mark.asyncio
async def test_burst_pause_never_raises(audit_recorder: _AuditRecorder):
    account = _make_account("acct")
    lb = _StubLoadBalancer(exc=RuntimeError("boom"))
    record_moderation_flag("acct", "bio_policy")

    assert await pause_account_for_moderation_burst(lb, account, error_code="bio_policy") is False
    assert audit_recorder.entries == []


# --- LoadBalancer.mark_bio_policy_pause ---------------------------------------


class _PauseHarness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, account: Account) -> None:
        self.account = account
        self.cas_calls: list[dict[str, Any]] = []
        self.routing_unavailable: list[str] = []
        self.invalidate_calls = 0

        accounts_repo = MagicMock()
        accounts_repo.update_status_if_current = AsyncMock(side_effect=self._cas)
        repos = MagicMock()
        repos.accounts = accounts_repo
        repos.__aenter__ = AsyncMock(return_value=repos)
        repos.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr(
            lb_module,
            "mark_account_routing_unavailable",
            lambda account_id: self.routing_unavailable.append(account_id),
        )

        self.balancer = LoadBalancer(repo_factory=lambda: repos)
        self.balancer._selection_inputs_cache = SimpleNamespace(invalidate=self._invalidate)

    def _cas(self, *args: Any, **kwargs: Any) -> bool:
        self.cas_calls.append({"args": args, "kwargs": kwargs})
        return self._cas_result

    _cas_result = True

    def _invalidate(self) -> None:
        self.invalidate_calls += 1


@pytest.mark.asyncio
async def test_mark_bio_policy_pause_persists_paused_status(monkeypatch: pytest.MonkeyPatch):
    harness = _PauseHarness(monkeypatch, _make_account("acct"))
    runtime = harness.balancer._runtime.setdefault("acct", lb_module.RuntimeState())
    runtime.reset_at = 12345.0
    runtime.blocked_at = 67890.0

    paused = await harness.balancer.mark_bio_policy_pause(harness.account, "review needed")

    assert paused is True
    assert len(harness.cas_calls) == 1
    call = harness.cas_calls[0]
    assert call["args"][0] == "acct"
    assert call["args"][1] is AccountStatus.PAUSED
    assert call["args"][2] == "review needed"
    assert call["args"][3] is None  # reset_at cleared
    assert call["kwargs"]["expected_status"] is AccountStatus.ACTIVE
    assert harness.routing_unavailable == ["acct"]
    assert harness.invalidate_calls == 1
    # In-memory account object reflects the pause for later CAS expectations.
    assert harness.account.status is AccountStatus.PAUSED
    assert harness.account.deactivation_reason == "review needed"
    # Runtime cooldown/block state is cleared alongside the persisted fields.
    assert runtime.reset_at is None
    assert runtime.blocked_at is None


@pytest.mark.asyncio
async def test_mark_bio_policy_pause_skips_already_unavailable(monkeypatch: pytest.MonkeyPatch):
    for status in (AccountStatus.PAUSED, AccountStatus.REAUTH_REQUIRED, AccountStatus.DEACTIVATED):
        harness = _PauseHarness(monkeypatch, _make_account("acct", status=status))
        assert await harness.balancer.mark_bio_policy_pause(harness.account, "review needed") is False
        assert harness.cas_calls == []
        assert harness.routing_unavailable == []


@pytest.mark.asyncio
async def test_mark_bio_policy_pause_cas_miss_does_not_mark_routing(monkeypatch: pytest.MonkeyPatch):
    harness = _PauseHarness(monkeypatch, _make_account("acct"))
    harness._cas_result = False

    assert await harness.balancer.mark_bio_policy_pause(harness.account, "review needed") is False
    assert len(harness.cas_calls) == 1
    assert harness.routing_unavailable == []
    assert harness.invalidate_calls == 1
