from __future__ import annotations

import json
import time
import zlib
from typing import cast

from app.core.openai.models import OpenAIEvent
from app.core.openai.requests import ResponsesRequest
from app.core.types import JsonValue
from app.modules.proxy._service.support import _fingerprint_input_items, _WebSocketContinuityState
from app.modules.proxy.replay_safety import (
    project_responses_input_for_account_neutral_fresh_replay,
    responses_payload_is_account_neutral_fresh_replay,
)

_HTTP_STREAM_ACTIVE_TURN_REPLAY_RAW_MAX_BYTES = 16 * 1024 * 1024
_HTTP_STREAM_ACTIVE_TURN_REPLAY_COMPRESSED_MAX_BYTES = 4 * 1024 * 1024
_HTTP_STREAM_ACTIVE_TURN_REPLAY_TTL_SECONDS = 2 * 60 * 60
_TOOL_CALL_TYPES = frozenset({"apply_patch_call", "custom_tool_call", "function_call"})


def _clear_http_stream_active_turn_replay(continuity_state: _WebSocketContinuityState) -> None:
    continuity_state.http_stream_active_turn_replay_zlib = None
    continuity_state.http_stream_active_turn_replay_recorded_at = None


def _project_account_neutral_input(input_items: list[JsonValue]) -> list[JsonValue] | None:
    if not input_items:
        return None
    projection = project_responses_input_for_account_neutral_fresh_replay(
        input_items,
        stored_count=len(input_items),
    )
    if projection is None or not projection.input_items:
        return None
    return projection.input_items


def _store_http_stream_active_turn_replay(
    continuity_state: _WebSocketContinuityState,
    input_items: list[JsonValue],
) -> None:
    raw = json.dumps(input_items, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > _HTTP_STREAM_ACTIVE_TURN_REPLAY_RAW_MAX_BYTES:
        _clear_http_stream_active_turn_replay(continuity_state)
        return
    compressed = zlib.compress(raw, level=1)
    if len(compressed) > _HTTP_STREAM_ACTIVE_TURN_REPLAY_COMPRESSED_MAX_BYTES:
        _clear_http_stream_active_turn_replay(continuity_state)
        return
    continuity_state.http_stream_active_turn_replay_zlib = compressed
    continuity_state.http_stream_active_turn_replay_recorded_at = time.monotonic()


def _load_http_stream_active_turn_replay(
    continuity_state: _WebSocketContinuityState | None,
) -> list[JsonValue] | None:
    if continuity_state is None:
        return None
    compressed = continuity_state.http_stream_active_turn_replay_zlib
    recorded_at = continuity_state.http_stream_active_turn_replay_recorded_at
    if compressed is None or recorded_at is None:
        return None
    if time.monotonic() - recorded_at > _HTTP_STREAM_ACTIVE_TURN_REPLAY_TTL_SECONDS:
        _clear_http_stream_active_turn_replay(continuity_state)
        return None
    try:
        raw = zlib.decompress(compressed)
        if len(raw) > _HTTP_STREAM_ACTIVE_TURN_REPLAY_RAW_MAX_BYTES:
            raise ValueError("retained HTTP stream replay exceeds raw size limit")
        decoded = json.loads(raw)
    except (UnicodeDecodeError, ValueError, zlib.error):
        _clear_http_stream_active_turn_replay(continuity_state)
        return None
    if not isinstance(decoded, list):
        _clear_http_stream_active_turn_replay(continuity_state)
        return None
    return cast(list[JsonValue], decoded)


def _retained_http_stream_fresh_replay(
    continuity_state: _WebSocketContinuityState | None,
    payload: ResponsesRequest,
) -> ResponsesRequest | None:
    retained_input = _load_http_stream_active_turn_replay(continuity_state)
    input_value = payload.input
    if retained_input is None or not isinstance(input_value, list) or not input_value:
        return None
    projected_input = _project_account_neutral_input(cast(list[JsonValue], input_value))
    if projected_input is None:
        return None
    if len(projected_input) >= len(retained_input) and projected_input[: len(retained_input)] == retained_input:
        replay_input = projected_input
    else:
        replay_input = [*retained_input, *projected_input]
    fresh_payload = payload.model_copy(
        update={
            "conversation": None,
            "input": replay_input,
            "previous_response_id": None,
        }
    )
    if not responses_payload_is_account_neutral_fresh_replay(fresh_payload.to_payload()):
        return None
    return fresh_payload


def _record_http_continuity(
    continuity_state: _WebSocketContinuityState | None,
    payload: ResponsesRequest,
    event: OpenAIEvent | None,
    event_payload: dict[str, JsonValue] | None,
    output_items: list[JsonValue],
) -> None:
    if isinstance(event_payload, dict) and event_payload.get("type") == "response.output_item.done":
        item = event_payload.get("item")
        if isinstance(item, dict):
            output_items.append(cast(JsonValue, item))
    if continuity_state is None or event is None or event.type != "response.completed":
        return
    response_id = event.response.id if event.response else None
    continuity_state.last_completed_response_id = response_id
    input_value = payload.input
    if response_id is not None and isinstance(input_value, list) and input_value:
        continuity_state.last_completed_input_count = len(input_value)
        continuity_state.last_completed_input_prefix_fingerprint = _fingerprint_input_items(
            cast(list[JsonValue], input_value)
        )
    else:
        continuity_state.last_completed_input_count = 0
        continuity_state.last_completed_input_prefix_fingerprint = None
    continuity_state.last_pending_function_call_ids = []
    continuity_state.last_pending_tool_call_types = {}
    if not any(isinstance(item, dict) and item.get("type") in _TOOL_CALL_TYPES for item in output_items):
        _clear_http_stream_active_turn_replay(continuity_state)
        return
    retained_input = _load_http_stream_active_turn_replay(continuity_state)
    input_value = payload.input
    if not isinstance(input_value, list) or not input_value:
        _clear_http_stream_active_turn_replay(continuity_state)
        return
    projected_input = _project_account_neutral_input(cast(list[JsonValue], input_value))
    projected_output = _project_account_neutral_input(output_items)
    if projected_input is None or projected_output is None:
        _clear_http_stream_active_turn_replay(continuity_state)
        return
    if retained_input is None:
        replay_input = projected_input
    elif len(projected_input) >= len(retained_input) and projected_input[: len(retained_input)] == retained_input:
        replay_input = projected_input
    else:
        replay_input = [*retained_input, *projected_input]
    _store_http_stream_active_turn_replay(
        continuity_state,
        [*replay_input, *projected_output],
    )
