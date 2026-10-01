from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from app.modules.reports.repository import ReportsRepository, UsageModelAggregateRow, UsageModelBucketRow
from app.modules.reports.service import ReportsService

pytestmark = pytest.mark.unit


def _summary_row(**overrides):
    base = dict(
        total_cost_usd=0.0,
        total_input_tokens=100,
        total_output_tokens=40,
        total_reasoning_tokens=0,
        reasoning_usage_known_requests=0,
        total_cached_tokens=10,
        total_requests=7,
        total_errors=1,
        active_accounts=2,
        conversation_count=0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_repo(bucket_rows=None, model_rows=None):
    return SimpleNamespace(
        aggregate_summary=AsyncMock(return_value=_summary_row()),
        aggregate_usage_by_model=AsyncMock(
            return_value=model_rows
            if model_rows is not None
            else [
                UsageModelAggregateRow(
                    model="gpt-x", requests=5, input_tokens=80, output_tokens=30, cached_input_tokens=8
                ),
                UsageModelAggregateRow(
                    model="gpt-y", requests=2, input_tokens=20, output_tokens=10, cached_input_tokens=2
                ),
            ]
        ),
        aggregate_usage_by_model_bucket=AsyncMock(return_value=bucket_rows or []),
    )


@pytest.mark.asyncio
async def test_get_usage_stats_today_uses_hourly_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo()
    service = ReportsService(cast(ReportsRepository, repo))
    fixed_now = datetime(2026, 6, 12, 15, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("app.modules.reports.service.utcnow", lambda: fixed_now)

    result = await service.get_usage_stats(range_key="today", report_timezone="UTC")

    assert result.range == "today"
    assert result.bucket == "hour"
    assert result.start_date == "2026-06-12"
    assert result.end_date == "2026-06-12"

    bucket_ranges = repo.aggregate_usage_by_model_bucket.await_args.args[0]
    assert len(bucket_ranges) == 24
    assert bucket_ranges[0][0] == "2026-06-12T00"
    assert bucket_ranges[0][1] == "00:00"
    assert bucket_ranges[0][2] == datetime(2026, 6, 12, 0, 0, 0)
    assert bucket_ranges[0][3] == datetime(2026, 6, 12, 1, 0, 0)
    assert bucket_ranges[-1][0] == "2026-06-12T23"

    # Summary window spans the full local day in UTC-naive terms.
    summary_args = repo.aggregate_summary.await_args.args
    assert summary_args[0] == datetime(2026, 6, 12, 0, 0, 0)
    assert summary_args[1] == datetime(2026, 6, 13, 0, 0, 0)

    assert len(result.series) == 24
    assert result.series[0].bucket == "2026-06-12T00"
    assert result.series[0].values == {}


@pytest.mark.asyncio
async def test_get_usage_stats_7d_uses_daily_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo()
    service = ReportsService(cast(ReportsRepository, repo))
    fixed_now = datetime(2026, 6, 12, 15, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("app.modules.reports.service.utcnow", lambda: fixed_now)

    result = await service.get_usage_stats(range_key="7d", report_timezone="UTC")

    assert result.bucket == "day"
    assert result.start_date == "2026-06-06"
    assert result.end_date == "2026-06-12"
    bucket_ranges = repo.aggregate_usage_by_model_bucket.await_args.args[0]
    assert len(bucket_ranges) == 7
    assert bucket_ranges[0] == ("2026-06-06", "06-06", datetime(2026, 6, 6), datetime(2026, 6, 7))
    assert bucket_ranges[-1][0] == "2026-06-12"


@pytest.mark.asyncio
async def test_get_usage_stats_30d_covers_thirty_calendar_days(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo()
    service = ReportsService(cast(ReportsRepository, repo))
    fixed_now = datetime(2026, 6, 12, 15, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("app.modules.reports.service.utcnow", lambda: fixed_now)

    result = await service.get_usage_stats(range_key="30d", report_timezone="UTC")

    assert result.start_date == "2026-05-14"
    assert result.end_date == "2026-06-12"
    assert len(result.series) == 30


@pytest.mark.asyncio
async def test_get_usage_stats_converts_local_boundaries_to_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo()
    service = ReportsService(cast(ReportsRepository, repo))
    fixed_now = datetime(2026, 6, 12, 2, 0, 0, tzinfo=timezone.utc)  # 10:00 in Asia/Shanghai
    monkeypatch.setattr("app.modules.reports.service.utcnow", lambda: fixed_now)

    result = await service.get_usage_stats(range_key="today", report_timezone="Asia/Shanghai")

    assert result.timezone == "Asia/Shanghai"
    bucket_ranges = repo.aggregate_usage_by_model_bucket.await_args.args[0]
    assert bucket_ranges[0][2] == datetime(2026, 6, 11, 16, 0, 0)
    assert bucket_ranges[0][3] == datetime(2026, 6, 11, 17, 0, 0)


@pytest.mark.asyncio
async def test_get_usage_stats_assembles_model_breakdown_and_series(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(
        bucket_rows=[
            UsageModelBucketRow(
                bucket_key="2026-06-12T10",
                bucket_label="10:00",
                model="gpt-x",
                requests=3,
                input_tokens=50,
                output_tokens=20,
                cached_input_tokens=5,
            ),
            UsageModelBucketRow(
                bucket_key="2026-06-12T10",
                bucket_label="10:00",
                model="unknown",
                requests=0,
                input_tokens=0,
                output_tokens=0,
                cached_input_tokens=0,
            ),
        ],
    )
    service = ReportsService(cast(ReportsRepository, repo))
    fixed_now = datetime(2026, 6, 12, 15, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("app.modules.reports.service.utcnow", lambda: fixed_now)

    result = await service.get_usage_stats(range_key="today", report_timezone="UTC")

    # Artificial zero-fill rows for empty buckets must not leak as a model.
    assert result.series[10].values == {"gpt-x": 75}
    assert all("unknown" not in bucket.values for bucket in result.series)

    # byModel keeps repository order (total tokens desc) and computes shares.
    assert [entry.model for entry in result.by_model] == ["gpt-x", "gpt-y"]
    assert result.by_model[0].total_tokens == 118
    assert result.by_model[0].percentage == 78.7  # 118 / 150
    assert result.by_model[1].percentage == 21.3  # 32 / 150

    # Summary totals combine input + output + cached.
    assert result.summary.total_tokens == 150
    assert result.summary.total_requests == 7
    assert result.summary.model_count == 2
    assert result.summary.avg_tokens_per_day == 150.0


@pytest.mark.asyncio
async def test_get_usage_stats_rejects_unknown_range() -> None:
    repo = _make_repo()
    service = ReportsService(cast(ReportsRepository, repo))

    with pytest.raises(ValueError, match="range must be one of"):
        await service.get_usage_stats(range_key="yesterday")

    repo.aggregate_summary.assert_not_awaited()


def _make_credit_repo(model_rows=None, bucket_rows=None):
    from app.modules.usage.credit_aggregation import CreditModelAggregateRow, CreditModelBucketRow

    return SimpleNamespace(
        aggregate_credits_by_model=AsyncMock(return_value=model_rows or []),
        aggregate_credits_by_model_bucket=AsyncMock(return_value=bucket_rows or []),
        _rows_for_typing=(CreditModelAggregateRow, CreditModelBucketRow),
    )


@pytest.mark.asyncio
async def test_get_usage_stats_cost_metric_uses_cost_series(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(
        model_rows=[
            UsageModelAggregateRow(
                model="gpt-y", requests=2, input_tokens=20, output_tokens=10, cached_input_tokens=2, cost_usd=1.5
            ),
            UsageModelAggregateRow(
                model="gpt-x", requests=5, input_tokens=80, output_tokens=30, cached_input_tokens=8, cost_usd=0.5
            ),
        ],
        bucket_rows=[
            UsageModelBucketRow(
                bucket_key="2026-06-12T10",
                bucket_label="10:00",
                model="gpt-x",
                requests=3,
                input_tokens=50,
                output_tokens=20,
                cached_input_tokens=5,
                cost_usd=0.5,
            )
        ],
    )
    service = ReportsService(cast(ReportsRepository, repo))
    fixed_now = datetime(2026, 6, 12, 15, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("app.modules.reports.service.utcnow", lambda: fixed_now)

    result = await service.get_usage_stats(range_key="today", report_timezone="UTC", metric="cost")

    assert result.metric == "cost"
    # byModel is ordered by the selected metric (cost), not tokens.
    assert [entry.model for entry in result.by_model] == ["gpt-y", "gpt-x"]
    assert result.by_model[0].percentage == 75.0  # 1.5 / 2.0
    assert result.by_model[0].cost_usd == 1.5
    assert result.series[10].values == {"gpt-x": 0.5}
    assert result.summary.total_cost_usd == 2.0


@pytest.mark.asyncio
async def test_get_usage_stats_credits_metric_uses_attributed_credits(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.usage.credit_aggregation import CreditModelAggregateRow, CreditModelBucketRow

    repo = _make_repo()
    credit_repo = _make_credit_repo(
        model_rows=[
            CreditModelAggregateRow(model="gpt-x", credits_sum=12.5, request_count=3),
            CreditModelAggregateRow(model="gpt-y", credits_sum=2.5, request_count=1),
        ],
        bucket_rows=[
            CreditModelBucketRow(
                bucket_key="2026-06-12T10", bucket_label="10:00", model="gpt-x", credits_sum=12.5, request_count=3
            )
        ],
    )
    from app.modules.usage.credit_aggregation import CreditAttributionRepository as _CreditRepo

    service = ReportsService(cast(ReportsRepository, repo), cast(_CreditRepo, credit_repo))
    fixed_now = datetime(2026, 6, 12, 15, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("app.modules.reports.service.utcnow", lambda: fixed_now)

    result = await service.get_usage_stats(range_key="today", report_timezone="UTC", metric="credits")

    assert result.metric == "credits"
    credit_repo.aggregate_credits_by_model.assert_awaited_once()
    # The weekly (secondary) window is the attribution source.
    assert credit_repo.aggregate_credits_by_model.await_args.args[2] == "secondary"
    assert [entry.model for entry in result.by_model] == ["gpt-x", "gpt-y"]
    assert result.by_model[0].credits == 12.5
    assert result.by_model[0].percentage == 83.3  # 12.5 / 15.0
    assert result.series[10].values == {"gpt-x": 12.5}
    assert result.summary.total_credits == 15.0
    assert result.summary.attributed_requests == 4


@pytest.mark.asyncio
async def test_get_usage_stats_credits_without_credit_repository_returns_zeros(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo()
    service = ReportsService(cast(ReportsRepository, repo))
    fixed_now = datetime(2026, 6, 12, 15, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("app.modules.reports.service.utcnow", lambda: fixed_now)

    result = await service.get_usage_stats(range_key="today", report_timezone="UTC", metric="credits")

    assert result.summary.total_credits == 0.0
    assert result.summary.attributed_requests == 0
    assert all(not bucket.values for bucket in result.series)


@pytest.mark.asyncio
async def test_get_usage_stats_rejects_unknown_metric() -> None:
    repo = _make_repo()
    service = ReportsService(cast(ReportsRepository, repo))

    with pytest.raises(ValueError, match="metric must be one of"):
        await service.get_usage_stats(range_key="7d", metric="stars")

    repo.aggregate_summary.assert_not_awaited()
