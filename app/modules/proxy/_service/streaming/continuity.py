from __future__ import annotations

from typing import cast

from app.core.openai.models import OpenAIEvent
from app.core.openai.requests import ResponsesRequest
from app.core.types import JsonValue
from app.modules.proxy._service.support import _fingerprint_input_items, _WebSocketContinuityState


def _record_http_stream_continuity_completion(
    continuity_state: _WebSocketContinuityState | None,
    payload: ResponsesRequest,
    event: OpenAIEvent | None,
) -> None:
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
