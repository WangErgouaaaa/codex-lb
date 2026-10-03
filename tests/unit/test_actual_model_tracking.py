"""Unit tests for the upstream actual_model tracking feature.

Covers:
- the two extraction helpers (`_upstream_model_from_event_payload`,
  `_upstream_model_from_response`);
- the streaming path persisting `actual_model` on the request log;
- the compact path persisting `actual_model` on the request log;
- the request-log API mapper exposing `actual_model`;
- the Alembic migration adding the `request_logs.actual_model` column.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, inspect

import app.modules.proxy.load_balancer as load_balancer_module
import app.modules.proxy.service as proxy_service
from app.core.crypto import TokenEncryptor
from app.core.openai.models import CompactResponsePayload, OpenAIResponsePayload
from app.core.openai.requests import ResponsesCompactRequest, ResponsesRequest
from app.core.utils.time import utcnow
from app.db.migrate import run_upgrade
from app.db.migration_url import to_sync_database_url
from app.db.models import Account, AccountStatus, RequestLog
from app.modules.accounts.repository import AccountsRepository
from app.modules.api_keys.repository import ApiKeysRepository
from app.modules.proxy.load_balancer import AccountSelection
from app.modules.proxy.repo_bundle import ProxyRepositories
from app.modules.proxy.sticky_repository import StickySessionsRepository
from app.modules.request_logs.mappers import to_request_log_entry
from app.modules.request_logs.repository import PreviousResponseOwnerRecord, RequestLogsRepository
from app.modules.usage.repository import AdditionalUsageRepository, UsageRepository

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _share_proxy_dashboard_caps_with_load_balancer(monkeypatch: pytest.MonkeyPatch) -> None:
    original_settings_cache_factory = proxy_service.get_settings_cache

    class _SettingsCache:
        async def get(self) -> object:
            if proxy_service.get_settings_cache is original_settings_cache_factory:
                return proxy_service.get_settings()
            return await proxy_service.get_settings_cache().get()

    monkeypatch.setattr(load_balancer_module, "get_settings_cache", lambda: _SettingsCache())


class _SettingsCache:
    def __init__(self, settings: object) -> None:
        self._settings = settings

    async def get(self) -> object:
        return self._settings


class _RequestLogsRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.response_owner_by_id: dict[tuple[str, str | None, str | None], str] = {}
        self.latest_response_by_session: dict[tuple[str, str | None], str] = {}
        self.lookup_calls: list[tuple[str, str | None, str | None]] = []
        self.session_lookup_calls: list[tuple[str, str | None]] = []
        self.lookup_error: Exception | None = None

    async def add_log(self, **kwargs: object) -> None:
        self.calls.append(dict(kwargs))

    async def find_latest_owner_record_for_response_id(
        self,
        *,
        response_id: str,
        api_key_id: str | None,
        session_id: str | None = None,
    ) -> PreviousResponseOwnerRecord | None:
        key = (response_id, api_key_id, session_id)
        self.lookup_calls.append(key)
        if self.lookup_error is not None:
            raise self.lookup_error
        owner = self.response_owner_by_id.get(key)
        if owner is not None:
            return PreviousResponseOwnerRecord(
                account_id=owner,
                requested_at=None,
                session_id=session_id,
            )
        if session_id is not None:
            fallback_owner = self.response_owner_by_id.get((response_id, api_key_id, None))
            if fallback_owner is not None:
                return PreviousResponseOwnerRecord(
                    account_id=fallback_owner,
                    requested_at=None,
                    session_id=None,
                )
        return None

    async def find_latest_account_id_for_response_id(
        self,
        *,
        response_id: str,
        api_key_id: str | None,
        session_id: str | None = None,
    ) -> str | None:
        owner = await self.find_latest_owner_record_for_response_id(
            response_id=response_id,
            api_key_id=api_key_id,
            session_id=session_id,
        )
        return owner.account_id if owner is not None else None

    async def find_latest_response_id_for_session_id(
        self,
        *,
        session_id: str,
        api_key_id: str | None,
    ) -> str | None:
        key = (session_id, api_key_id)
        self.session_lookup_calls.append(key)
        response_id = self.latest_response_by_session.get(key)
        if response_id is not None:
            return response_id
        if api_key_id is not None:
            return self.latest_response_by_session.get((session_id, None))
        return None


class _RepoContext:
    def __init__(self, request_logs: _RequestLogsRecorder) -> None:
        self._repos = ProxyRepositories(
            accounts=cast(AccountsRepository, AsyncMock()),
            usage=cast(UsageRepository, AsyncMock()),
            request_logs=cast(RequestLogsRepository, request_logs),
            sticky_sessions=cast(StickySessionsRepository, AsyncMock()),
            api_keys=cast(ApiKeysRepository, AsyncMock()),
            additional_usage=cast(AdditionalUsageRepository, AsyncMock()),
        )

    async def __aenter__(self) -> ProxyRepositories:
        return self._repos

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


def _repo_factory(request_logs: _RequestLogsRecorder) -> proxy_service.ProxyRepoFactory:
    def factory() -> _RepoContext:
        return _RepoContext(request_logs)

    return factory


def _make_proxy_settings(*, trace_channels: frozenset[str] = frozenset()) -> SimpleNamespace:
    return SimpleNamespace(
        prefer_earlier_reset_accounts=False,
        prefer_earlier_reset_window="secondary",
        sticky_threads_enabled=False,
        sticky_reallocation_budget_threshold_pct=95.0,
        upstream_stream_transport="default",
        openai_cache_affinity_max_age_seconds=300,
        openai_prompt_cache_key_derivation_enabled=True,
        routing_strategy="usage_weighted",
        proxy_request_budget_seconds=75.0,
        compact_request_budget_seconds=75.0,
        transcription_request_budget_seconds=120.0,
        upstream_compact_timeout_seconds=None,
        http_responses_session_bridge_gateway_safe_mode=False,
        trace_channels=trace_channels,
        proxy_token_refresh_limit=32,
        proxy_upstream_websocket_connect_limit=64,
        proxy_account_response_create_limit=4,
        proxy_account_stream_limit=8,
        proxy_account_stream_recovery_reserve=1,
        proxy_response_create_limit=64,
        proxy_compact_response_create_limit=16,
        proxy_admission_wait_timeout_seconds=10.0,
        max_sse_event_bytes=16 * 1024 * 1024,
        http_responses_session_bridge_instance_id="test-instance",
        http_responses_session_bridge_instance_ring=[],
        http_downstream_transport_policy="smart",
    )


def _make_account(account_id: str) -> Account:
    encryptor = TokenEncryptor()
    now = utcnow()
    return Account(
        id=account_id,
        chatgpt_account_id=account_id,
        email=f"{account_id}@example.com",
        plan_type="plus",
        access_token_encrypted=encryptor.encrypt("access-token"),
        refresh_token_encrypted=encryptor.encrypt("refresh-token"),
        id_token_encrypted=encryptor.encrypt("id-token"),
        last_refresh=now,
        status=AccountStatus.ACTIVE,
        deactivation_reason=None,
    )


def _db_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------


def test_upstream_model_from_event_payload_extracts_model_string():
    payload = {"type": "response.completed", "response": {"id": "resp_1", "model": "gpt-5.1-snapshot"}}

    assert proxy_service._upstream_model_from_event_payload(payload) == "gpt-5.1-snapshot"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "not-a-dict",
        {"type": "response.completed"},
        {"type": "response.completed", "response": "not-a-dict"},
        {"type": "response.completed", "response": {"id": "resp_1"}},
        {"type": "response.completed", "response": {"id": "resp_1", "model": 42}},
        {"type": "response.completed", "response": {"id": "resp_1", "model": None}},
        {"type": "response.completed", "response": {"id": "resp_1", "model": ""}},
    ],
)
def test_upstream_model_from_event_payload_returns_none(payload):
    assert proxy_service._upstream_model_from_event_payload(payload) is None


def test_upstream_model_from_response_reads_model_extra_from_compact_payload():
    response = CompactResponsePayload.model_validate(
        {"object": "response.compaction", "model": "gpt-5.1-compact-snapshot"}
    )

    assert response.model_extra is not None
    assert proxy_service._upstream_model_from_response(response) == "gpt-5.1-compact-snapshot"


def test_upstream_model_from_response_reads_model_extra_from_openai_payload():
    response = OpenAIResponsePayload.model_validate({"id": "resp_1", "model": "gpt-5.1-snapshot"})

    assert proxy_service._upstream_model_from_response(response) == "gpt-5.1-snapshot"


@pytest.mark.parametrize(
    "response",
    [
        None,
        # Declared fields only: no model_extra mapping at all.
        CompactResponsePayload.model_validate({"object": "response.compaction"}),
        OpenAIResponsePayload.model_validate({}),
        # model_extra exists but "model" is missing / not a string / empty.
        CompactResponsePayload.model_validate({"object": "response.compaction", "output": []}),
        CompactResponsePayload.model_validate({"object": "response.compaction", "model": 42}),
        CompactResponsePayload.model_validate({"object": "response.compaction", "model": None}),
        CompactResponsePayload.model_validate({"object": "response.compaction", "model": ""}),
    ],
)
def test_upstream_model_from_response_returns_none(response):
    assert proxy_service._upstream_model_from_response(response) is None


# ---------------------------------------------------------------------------
# Streaming path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_responses_logs_actual_model_from_upstream_snapshot(monkeypatch):
    settings = _make_proxy_settings()
    request_logs = _RequestLogsRecorder()
    service = proxy_service.ProxyService(_repo_factory(request_logs))
    account = _make_account("acc_actual_model_stream")

    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: _SettingsCache(settings))
    monkeypatch.setattr(proxy_service, "get_settings", lambda: settings)
    monkeypatch.setattr(
        service._load_balancer,
        "select_account",
        AsyncMock(return_value=AccountSelection(account=account, error_message=None)),
    )
    monkeypatch.setattr(service, "_ensure_fresh", AsyncMock(return_value=account))
    monkeypatch.setattr(service, "_settle_stream_api_key_usage", AsyncMock(return_value=True))

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        yield (
            'data: {"type":"response.completed","response":{"id":"resp_actual_model_stream",'
            '"model":"gpt-5.1-snapshot"}}\n\n'
        )

    monkeypatch.setattr(proxy_service, "core_stream_responses", fake_stream)

    payload = ResponsesRequest.model_validate(
        {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    )

    chunks = [chunk async for chunk in service.stream_responses(payload, {"session_id": "sid-actual-model"})]

    assert chunks
    assert await service.drain_persistence_tasks(timeout_seconds=1)
    assert request_logs.calls
    assert request_logs.calls[0]["actual_model"] == "gpt-5.1-snapshot"


@pytest.mark.asyncio
async def test_stream_responses_logs_actual_model_none_when_upstream_omits_model(monkeypatch):
    settings = _make_proxy_settings()
    request_logs = _RequestLogsRecorder()
    service = proxy_service.ProxyService(_repo_factory(request_logs))
    account = _make_account("acc_actual_model_stream_missing")

    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: _SettingsCache(settings))
    monkeypatch.setattr(proxy_service, "get_settings", lambda: settings)
    monkeypatch.setattr(
        service._load_balancer,
        "select_account",
        AsyncMock(return_value=AccountSelection(account=account, error_message=None)),
    )
    monkeypatch.setattr(service, "_ensure_fresh", AsyncMock(return_value=account))
    monkeypatch.setattr(service, "_settle_stream_api_key_usage", AsyncMock(return_value=True))

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        yield 'data: {"type":"response.completed","response":{"id":"resp_actual_model_stream_missing"}}\n\n'

    monkeypatch.setattr(proxy_service, "core_stream_responses", fake_stream)

    payload = ResponsesRequest.model_validate(
        {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    )

    chunks = [chunk async for chunk in service.stream_responses(payload, {"session_id": "sid-actual-model"})]

    assert chunks
    assert await service.drain_persistence_tasks(timeout_seconds=1)
    assert request_logs.calls
    assert request_logs.calls[0]["actual_model"] is None


# ---------------------------------------------------------------------------
# Compact path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compact_responses_logs_actual_model_from_compact_payload(monkeypatch):
    settings = _make_proxy_settings()
    request_logs = _RequestLogsRecorder()
    service = proxy_service.ProxyService(_repo_factory(request_logs))
    account = _make_account("acc_actual_model_compact")

    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: _SettingsCache(settings))
    monkeypatch.setattr(proxy_service, "get_settings", lambda: settings)
    monkeypatch.setattr(
        service._load_balancer,
        "select_account",
        AsyncMock(return_value=AccountSelection(account=account, error_message=None)),
    )
    monkeypatch.setattr(service, "_ensure_fresh", AsyncMock(return_value=account))
    monkeypatch.setattr(service, "_settle_compact_api_key_usage", AsyncMock())

    async def fake_compact(payload, headers, access_token, account_id, *, chatgpt_account_id=None):
        del payload, headers, access_token
        return CompactResponsePayload.model_validate(
            {"object": "response.compaction", "output": [], "model": "gpt-5.1-compact-snapshot"}
        )

    monkeypatch.setattr(proxy_service, "core_compact_responses", fake_compact)

    payload = ResponsesCompactRequest.model_validate({"model": "gpt-5.1", "instructions": "hi", "input": []})

    result = await service.compact_responses(payload, {"session_id": "sid-compact-actual-model"})

    assert result.model_extra is not None
    assert result.model_extra["model"] == "gpt-5.1-compact-snapshot"
    assert await service.drain_persistence_tasks(timeout_seconds=1)
    assert request_logs.calls
    assert request_logs.calls[-1]["actual_model"] == "gpt-5.1-compact-snapshot"


# ---------------------------------------------------------------------------
# API mapper
# ---------------------------------------------------------------------------


def test_to_request_log_entry_exposes_actual_model():
    log = RequestLog(
        request_id="req_actual_model_mapper",
        request_kind="normal",
        requested_at=utcnow(),
        model="gpt-5.1",
        status="success",
        actual_model="gpt-5.1-snapshot",
    )

    entry = to_request_log_entry(log)

    assert entry.actual_model == "gpt-5.1-snapshot"


def test_to_request_log_entry_exposes_actual_model_none():
    log = RequestLog(
        request_id="req_actual_model_mapper_none",
        request_kind="normal",
        requested_at=utcnow(),
        model="gpt-5.1",
        status="success",
    )

    entry = to_request_log_entry(log)

    assert entry.actual_model is None


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_request_log_actual_model_column_exists_after_head_upgrade(tmp_path: Path) -> None:
    db_path = tmp_path / "request-log-actual-model.db"
    url = _db_url(db_path)

    run_upgrade(url, "head", bootstrap_legacy=False)

    sync_url = to_sync_database_url(url)
    with create_engine(sync_url, future=True).connect() as connection:
        request_log_columns = {column["name"] for column in inspect(connection).get_columns("request_logs")}

    assert "actual_model" in request_log_columns
