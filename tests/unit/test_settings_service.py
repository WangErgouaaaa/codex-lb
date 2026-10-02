from __future__ import annotations

import json
from collections.abc import Callable
from typing import cast

import pytest

import app.modules.settings.service as settings_service_module
from app.db.models import DashboardSettings
from app.modules.settings.repository import SettingsRepository
from app.modules.settings.schemas import PlanCapPair
from app.modules.settings.service import (
    DashboardSettingsUpdateData,
    SettingsService,
    _dump_additional_quota_routing_policies,
    _parse_additional_quota_routing_policies,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_migrated_null_account_caps_inherit_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    row = DashboardSettings()
    row.proxy_account_response_create_limit = None
    row.proxy_account_stream_limit = None
    row.proxy_account_stream_recovery_reserve = None

    class _Repository:
        async def get_or_create(self) -> DashboardSettings:
            return row

    monkeypatch.setattr(
        settings_service_module,
        "get_settings",
        lambda: type(
            "_StartupSettings",
            (),
            {
                "proxy_account_response_create_limit": 24,
                "proxy_account_stream_limit": 32,
                "proxy_account_stream_recovery_reserve": 4,
                "request_log_retention_days": 0,
                "usage_history_retention_days": 0,
            },
        )(),
    )

    settings = await SettingsService(cast(SettingsRepository, _Repository())).get_settings()

    assert settings.proxy_account_response_create_limit == 24
    assert settings.proxy_account_stream_limit == 32
    assert settings.proxy_account_stream_recovery_reserve == 4


@pytest.mark.asyncio
async def test_null_retention_inherits_environment_and_dashboard_value_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = DashboardSettings()

    class _Repository:
        async def get_or_create(self) -> DashboardSettings:
            return row

    monkeypatch.setattr(
        settings_service_module,
        "get_settings",
        lambda: type(
            "_StartupSettings",
            (),
            {
                "proxy_account_response_create_limit": 24,
                "proxy_account_stream_limit": 32,
                "proxy_account_stream_recovery_reserve": 4,
                "request_log_retention_days": 90,
                "usage_history_retention_days": 45,
            },
        )(),
    )
    service = SettingsService(cast(SettingsRepository, _Repository()))

    # NULL dashboard values inherit the deprecated env alias; the raw
    # overrides stay exposed as None (= inherit).
    settings = await service.get_settings()
    assert settings.request_log_retention_days == 90
    assert settings.usage_history_retention_days == 45
    assert settings.request_log_retention_override_days is None
    assert settings.usage_history_retention_override_days is None

    # Non-NULL dashboard values win, including 0 (explicitly disabled).
    row.request_log_retention_days = 30
    row.usage_history_retention_days = 0
    settings = await service.get_settings()
    assert settings.request_log_retention_days == 30
    assert settings.usage_history_retention_days == 0
    assert settings.request_log_retention_override_days == 30
    assert settings.usage_history_retention_override_days == 0


def test_parse_additional_quota_routing_policies_normalizes_aliases_and_policy_case() -> None:
    raw = json.dumps(
        {
            "codex-spark": "burn_first",
            "codex_spark": " preserve ",
            "gpt-5.3-codex-spark": "normal",
            "other": "legacy",
            123: "preserve",
        }
    )

    parsed = _parse_additional_quota_routing_policies(raw)
    assert parsed == {
        "codex_spark": "normal",
    }


def test_parse_additional_quota_routing_policies_handles_invalid_json() -> None:
    assert _parse_additional_quota_routing_policies(None) == {}
    assert _parse_additional_quota_routing_policies("not-json") == {}


def test_dump_additional_quota_routing_policies_canonicalizes_keys_and_filters_invalid() -> None:
    dumped = _dump_additional_quota_routing_policies(
        {
            "codex-spark": "normal",
            "codex_spark": "preserve",
            "  gpt-5.3-codex-spark  ": "burn_first",
            "bad-key": "normal",
        }
    )
    assert json.loads(dumped) == {"codex_spark": "burn_first"}


def _patch_startup_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings_service_module,
        "get_settings",
        lambda: type(
            "_StartupSettings",
            (),
            {
                "proxy_account_response_create_limit": 24,
                "proxy_account_stream_limit": 32,
                "proxy_account_stream_recovery_reserve": 4,
                "request_log_retention_days": 0,
                "usage_history_retention_days": 0,
            },
        )(),
    )


