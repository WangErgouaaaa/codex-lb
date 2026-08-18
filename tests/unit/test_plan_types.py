from __future__ import annotations

import pytest

from app.core.plan_types import (
    account_plan_matches_allowed,
    canonicalize_account_plan_type,
    coerce_account_plan_type,
    normalize_account_plan_type,
    normalize_rate_limit_plan_type,
)

pytestmark = pytest.mark.unit


def test_prolite_matches_pro_model_plan_entitlement():
    assert account_plan_matches_allowed("prolite", frozenset({"pro"})) is True
    assert account_plan_matches_allowed("prolite", frozenset({"plus"})) is False


def test_k12_matches_edu_model_plan_entitlement():
    assert account_plan_matches_allowed("k12", frozenset({"edu"})) is True
    assert account_plan_matches_allowed("k12", frozenset({"plus"})) is False


@pytest.mark.parametrize("value", ["chatgptplusplan", " ChatGPTPlusPlan "])
def test_chatgpt_plus_plan_alias_is_canonicalized_as_plus(value: str):
    assert normalize_account_plan_type(value) == "plus"
    assert canonicalize_account_plan_type(value) == "plus"
    assert coerce_account_plan_type(value, "free") == "plus"
    assert normalize_rate_limit_plan_type(value) == "plus"
    assert account_plan_matches_allowed(value, frozenset({"plus"})) is True


def test_unknown_plan_passes_when_explicitly_allowed():
    assert account_plan_matches_allowed("future_plan", frozenset({"future_plan", "plus"})) is True


def test_unknown_plan_matching_is_case_insensitive_and_trims_account_value():
    assert account_plan_matches_allowed(" Future_Plan ", frozenset({"future_plan"})) is True


def test_unknown_plan_blocked_when_not_explicitly_allowed():
    assert account_plan_matches_allowed("future_plan", frozenset({"plus"})) is False
