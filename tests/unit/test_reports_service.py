from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from app.modules.reports.repository import DailyReportRangeTooLargeError, ReportsRepository
from app.modules.reports.service import InvalidReportDateRangeError, ReportsService
from app.modules.usage.credit_aggregation import CreditAttributionRepository

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_get_reports_rejects_oversized_range_after_applying_default_end_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = SimpleNamespace(
        aggregate_summary=AsyncMock(),
        aggregate_daily_rows=AsyncMock(),
        aggregate_by_model=AsyncMock(),
        aggregate_by_account=AsyncMock(),
        earliest_report_activity_at=AsyncMock(),
    )
    service = ReportsService(cast(ReportsRepository, repo))
    fixed_now = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("app.modules.reports.service.utcnow", lambda: fixed_now)

    with pytest.raises(DailyReportRangeTooLargeError, match="730 days or less"):
        await service.get_reports(start_date=date(2020, 1, 1))

    repo.aggregate_summary.assert_not_awaited()
    repo.aggregate_daily_rows.assert_not_awaited()
    repo.aggregate_by_model.assert_not_awaited()
    repo.aggregate_by_account.assert_not_awaited()
    repo.earliest_report_activity_at.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_reports_rejects_inverted_defaulted_range_before_repository_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = SimpleNamespace(
        aggregate_summary=AsyncMock(),
        aggregate_daily_rows=AsyncMock(),
        aggregate_by_model=AsyncMock(),
        aggregate_by_account=AsyncMock(),
        aggregate_by_useragent=AsyncMock(),
        earliest_report_activity_at=AsyncMock(),
    )
    service = ReportsService(cast(ReportsRepository, repo))
    fixed_now = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("app.modules.reports.service.utcnow", lambda: fixed_now)

    with pytest.raises(
        InvalidReportDateRangeError,
        match="start_date must be on or before end_date",
    ):
        await service.get_reports(start_date=date(2026, 6, 13))

    repo.aggregate_summary.assert_not_awaited()
    repo.aggregate_daily_rows.assert_not_awaited()
    repo.aggregate_by_model.assert_not_awaited()
    repo.aggregate_by_account.assert_not_awaited()
    repo.aggregate_by_useragent.assert_not_awaited()
    repo.earliest_report_activity_at.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_reports_serializes_conversation_and_breakdown_request_counts() -> None:
    repo = SimpleNamespace(
        aggregate_summary=AsyncMock(
            side_effect=[
                SimpleNamespace(
                    total_cost_usd=1.2,
                    total_input_tokens=12,
                    total_output_tokens=6,
                    total_cached_tokens=2,
                    total_requests=2,
                    conversation_count=1,
                    total_errors=0,
                    active_accounts=1,
                ),
                SimpleNamespace(
                    total_cost_usd=0.4,
                    total_input_tokens=4,
                    total_output_tokens=2,
                    total_cached_tokens=0,
                    total_requests=1,
                    conversation_count=0,
                    total_errors=0,
                    active_accounts=1,
                ),
            ]
        ),
        aggregate_daily_rows=AsyncMock(
            return_value=[
                SimpleNamespace(
                    date="2026-06-01",
                    requests=2,
                    conversation_count=1,
                    input_tokens=12,
                    output_tokens=6,
                    cached_input_tokens=2,
                    cost_usd=1.2,
                    active_accounts=1,
                    error_count=0,
                    median_ttft_ms=123.456,
                    median_tps=78.901,
                    median_queue_ms=45.678,
                )
            ]
        ),
        aggregate_by_model=AsyncMock(return_value=[SimpleNamespace(model="gpt-5.1", cost_usd=1.2, request_count=2)]),
        aggregate_by_account=AsyncMock(
            return_value=[SimpleNamespace(account_id="acc_reports", alias="Reports", cost_usd=1.2, request_count=2)]
        ),
        aggregate_by_useragent=AsyncMock(
            return_value=[SimpleNamespace(useragent_group="opencode", cost_usd=1.2, request_count=2)]
        ),
        earliest_report_activity_at=AsyncMock(return_value=datetime(2026, 5, 1, 0, 0, 0)),
    )
    service = ReportsService(cast(ReportsRepository, repo))

    result = await service.get_reports(
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 1),
        useragent_group="opencode",
    )

    repo.aggregate_summary.assert_any_await(
        datetime(2026, 6, 1, 0, 0, 0),
        datetime(2026, 6, 2, 0, 0, 0),
        None,
        None,
        "opencode",
    )
    repo.aggregate_daily_rows.assert_awaited_once_with(
        date(2026, 6, 1),
        date(2026, 6, 1),
        timezone.utc,
        None,
        None,
        "opencode",
    )
    repo.aggregate_by_model.assert_awaited_once_with(
        datetime(2026, 6, 1, 0, 0, 0),
        datetime(2026, 6, 2, 0, 0, 0),
        None,
        None,
        "opencode",
    )
    repo.aggregate_by_account.assert_awaited_once_with(
        datetime(2026, 6, 1, 0, 0, 0),
        datetime(2026, 6, 2, 0, 0, 0),
        None,
        None,
        "opencode",
    )
    repo.aggregate_by_useragent.assert_awaited_once_with(
        datetime(2026, 6, 1, 0, 0, 0),
        datetime(2026, 6, 2, 0, 0, 0),
        None,
        None,
        "opencode",
    )
    repo.earliest_report_activity_at.assert_awaited_once_with(None, None, "opencode")

    assert result.daily[0].median_ttft_ms == 123.46
    assert result.daily[0].conversations == 1
    assert result.daily[0].median_tps == 78.9
    assert result.daily[0].median_queue_ms == 45.68
    assert result.by_model[0].model == "gpt-5.1"
    assert result.summary.total_conversations == 1
    assert result.by_model[0].requests == 2
    assert result.by_useragent[0].useragent == "opencode"
    assert result.by_useragent[0].requests == 2
    assert result.by_useragent[0].percentage == 100.0


