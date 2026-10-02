from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest

import app.modules.proxy.load_balancer as load_balancer_module
from app.core.balancer.logic import AccountState
from app.core.crypto import TokenEncryptor
from app.db.models import Account, AccountStatus, StickySessionKind
from app.modules.api_keys.repository import ApiKeysRepository
from app.modules.proxy._load_balancer.sticky_selection import (
    _account_cap_error_message,
    _filter_states_for_account_caps,
)
from app.modules.proxy._load_balancer.types import AccountConcurrencyCaps
from app.modules.proxy.cap_partitioning import CapPartition
from app.modules.proxy.load_balancer import LoadBalancer, effective_account_concurrency_caps
from app.modules.proxy.repo_bundle import ProxyRepositories
from app.modules.request_logs.repository import RequestLogsRepository
from app.modules.usage.repository import AdditionalUsageRepository

pytestmark = pytest.mark.unit

# Global caps 8/16 with a plus override of 2/3 keep the numbers small enough to
# saturate in tests while leaving pro (and unconfigured plans) on the globals.
_GLOBAL_RESPONSE_CREATE_LIMIT = 8
_GLOBAL_STREAM_LIMIT = 16
_PLUS_OVERRIDE = (2, 3)
_PLUS_OVERRIDES_JSON = '{"plus":{"responseCreate":2,"stream":3}}'


@pytest.fixture(autouse=True)
def _use_dashboard_caps_from_test_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    class _SettingsCache:
        async def get(self) -> object:
            return load_balancer_module.get_settings()

    monkeypatch.setattr(load_balancer_module, "get_settings_cache", lambda: _SettingsCache())


def _make_account(account_id: str, *, plan_type: str = "plus") -> Account:
    encryptor = TokenEncryptor()
    return Account(
        id=account_id,
        chatgpt_account_id=f"workspace-{account_id}",
        email=f"{account_id}@example.com",
        plan_type=plan_type,
        access_token_encrypted=encryptor.encrypt("access"),
        refresh_token_encrypted=encryptor.encrypt("refresh"),
        id_token_encrypted=encryptor.encrypt("id"),
        last_refresh=datetime.now(tz=timezone.utc),
        status=AccountStatus.ACTIVE,
        deactivation_reason=None,
    )


def _plan_caps() -> AccountConcurrencyCaps:
    return AccountConcurrencyCaps(
        response_create_limit=_GLOBAL_RESPONSE_CREATE_LIMIT,
        stream_limit=_GLOBAL_STREAM_LIMIT,
        plan_overrides={"plus": _PLUS_OVERRIDE},
    )


class _StubAccountsRepository:
    def __init__(self, accounts: list[Account]) -> None:
        self._accounts = accounts

    async def list_accounts(self) -> list[Account]:
        return list(self._accounts)

    async def get_by_id(self, account_id: str) -> Account | None:
        return next((account for account in self._accounts if account.id == account_id), None)

    async def update_status(self, *args: Any, **kwargs: Any) -> bool:
        del args, kwargs
        return True

    async def update_status_if_current(self, *args: Any, **kwargs: Any) -> bool:
        del args, kwargs
        return True


class _StubUsageRepository:
    def __init__(
        self,
        primary: dict[str, Any],
        secondary: dict[str, Any],
        monthly: dict[str, Any] | None = None,
    ) -> None:
        self._primary = primary
        self._secondary = secondary
        self._monthly = monthly or {}

    async def latest_by_account(
        self,
        window: str | None = None,
        *,
        account_ids: Collection[str] | None = None,
    ) -> dict[str, Any]:
        del account_ids
        if window == "secondary":
            return self._secondary
        if window == "monthly":
            return self._monthly
        return self._primary

    async def latest_entry_for_account(
        self,
        account_id: str,
        *,
        window: str | None = None,
    ) -> Any:
        if window == "secondary":
            return self._secondary.get(account_id)
        if window == "monthly":
            return self._monthly.get(account_id)
        return self._primary.get(account_id)


