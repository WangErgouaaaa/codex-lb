from __future__ import annotations

import json
from typing import Mapping

from app.core.plan_types import normalize_account_plan_type

# (response_create_limit, stream_limit); 0 keeps the "unlimited" convention of the global caps.
PlanConcurrencyCaps = tuple[int, int]

_RESPONSE_CREATE_KEYS = ("responseCreate", "response_create")
_STREAM_KEYS = ("stream", "stream_limit")


def _parse_cap_value(raw: object) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if raw >= 0 else None


def _entry_caps(raw_entry: object) -> PlanConcurrencyCaps | None:
    if not isinstance(raw_entry, dict):
        return None
    response_create: int | None = None
    stream: int | None = None
    for key, value in raw_entry.items():
        if not isinstance(key, str):
            continue
        if key in _RESPONSE_CREATE_KEYS:
            response_create = _parse_cap_value(value)
        elif key in _STREAM_KEYS:
            stream = _parse_cap_value(value)
    if response_create is None or stream is None:
        return None
    return (response_create, stream)


def parse_plan_concurrency_caps(raw: str | None) -> dict[str, PlanConcurrencyCaps]:
    """Leniently parse the dashboard plan-cap JSON column.

    Any malformed input (invalid JSON, non-object, unknown plans, missing or
    negative values) degrades to the global caps: invalid entries are dropped
    instead of failing the request path.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    caps: dict[str, PlanConcurrencyCaps] = {}
    for plan, entry in parsed.items():
        if not isinstance(plan, str):
            continue
        normalized = normalize_account_plan_type(plan)
        if normalized is None or normalized in caps:
            continue
        entry_caps = _entry_caps(entry)
        if entry_caps is not None:
            caps[normalized] = entry_caps
    return caps


def serialize_plan_concurrency_caps(caps: Mapping[str, Mapping[str, object]] | None) -> str:
    """Strictly validate and serialize plan caps for the dashboard column.

    Raises ``ValueError`` on unknown plan keys or invalid values so write paths
    reject bad configuration instead of silently storing it.
    """
    normalized: dict[str, dict[str, int]] = {}
    if caps is not None:
        for plan, entry in caps.items():
            if not isinstance(plan, str):
                raise ValueError("plan cap keys must be strings")
            normalized_plan = normalize_account_plan_type(plan)
            if normalized_plan is None:
                raise ValueError(f"unknown plan type for concurrency caps: {plan!r}")
            entry_caps = _entry_caps(entry)
            if entry_caps is None:
                raise ValueError(f"plan {normalized_plan!r} requires integer responseCreate and stream caps >= 0")
            normalized[normalized_plan] = {"responseCreate": entry_caps[0], "stream": entry_caps[1]}
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def plan_cap_violating_reserve(
    caps: Mapping[str, PlanConcurrencyCaps],
    stream_recovery_reserve: int,
) -> str | None:
    """Return the first plan whose stream cap sits below the recovery reserve, else ``None``."""
    reserve = max(0, int(stream_recovery_reserve))
    for plan, (_, stream_limit) in caps.items():
        if stream_limit > 0 and reserve > stream_limit:
            return plan
    return None
