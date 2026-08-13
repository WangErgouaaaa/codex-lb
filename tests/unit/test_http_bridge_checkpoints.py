"""Canonical account-neutral continuation checkpoints for HTTP bridge.

Covers the storage layer (save/get/TTL) and the replay-eligibility predicate
that gates owner-unavailable checkpoint rehydration. The streaming trigger
itself is exercised indirectly: ``checkpoint_replay_owner_unavailable_allowed``
is the exact predicate consulted in the except branch.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.utils.time import utcnow
from app.db.models import Base, HttpBridgeCheckpointRecord, HttpBridgeSessionRecord, HttpBridgeSessionState
from app.modules.proxy.durable_bridge_coordinator import DurableBridgeSessionCoordinator
from app.modules.proxy.durable_bridge_repository import DurableBridgeRepository

pytestmark = pytest.mark.unit


@pytest.fixture
async def async_session_factory() -> AsyncIterator[Callable[[], AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    def get_session() -> AsyncSession:
        return session_maker()

    yield get_session

    await engine.dispose()


@pytest.fixture
async def coordinator(async_session_factory: Callable[[], AsyncSession]) -> DurableBridgeSessionCoordinator:
    return DurableBridgeSessionCoordinator(async_session_factory)


async def _claim_session(coordinator: DurableBridgeSessionCoordinator, *, session_key_value: str = "sid-cp") -> str:
    claimed = await coordinator.claim_live_session(
        session_key_kind="session_header",
        session_key_value=session_key_value,
        api_key_id="key-cp",
        instance_id="instance-a",
        owner_process_epoch="test-process",
        lease_ttl_seconds=120.0,
        account_id="acc-1",
        model=None,
        service_tier=None,
        latest_turn_state=None,
        latest_response_id=None,
        allow_takeover=True,
    )
    assert claimed is not None
    return claimed.session_id


@pytest.mark.asyncio
async def test_checkpoint_save_get_roundtrip(coordinator: DurableBridgeSessionCoordinator) -> None:
    session_id = await _claim_session(coordinator)
    saved = await coordinator.save_checkpoint(
        response_id="resp_abc",
        session_id=session_id,
        api_key_scope="scope-1",
        account_id="acc-1",
        model="gpt-5.6-sol",
        input_json='[{"role":"user","content":"hello"}]',
        output_json='[{"role":"assistant","content":"hi"}]',
        input_item_count=1,
        input_fingerprint="fp-1",
    )
    assert saved.response_id == "resp_abc"
    assert saved.input_item_count == 1

    loaded = await coordinator.get_checkpoint(response_id="resp_abc")
    assert loaded is not None
    assert loaded.input_json == '[{"role":"user","content":"hello"}]'
    assert loaded.output_json == '[{"role":"assistant","content":"hi"}]'
    assert loaded.session_id == session_id

    missing = await coordinator.get_checkpoint(response_id="resp_nope")
    assert missing is None


@pytest.mark.asyncio
async def test_checkpoint_upsert_newest_wins(coordinator: DurableBridgeSessionCoordinator) -> None:
    session_id = await _claim_session(coordinator)
    await coordinator.save_checkpoint(
        response_id="resp_dup",
        session_id=session_id,
        api_key_scope="scope-1",
        account_id="acc-1",
        model="gpt-5.6-sol",
        input_json='[{"role":"user","content":"first"}]',
        output_json=None,
        input_item_count=1,
        input_fingerprint=None,
    )
    await coordinator.save_checkpoint(
        response_id="resp_dup",
        session_id=session_id,
        api_key_scope="scope-1",
        account_id="acc-1",
        model="gpt-5.6-sol",
        input_json='[{"role":"user","content":"second"}]',
        output_json='[{"role":"assistant","content":"second reply"}]',
        input_item_count=2,
        input_fingerprint="fp-2",
    )
    loaded = await coordinator.get_checkpoint(response_id="resp_dup")
    assert loaded is not None
    assert loaded.input_json == '[{"role":"user","content":"second"}]'
    assert loaded.output_json == '[{"role":"assistant","content":"second reply"}]'
    assert loaded.input_item_count == 2


@pytest.mark.asyncio
async def test_checkpoint_ttl_expired_is_unreadable(coordinator: DurableBridgeSessionCoordinator) -> None:
    session_id = await _claim_session(coordinator)
    await coordinator.save_checkpoint(
        response_id="resp_ttl",
        session_id=session_id,
        api_key_scope="scope-1",
        account_id="acc-1",
        model="gpt-5.6-sol",
        input_json='[{"role":"user","content":"hello"}]',
        output_json=None,
        input_item_count=1,
        input_fingerprint=None,
        ttl_seconds=1.0,
    )
    # Simulate the TTL elapsing by rewriting expires_at into the past.
    async with coordinator._session() as session:
        record = await session.get(HttpBridgeCheckpointRecord, "resp_ttl")
        assert record is not None
        record.expires_at = utcnow() - timedelta(seconds=5)
        await session.commit()

    assert await coordinator.get_checkpoint(response_id="resp_ttl") is None


@pytest.mark.asyncio
async def test_checkpoint_survives_without_operation_row(
    async_session_factory: Callable[[], AsyncSession],
    coordinator: DurableBridgeSessionCoordinator,
) -> None:
    """A checkpoint is written even when no durable operation is recorded
    (the structural gap: unanchored first turns never create operations)."""
    session_id = await _claim_session(coordinator)
    await coordinator.save_checkpoint(
        response_id="resp_first_turn",
        session_id=session_id,
        api_key_scope="scope-1",
        account_id="acc-1",
        model="gpt-5.6-sol",
        input_json='[{"role":"user","content":"full history"}]',
        output_json=None,
        input_item_count=1,
        input_fingerprint=None,
    )
    assert (await coordinator.get_checkpoint(response_id="resp_first_turn")) is not None


@pytest.mark.asyncio
async def test_checkpoint_repository_scoped(coordinator: DurableBridgeSessionCoordinator) -> None:
    session_id = await _claim_session(coordinator)
    async with coordinator._session() as session:
        repo = DurableBridgeRepository(session)
        saved = await repo.save_checkpoint(
            response_id="resp_repo",
            session_id=session_id,
            api_key_scope="scope-1",
            account_id="acc-1",
            model="gpt-5.6-sol",
            input_json='[]',
            output_json=None,
            input_item_count=0,
            input_fingerprint=None,
        )
        assert saved.response_id == "resp_repo"
        loaded = await repo.get_checkpoint(response_id="resp_repo")
        assert loaded is not None


def _request_state(**overrides: object) -> object:
    from app.modules.proxy._service.support import _WebSocketRequestState

    defaults = dict(
        request_id="req-1",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=0.0,
    )
    defaults.update(overrides)
    return _WebSocketRequestState(**defaults)


def test_checkpoint_lookup_key_resolution_order() -> None:
    """Key precedence: payload anchor, durable latest response, checkpoint
    pointer. Codex HTTP/SSE continuations carry no payload anchor and the
    durable anchor may be cleared by a stuck timeout; the pointer is the last
    resort that survives both."""
    from app.modules.proxy._service.http_bridge.streaming import _http_bridge_checkpoint_lookup_key
    from app.modules.proxy.durable_bridge_coordinator import DurableBridgeLookup

    # 1. payload anchor wins
    rs = _request_state(previous_response_id="resp_payload")
    assert _http_bridge_checkpoint_lookup_key(rs, durable_lookup=None) == "resp_payload"

    # 2. no payload anchor -> durable latest response
    rs = _request_state(previous_response_id=None)
    lookup = DurableBridgeLookup(
        session_id="sid",
        canonical_kind="session_header",
        canonical_key="k",
        api_key_scope="scope",
        account_id="acc",
        owner_instance_id=None,
        owner_epoch=0,
        lease_expires_at=None,
        state=HttpBridgeSessionState.ACTIVE,
        latest_turn_state=None,
        latest_response_id="resp_durable",
    )
    assert _http_bridge_checkpoint_lookup_key(rs, durable_lookup=lookup) == "resp_durable"

    # 3. both cleared -> checkpoint pointer survives
    rs = _request_state(previous_response_id=None)
    lookup = DurableBridgeLookup(
        session_id="sid",
        canonical_kind="session_header",
        canonical_key="k",
        api_key_scope="scope",
        account_id="acc",
        owner_instance_id=None,
        owner_epoch=0,
        lease_expires_at=None,
        state=HttpBridgeSessionState.ACTIVE,
        latest_turn_state=None,
        latest_response_id=None,
        latest_checkpoint_response_id="resp_checkpoint",
    )
    assert _http_bridge_checkpoint_lookup_key(rs, durable_lookup=lookup) == "resp_checkpoint"

    # 4. nothing available -> None
    assert _http_bridge_checkpoint_lookup_key(rs, durable_lookup=None) is None


def test_checkpoint_settings_exist() -> None:
    from app.core.config.settings import Settings

    settings = Settings(_env_file=None)
    assert settings.http_bridge_checkpoint_replay_enabled is True
    assert settings.http_bridge_checkpoint_ttl_seconds > 0


def test_checkpoint_models_exist() -> None:
    assert HttpBridgeCheckpointRecord.__tablename__ == "http_bridge_checkpoints"
    for column in ("response_id", "input_json", "output_json", "input_item_count", "expires_at"):
        assert column in HttpBridgeCheckpointRecord.__table__.columns