class _StubStickySessionsRepository:
    def __init__(self) -> None:
        self.account_id: str | None = None
        self.deleted: list[tuple[str, StickySessionKind | None]] = []
        self.upserts: list[tuple[str, str, StickySessionKind | None]] = []

    async def get_account_id(self, *args: Any, **kwargs: Any) -> str | None:
        del args, kwargs
        return self.account_id

    async def upsert(self, *args: Any, **kwargs: Any) -> Any:
        sticky_key = cast(str, args[0])
        account_id = cast(str, args[1])
        self.account_id = account_id
        self.upserts.append((sticky_key, account_id, kwargs.get("kind")))
        return None

    async def delete(self, *args: Any, **kwargs: Any) -> bool:
        sticky_key = cast(str, args[0])
        self.deleted.append((sticky_key, kwargs.get("kind")))
        self.account_id = None
        return True

    async def restore_if_current(
        self,
        key: str,
        *,
        kind: StickySessionKind,
        expected_account_id: str | None,
        restore_account_id: str | None,
    ) -> bool:
        if self.account_id != expected_account_id:
            return False
        if restore_account_id is None:
            self.deleted.append((key, kind))
            self.account_id = None
            return True
        self.upserts.append((key, restore_account_id, kind))
        self.account_id = restore_account_id
        return True


@asynccontextmanager
async def _repo_factory(
    accounts_repo: _StubAccountsRepository,
    usage_repo: _StubUsageRepository,
    sticky_repo: _StubStickySessionsRepository | None = None,
) -> AsyncIterator[ProxyRepositories]:
    sticky_repo = sticky_repo or _StubStickySessionsRepository()
    yield ProxyRepositories(
        accounts=cast(Any, accounts_repo),
        usage=cast(Any, usage_repo),
        request_logs=cast(RequestLogsRepository, object()),
        sticky_sessions=cast(Any, sticky_repo),
        api_keys=cast(ApiKeysRepository, object()),
        additional_usage=cast(AdditionalUsageRepository, object()),
    )


def test_effective_caps_parse_dashboard_json_into_plan_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        load_balancer_module,
        "get_settings",
        lambda: SimpleNamespace(
            proxy_account_response_create_limit=_GLOBAL_RESPONSE_CREATE_LIMIT,
            proxy_account_stream_limit=_GLOBAL_STREAM_LIMIT,
            proxy_account_caps_scope="partitioned",
        ),
    )
    dashboard = SimpleNamespace(proxy_account_plan_concurrency_caps_json=_PLUS_OVERRIDES_JSON)

    caps = effective_account_concurrency_caps(dashboard)

    # An empty (or missing) override map must stay ``None`` so existing caps
    # equality assertions are untouched; only a configured plan yields a dict.
    assert caps.plan_overrides == {"plus": _PLUS_OVERRIDE}
    assert caps.caps_for_plan("plus") == _PLUS_OVERRIDE
    assert caps.caps_for_plan("pro") == (_GLOBAL_RESPONSE_CREATE_LIMIT, _GLOBAL_STREAM_LIMIT)
    assert effective_account_concurrency_caps(
        SimpleNamespace(proxy_account_plan_concurrency_caps_json="{}")
    ).plan_overrides is None
    assert effective_account_concurrency_caps().plan_overrides is None


def test_effective_caps_ignore_plan_overrides_in_partitioned_mode(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        load_balancer_module,
        "get_settings",
        lambda: SimpleNamespace(
            proxy_account_response_create_limit=_GLOBAL_RESPONSE_CREATE_LIMIT,
            proxy_account_stream_limit=_GLOBAL_STREAM_LIMIT,
            proxy_account_caps_scope="partitioned",
        ),
    )
    monkeypatch.setattr(
        load_balancer_module,
        "get_cap_partition",
        lambda: CapPartition(replica_count=2, rank=0),
    )
    monkeypatch.setattr(load_balancer_module, "_PLAN_CAPS_PARTITION_SKIP_WARNED", False)
    dashboard = SimpleNamespace(proxy_account_plan_concurrency_caps_json=_PLUS_OVERRIDES_JSON)

    with caplog.at_level(logging.WARNING, logger="app.modules.proxy.load_balancer"):
        caps = effective_account_concurrency_caps(dashboard)
        again = effective_account_concurrency_caps(dashboard)

    assert caps.plan_overrides is None
    assert caps.response_create_limit == _GLOBAL_RESPONSE_CREATE_LIMIT // 2
    assert caps.stream_limit == _GLOBAL_STREAM_LIMIT // 2
    assert caps.configured_response_create_limit == _GLOBAL_RESPONSE_CREATE_LIMIT
    assert caps.configured_stream_limit == _GLOBAL_STREAM_LIMIT
    assert caps.replica_count == 2
    plan_skip_warnings = [
        record for record in caplog.records if "Plan-scoped account concurrency caps are ignored" in record.message
    ]
    assert len(plan_skip_warnings) == 1
    assert again.plan_overrides is None


