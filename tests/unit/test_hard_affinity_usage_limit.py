from __future__ import annotations

import time

import pytest

from app.core.balancer import RATE_LIMIT_RESET_MAX_HORIZON_SECONDS, HardAffinityOwnerQuota
from app.core.errors import response_failed_event
from app.db.models import AccountStatus
from app.modules.proxy._service.streaming.helpers import hard_affinity_usage_limit_failed_event

pytestmark = pytest.mark.unit


def test_response_failed_event_carries_quota_metadata() -> None:
    event = response_failed_event(
        "usage_limit_reached",
        "Usage limit reached",
        error_type="usage_limit_reached",
        response_id="resp_test",
        plan_type="plus",
        resets_at=1_791_397_842,
        resets_in_seconds=3_600,
    )

    error = event["response"]["error"]
    assert error["code"] == "usage_limit_reached"
    assert error["type"] == "usage_limit_reached"
    assert error["plan_type"] == "plus"
    assert error["resets_at"] == 1_791_397_842
    assert error["resets_in_seconds"] == 3_600


def test_usage_limit_event_builds_upstream_shape_for_rate_limited_owner() -> None:
    now = time.time()
    reset_at = now + 3_600
    quota = HardAffinityOwnerQuota(
        account_id="owner-1",
        status=AccountStatus.RATE_LIMITED,
        reset_at=reset_at,
        plan_type="plus",
    )

    event = hard_affinity_usage_limit_failed_event(
        error_code="hard_affinity_saturated",
        hard_owner_quota=quota,
        request_id="req_test",
        now=now,
    )

    assert event is not None
    error = event["response"]["error"]
    assert error["code"] == "usage_limit_reached"
    assert error["type"] == "usage_limit_reached"
    assert error["plan_type"] == "plus"
    assert error["resets_at"] == int(reset_at)
    assert error["resets_in_seconds"] == 3_600
    assert "Start a new conversation" in error["message"]


def test_usage_limit_event_requires_hard_affinity_saturated_code() -> None:
    quota = HardAffinityOwnerQuota(
        account_id="owner-1",
        status=AccountStatus.RATE_LIMITED,
        reset_at=time.time() + 3_600,
        plan_type="plus",
    )

    assert (
        hard_affinity_usage_limit_failed_event(
            error_code="no_accounts",
            hard_owner_quota=quota,
            request_id="req_test",
        )
        is None
    )
    assert (
        hard_affinity_usage_limit_failed_event(
            error_code="hard_affinity_saturated",
            hard_owner_quota=None,
            request_id="req_test",
        )
        is None
    )


@pytest.mark.parametrize(
    "status",
    [AccountStatus.ACTIVE, AccountStatus.QUOTA_EXCEEDED, AccountStatus.DEACTIVATED],
)
def test_usage_limit_event_ignores_non_rate_limited_owners(status: AccountStatus) -> None:
    quota = HardAffinityOwnerQuota(
        account_id="owner-1",
        status=status,
        reset_at=time.time() + 3_600,
        plan_type="plus",
    )

    assert (
        hard_affinity_usage_limit_failed_event(
            error_code="hard_affinity_saturated",
            hard_owner_quota=quota,
            request_id="req_test",
        )
        is None
    )


@pytest.mark.parametrize("reset_at_offset", [-120, None, RATE_LIMIT_RESET_MAX_HORIZON_SECONDS * 10])
def test_usage_limit_event_requires_plausible_future_reset(reset_at_offset: int | None) -> None:
    now = time.time()
    reset_at = None if reset_at_offset is None else now + reset_at_offset
    quota = HardAffinityOwnerQuota(
        account_id="owner-1",
        status=AccountStatus.RATE_LIMITED,
        reset_at=reset_at,
        plan_type="plus",
    )

    assert (
        hard_affinity_usage_limit_failed_event(
            error_code="hard_affinity_saturated",
            hard_owner_quota=quota,
            request_id="req_test",
            now=now,
        )
        is None
    )
