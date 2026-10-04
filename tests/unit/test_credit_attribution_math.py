from __future__ import annotations

from datetime import datetime

import pytest

from app.modules.usage.credit_attribution import (
    delta_percent_to_credits,
    request_token_weight,
    split_credits,
    window_delta_percent,
)


def _dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, 12, hour, minute, 0)


class TestWindowDeltaPercent:
    def test_increasing_usage_with_same_reset_returns_delta(self) -> None:
        reset = _dt(15)
        assert window_delta_percent(10.0, 20.0, reset, reset) == pytest.approx(10.0)

    def test_reset_at_change_skips_pair(self) -> None:
        assert window_delta_percent(10.0, 20.0, _dt(15), _dt(16)) is None

    def test_missing_reset_at_skips_pair(self) -> None:
        assert window_delta_percent(10.0, 20.0, None, _dt(15)) is None
        assert window_delta_percent(10.0, 20.0, _dt(15), None) is None

    def test_usage_drop_is_not_consumption(self) -> None:
        reset = _dt(15)
        assert window_delta_percent(20.0, 10.0, reset, reset) is None

    def test_equal_usage_yields_no_delta(self) -> None:
        reset = _dt(15)
        assert window_delta_percent(20.0, 20.0, reset, reset) is None

    def test_missing_usage_on_either_side_skips_pair(self) -> None:
        reset = _dt(15)
        assert window_delta_percent(None, 20.0, reset, reset) is None
        assert window_delta_percent(10.0, None, reset, reset) is None


class TestDeltaPercentToCredits:
    def test_converts_percent_delta_via_capacity(self) -> None:
        assert delta_percent_to_credits(10.0, 225.0) == pytest.approx(22.5)

    def test_unknown_capacity_skips_conversion(self) -> None:
        assert delta_percent_to_credits(10.0, None) is None

    def test_non_positive_capacity_skips_conversion(self) -> None:
        assert delta_percent_to_credits(10.0, 0.0) is None
        assert delta_percent_to_credits(10.0, -5.0) is None


class TestRequestTokenWeight:
    def test_output_tokens_prefer_reasoning_fallback(self) -> None:
        assert request_token_weight(100, 50, 80, 10) == 150
        assert request_token_weight(100, None, 80, 10) == 180

    def test_nulls_count_as_zero(self) -> None:
        assert request_token_weight(None, None, None, None) == 0
        assert request_token_weight(None, 40, None, None) == 40

    def test_cached_tokens_do_not_add_weight(self) -> None:
        # Cached input is already counted inside input_tokens; adding it again
        # would double-count the cached share of the footprint.
        assert request_token_weight(100, 50, 80, 10) == request_token_weight(100, 50, 80, 0) == 150
        assert request_token_weight(100, 50, 80, 83) == 150


class TestSplitCredits:
    def test_split_proportional_to_weights(self) -> None:
        shares = split_credits(10.0, [300, 100])
        assert shares[0] == pytest.approx(7.5)
        assert shares[1] == pytest.approx(2.5)

    def test_zero_total_weight_splits_equally(self) -> None:
        shares = split_credits(9.0, [0, 0, 0])
        assert shares == [pytest.approx(3.0)] * 3

    def test_single_request_takes_all_credits(self) -> None:
        assert split_credits(42.5, [123]) == [pytest.approx(42.5)]

    def test_shares_conserve_total(self) -> None:
        shares = split_credits(151.2, [1650, 150, 60])
        assert sum(shares) == pytest.approx(151.2)