def test_filter_states_for_account_caps_separates_plan_limits() -> None:
    caps = _plan_caps()
    plus_state = AccountState(
        account_id="acc-plan-plus",
        status=AccountStatus.ACTIVE,
        plan_type="plus",
        inflight_response_creates=2,
        inflight_streams=2,
    )
    pro_state = AccountState(
        account_id="acc-plan-pro",
        status=AccountStatus.ACTIVE,
        plan_type="pro",
        inflight_response_creates=2,
        inflight_streams=2,
    )

    response_create_filtered = _filter_states_for_account_caps(
        [plus_state, pro_state],
        lease_kind="response_create",
        caps=caps,
    )
    assert [state.account_id for state in response_create_filtered] == ["acc-plan-pro"]

    # The stream reserve deducts from each plan's own stream limit: the plus
    # share of max(1, 3 - 1) saturates at 2 while pro still admits.
    stream_filtered = _filter_states_for_account_caps(
        [plus_state, pro_state],
        lease_kind="stream",
        caps=caps,
        stream_reserve_slots=1,
    )
    assert [state.account_id for state in stream_filtered] == ["acc-plan-pro"]

    unreserved_stream_filtered = _filter_states_for_account_caps(
        [plus_state, pro_state],
        lease_kind="stream",
        caps=caps,
    )
    assert [state.account_id for state in unreserved_stream_filtered] == ["acc-plan-plus", "acc-plan-pro"]


@pytest.mark.asyncio
async def test_acquire_account_lease_rejects_beyond_plan_caps() -> None:
    balancer = LoadBalancer(lambda: _repo_factory(_StubAccountsRepository([]), _StubUsageRepository({}, {})))
    caps = _plan_caps()

    plus_create_leases = [
        await balancer.acquire_account_lease(
            "acc-plan-plus",
            kind="response_create",
            concurrency_caps=caps,
            plan_type="plus",
        )
        for _ in range(_PLUS_OVERRIDE[0])
    ]
    assert all(lease is not None for lease in plus_create_leases)
    assert await balancer.acquire_account_lease(
        "acc-plan-plus",
        kind="response_create",
        concurrency_caps=caps,
        plan_type="plus",
    ) is None

    plus_stream_leases = [
        await balancer.acquire_account_lease("acc-plan-plus", kind="stream", concurrency_caps=caps, plan_type="plus")
        for _ in range(_PLUS_OVERRIDE[1])
    ]
    assert all(lease is not None for lease in plus_stream_leases)
    assert await balancer.acquire_account_lease(
        "acc-plan-plus",
        kind="stream",
        concurrency_caps=caps,
        plan_type="plus",
    ) is None

    # The same inflight counts stay admissible for a plan without an override:
    # it resolves to the global 8/16 caps.
    pro_create_leases = [
        await balancer.acquire_account_lease(
            "acc-plan-pro",
            kind="response_create",
            concurrency_caps=caps,
            plan_type="pro",
        )
        for _ in range(_PLUS_OVERRIDE[0])
    ]
    assert all(lease is not None for lease in pro_create_leases)
    pro_stream_leases = [
        await balancer.acquire_account_lease("acc-plan-pro", kind="stream", concurrency_caps=caps, plan_type="pro")
        for _ in range(_PLUS_OVERRIDE[1])
    ]
    assert all(lease is not None for lease in pro_stream_leases)