def _update_data(**overrides: object) -> DashboardSettingsUpdateData:
    values: dict[str, object] = {
        "sticky_threads_enabled": True,
        "upstream_stream_transport": "default",
        "prohibit_fast_mode": False,
        "http_downstream_transport_policy": "smart",
        "proxy_account_response_create_limit": None,
        "proxy_account_stream_limit": None,
        "proxy_account_stream_recovery_reserve": None,
        "upstream_proxy_routing_enabled": False,
        "upstream_proxy_default_pool_id": None,
        "prefer_earlier_reset_accounts": True,
        "prefer_earlier_reset_window": "secondary",
        "show_reset_credit_badges": True,
        "auto_redeem_reset_credits_before_expiry": False,
        "show_reset_credit_expiry_badge": True,
        "routing_strategy": "capacity_weighted",
        "relative_availability_power": 2.0,
        "relative_availability_top_k": 5,
        "single_account_id": None,
        "openai_cache_affinity_max_age_seconds": 1800,
        "dashboard_session_ttl_seconds": 31536000,
        "http_responses_session_bridge_prompt_cache_idle_ttl_seconds": 3600,
        "http_responses_session_bridge_gateway_safe_mode": False,
        "sticky_reallocation_budget_threshold_pct": 95.0,
        "sticky_reallocation_primary_budget_threshold_pct": 95.0,
        "sticky_reallocation_secondary_budget_threshold_pct": 100.0,
        "additional_quota_routing_policies": {},
        "warmup_model": "auto",
        "import_without_overwrite": True,
        "totp_required_on_login": False,
        "api_key_auth_enabled": False,
        "hide_upstream_quota_from_api_keys": False,
        "limit_warmup_enabled": False,
        "limit_warmup_windows": "both",
        "limit_warmup_model": "auto",
        "limit_warmup_prompt": "Say OK.",
        "limit_warmup_cooldown_seconds": 3600,
        "limit_warmup_exhausted_threshold_percent": 99.0,
        "limit_warmup_idle_threshold_percent": 1.0,
        "limit_warmup_min_available_percent": 100.0,
        "weekly_pace_working_days": "0,1,2,3,4,5,6",
        "weekly_pace_smoothing_minutes": 30,
        "guest_access_enabled": False,
        "limit_warmup_staggered_idle_enabled": False,
        "request_log_retention_override_days": None,
        "usage_history_retention_override_days": None,
        "clear_request_log_retention_override": False,
        "clear_usage_history_retention_override": False,
        "proxy_account_plan_concurrency_caps": None,
    }
    values.update(overrides)
    return DashboardSettingsUpdateData(**values)


@pytest.mark.asyncio
async def test_plan_concurrency_caps_row_json_parses_into_settings_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = DashboardSettings()
    row.proxy_account_plan_concurrency_caps_json = json.dumps(
        {"plus": {"responseCreate": 4, "stream": 6}, "pro": {"responseCreate": 8, "stream": 12}}
    )

    class _Repository:
        async def get_or_create(self) -> DashboardSettings:
            return row

    _patch_startup_settings(monkeypatch)

    settings = await SettingsService(cast(SettingsRepository, _Repository())).get_settings()

    assert settings.proxy_account_plan_concurrency_caps == {"plus": (4, 6), "pro": (8, 12)}

    # Malformed row JSON degrades to the global caps instead of failing reads.
    row.proxy_account_plan_concurrency_caps_json = "not-json"
    settings = await SettingsService(cast(SettingsRepository, _Repository())).get_settings()
    assert settings.proxy_account_plan_concurrency_caps == {}


@pytest.mark.asyncio
async def test_plan_concurrency_caps_update_serializes_and_round_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = DashboardSettings()
    captured: dict[str, object] = {}

    class _Repository:
        async def get_or_create(self) -> DashboardSettings:
            return row

        async def update(self, **kwargs: object) -> DashboardSettings:
            captured.update(kwargs)
            plan_caps_json = kwargs["proxy_account_plan_concurrency_caps_json"]
            if plan_caps_json is not None:
                row.proxy_account_plan_concurrency_caps_json = str(plan_caps_json)
            return row

        async def commit_refresh(
            self,
            settings: DashboardSettings,
            *,
            on_committed: Callable[[], None] | None = None,
        ) -> None:
            return None

    _patch_startup_settings(monkeypatch)
    service = SettingsService(cast(SettingsRepository, _Repository()))

    settings = await service.update_settings(
        _update_data(proxy_account_plan_concurrency_caps={"plus": PlanCapPair(response_create=4, stream=6)})
    )

    assert captured["proxy_account_plan_concurrency_caps_json"] == '{"plus":{"responseCreate":4,"stream":6}}'
    # The returned dataclass re-parses the persisted column JSON.
    assert settings.proxy_account_plan_concurrency_caps == {"plus": (4, 6)}

    # An absent field passes None through: the column stays untouched.
    captured.clear()
    settings = await service.update_settings(_update_data())
    assert captured["proxy_account_plan_concurrency_caps_json"] is None
    assert settings.proxy_account_plan_concurrency_caps == {"plus": (4, 6)}