@pytest.mark.asyncio
async def test_get_usage_stats_accounts_metric_builds_entries_and_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = SimpleNamespace(
        aggregate_summary=AsyncMock(
            return_value=SimpleNamespace(
                total_input_tokens=1900,
                total_output_tokens=200,
                total_cached_tokens=50,
                total_requests=3,
                total_errors=0,
            )
        ),
        aggregate_usage_by_model=AsyncMock(return_value=[]),
        aggregate_usage_by_model_bucket=AsyncMock(return_value=[]),
        aggregate_usage_by_account=AsyncMock(
            return_value=[
                SimpleNamespace(
                    account_id="acc_token_heavy",
                    requests=2,
                    input_tokens=1000,
                    output_tokens=200,
                    cached_input_tokens=50,
                    cost_usd=0.0,
                ),
                SimpleNamespace(
                    account_id="acc_alias",
                    requests=1,
                    input_tokens=900,
                    output_tokens=0,
                    cached_input_tokens=0,
                    cost_usd=0.0,
                ),
            ]
        ),
        list_accounts=AsyncMock(
            return_value=[
                SimpleNamespace(id="acc_alias", alias="Alias", email="alias@example.com", plan_type="plus"),
                SimpleNamespace(id="acc_token_heavy", alias=None, email="token-heavy@example.com", plan_type=None),
            ]
        ),
    )
    credit_repo = SimpleNamespace(
        aggregate_credits_by_model=AsyncMock(return_value=[]),
        aggregate_credits_by_model_bucket=AsyncMock(return_value=[]),
        aggregate_credits_by_account=AsyncMock(
            return_value=[SimpleNamespace(account_id="acc_alias", credits_sum=75.6, request_count=1)]
        ),
    )
    service = ReportsService(cast(ReportsRepository, repo), cast(CreditAttributionRepository, credit_repo))
    fixed_now = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("app.modules.reports.service.utcnow", lambda: fixed_now)

    result = await service.get_usage_stats(range_key="7d", metric="accounts")

    # Entries sort by total tokens desc regardless of credits volume.
    assert [entry.account_id for entry in result.accounts] == ["acc_token_heavy", "acc_alias"]
    token_heavy, alias_entry = result.accounts
    # Name falls back alias > email; quota_percent divides by the plan's
    # nominal weekly capacity (plus: 7560 credits).
    assert alias_entry.name == "Alias"
    assert alias_entry.plan_type == "plus"
    assert alias_entry.capacity_credits == 7560.0
    assert alias_entry.quota_percent == 1.0
    assert alias_entry.credits == 75.6
    # Unknown/missing plan types have no nominal capacity to divide by.
    assert token_heavy.name == "token-heavy@example.com"
    assert token_heavy.plan_type is None
    assert token_heavy.capacity_credits == 0.0
    assert token_heavy.quota_percent == 0.0
    # Token totals count input + output only (cached is an input subset).
    assert token_heavy.total_tokens == 1200
    # The accounts view skips the per-model bucket series entirely.
    assert all(bucket.values == {} for bucket in result.series)
    assert result.summary.account_count == 2
    assert result.summary.secondary_capacity_credits == 7560.0
    repo.aggregate_usage_by_model_bucket.assert_not_awaited()
    credit_repo.aggregate_credits_by_model_bucket.assert_not_awaited()
    repo.aggregate_usage_by_account.assert_awaited_once_with(
        datetime(2026, 6, 6, 0, 0, 0),
        datetime(2026, 6, 13, 0, 0, 0),
    )
    credit_repo.aggregate_credits_by_account.assert_awaited_once_with(
        datetime(2026, 6, 6, 0, 0, 0),
        datetime(2026, 6, 13, 0, 0, 0),
        "secondary",
    )


@pytest.mark.asyncio
async def test_get_usage_stats_accounts_metric_without_credit_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = SimpleNamespace(
        aggregate_summary=AsyncMock(
            return_value=SimpleNamespace(
                total_input_tokens=10,
                total_output_tokens=5,
                total_cached_tokens=0,
                total_requests=1,
                total_errors=0,
            )
        ),
        aggregate_usage_by_model=AsyncMock(return_value=[]),
        aggregate_usage_by_model_bucket=AsyncMock(return_value=[]),
        aggregate_usage_by_account=AsyncMock(
            return_value=[
                SimpleNamespace(
                    account_id="acc_solo",
                    requests=1,
                    input_tokens=10,
                    output_tokens=5,
                    cached_input_tokens=0,
                    cost_usd=0.0,
                )
            ]
        ),
        list_accounts=AsyncMock(
            return_value=[SimpleNamespace(id="acc_solo", alias=None, email="solo@example.com", plan_type="edu")]
        ),
    )
    service = ReportsService(cast(ReportsRepository, repo))
    fixed_now = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("app.modules.reports.service.utcnow", lambda: fixed_now)

    result = await service.get_usage_stats(range_key="today", metric="accounts")

    # Without a credit repository the accounts view still returns entries;
    # credits and quota stay zero rather than blocking the panel.
    assert len(result.accounts) == 1
    entry = result.accounts[0]
    assert entry.name == "solo@example.com"
    assert entry.total_tokens == 15
    assert entry.credits == 0.0
    assert entry.quota_percent == 0.0
    assert entry.capacity_credits == 7560.0
    assert result.summary.account_count == 1