@pytest.mark.asyncio
async def test_account_lease_allowed_locked_applies_plan_stream_reserve() -> None:
    # This helper is the hard-sticky final admission gate's checkpoint
    # (sticky_selection.py calls it with plan_type=selected.plan_type).
    balancer = LoadBalancer(lambda: _repo_factory(_StubAccountsRepository([]), _StubUsageRepository({}, {})))
    caps = _plan_caps()
    first = await balancer.acquire_account_lease(
        "acc-plan-reserve",
        kind="stream",
        concurrency_caps=caps,
        plan_type="plus",
    )
    assert first is not None

    # plus stream cap 3 with reserve 2 leaves an effective share of max(1, 3-2)=1,
    # already consumed by the lease above.
    assert not balancer._account_lease_allowed_locked(
        "acc-plan-reserve",
        kind="stream",
        caps=caps,
        stream_reserve_slots=2,
        plan_type="plus",
    )
    assert balancer._account_lease_allowed_locked(
        "acc-plan-reserve",
        kind="stream",
        caps=caps,
        stream_reserve_slots=2,
        plan_type="pro",
    )
    # Reattach passes stream_reserve_slots=0 and keeps its bypass semantics.
    assert balancer._account_lease_allowed_locked(
        "acc-plan-reserve",
        kind="stream",
        caps=caps,
        stream_reserve_slots=0,
        plan_type="plus",
    )


@pytest.mark.asyncio
async def test_hard_sticky_final_gate_rejects_beyond_plan_stream_cap() -> None:
    account = _make_account("acc-plan-hard-sticky", plan_type="plus")
    sticky_repo = _StubStickySessionsRepository()
    sticky_repo.account_id = account.id
    balancer = LoadBalancer(
        lambda: _repo_factory(_StubAccountsRepository([account]), _StubUsageRepository({}, {}), sticky_repo)
    )
    caps = _plan_caps()
    saturating = [await balancer.acquire_account_lease(account.id, kind="stream") for _ in range(_PLUS_OVERRIDE[1])]
    assert all(lease is not None for lease in saturating)

    result = await balancer.select_account(
        sticky_key="plan-hard-sticky",
        sticky_kind=StickySessionKind.CODEX_SESSION,
        lease_kind="stream",
        routing_strategy="usage_weighted",
        concurrency_caps=caps,
    )

    # Hard-sticky ownership bypasses the candidate filter, so the final
    # admission gate is the only cap checkpoint. It must resolve the caps from
    # the selected account's plan: 3 inflight streams saturate the plus plan
    # stream cap of 3 while the global stream cap (16) would still admit.
    assert result.account is None
    assert result.error_code == "account_stream_cap"
    assert "per-account limit is 3" in (result.error_message or "")


@pytest.mark.asyncio
async def test_hard_sticky_final_gate_admits_within_plan_stream_cap() -> None:
    account = _make_account("acc-plan-hard-sticky-pro", plan_type="pro")
    sticky_repo = _StubStickySessionsRepository()
    sticky_repo.account_id = account.id
    balancer = LoadBalancer(
        lambda: _repo_factory(_StubAccountsRepository([account]), _StubUsageRepository({}, {}), sticky_repo)
    )
    caps = _plan_caps()
    saturating = [await balancer.acquire_account_lease(account.id, kind="stream") for _ in range(_PLUS_OVERRIDE[1])]
    assert all(lease is not None for lease in saturating)

    result = await balancer.select_account(
        sticky_key="plan-hard-sticky-pro",
        sticky_kind=StickySessionKind.CODEX_SESSION,
        lease_kind="stream",
        routing_strategy="usage_weighted",
        concurrency_caps=caps,
    )

    assert result.account is not None
    assert result.error_code is None


def test_account_cap_error_message_uses_plan_effective_numbers() -> None:
    caps = _plan_caps()

    plus_stream = _account_cap_error_message("stream", caps, plan_type="plus")
    assert "per-account limit is 3" in plus_stream
    plus_create = _account_cap_error_message("response_create", caps, plan_type="plus")
    assert "per-account limit is 2" in plus_create

    global_stream = _account_cap_error_message("stream", caps)
    assert f"per-account limit is {_GLOBAL_STREAM_LIMIT}" in global_stream
    global_create = _account_cap_error_message("response_create", caps)
    assert f"per-account limit is {_GLOBAL_RESPONSE_CREATE_LIMIT}" in global_create
