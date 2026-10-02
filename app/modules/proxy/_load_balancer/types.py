from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

from app.core.plan_types import normalize_account_plan_type

AccountLeaseKind = Literal["response_create", "stream"]

MAX_SELECTION_ATTEMPTS = 4


@dataclass
class RuntimeState:
    reset_at: float | None = None
    cooldown_until: float | None = None
    last_error_at: float | None = None
    last_selected_at: float | None = None
    error_count: int = 0
    version: int = 0
    health_version: int = 0
    blocked_at: float | None = None
    health_tier: int = 0
    drain_entered_at: float | None = None
    probe_success_streak: int = 0
    inflight_response_creates: int = 0
    inflight_streams: int = 0
    leased_tokens: float = 0.0
    leases: dict[str, AccountLease] | None = None


@dataclass(frozen=True, slots=True)
class ProbeReservation:
    account_id: str
    previous_last_selected_at: float | None
    reserved_at: float
    expected_runtime_version: int


@dataclass(frozen=True, slots=True)
class AccountLease:
    lease_id: str
    account_id: str
    kind: AccountLeaseKind
    acquired_at: float
    estimated_tokens: float = 0.0


@dataclass(frozen=True, slots=True)
class AccountConcurrencyCaps:
    response_create_limit: int
    stream_limit: int
    configured_response_create_limit: int | None = None
    configured_stream_limit: int | None = None
    replica_count: int = 1
    # Normalized plan -> (response_create_limit, stream_limit); ``None`` (never
    # an empty dict) means no overrides are configured.
    plan_overrides: Mapping[str, tuple[int, int]] | None = None

    def caps_for_plan(self, plan_type: str | None) -> tuple[int, int]:
        """Resolve this account's caps: exact normalized plan match, else the global caps."""
        if self.plan_overrides:
            normalized = normalize_account_plan_type(plan_type)
            if normalized is not None:
                override = self.plan_overrides.get(normalized)
                if override is not None:
                    return (override[0], override[1])
        return (self.response_create_limit, self.stream_limit)
