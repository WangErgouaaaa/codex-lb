from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.utils.time import to_utc_naive, utcnow
from app.core import usage as usage_core
from app.modules.reports.repository import (
    MAX_DAILY_REPORT_DAYS,
    UNKNOWN_MODEL_BUCKET,
    USAGE_BUCKET_DAY,
    USAGE_BUCKET_HOUR,
    DailyReportRangeTooLargeError,
    ReportsRepository,
    UsageAccountAggregateRow,
    build_usage_bucket_ranges,
)
from app.modules.reports.schemas import (
    AccountCostEntry,
    DailyReportRow,
    ModelCostEntry,
    ReportComparison,
    ReportComparisonPrevious,
    ReportsResponse,
    ReportSummary,
    UsageAccountEntry,
    UsageModelEntry,
    UsageSeriesBucket,
    UsageStatsResponse,
    UsageStatsSummary,
    UsageWindowSeriesBucket,
    UserAgentCostEntry,
)
from app.modules.usage.credit_aggregation import (
    CreditAccountAggregateRow,
    CreditAttributionRepository,
)
from app.modules.usage.mappers import usage_history_to_window_row
from app.modules.usage.repository import UsageRepository


class InvalidReportDateRangeError(ValueError):
    """Raised when a report starts after it ends."""


USAGE_STATS_RANGES = ("today", "7d", "30d")
USAGE_STATS_METRICS = ("tokens", "cost", "credits", "window_credits", "accounts")
# Credits are read from the weekly window: it is the real subscription budget.
CREDITS_WINDOW = "secondary"
# The window_credits metric charts both quota windows as share-of-range lines.
WINDOW_CREDITS_SERIES_WINDOWS = ("primary", "secondary")


class ReportsService:
    def __init__(
        self,
        repository: ReportsRepository,
        credit_repository: CreditAttributionRepository | None = None,
        usage_repository: UsageRepository | None = None,
    ) -> None:
        self._repository = repository
        self._credit_repository = credit_repository
        self._usage_repository = usage_repository

    async def get_reports(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
        report_timezone: str | None = None,
        account_ids: list[str] | None = None,
        model: str | None = None,
        useragent_group: str | None = None,
    ) -> ReportsResponse:
        timezone_info = _resolve_timezone(report_timezone)
        now = utcnow().replace(tzinfo=timezone.utc).astimezone(timezone_info)
        if end_date is None:
            end_date = now.date()
        if start_date is None:
            start_date = end_date - timedelta(days=6)
        if start_date > end_date:
            raise InvalidReportDateRangeError("start_date must be on or before end_date")
        window_days = (end_date - start_date).days + 1
        if window_days > MAX_DAILY_REPORT_DAYS:
            raise DailyReportRangeTooLargeError(f"report date range must be {MAX_DAILY_REPORT_DAYS} days or less")

        start_at = _local_midnight_to_utc_naive(start_date, timezone_info)
        end_at = _local_midnight_to_utc_naive(end_date + timedelta(days=1), timezone_info)
        previous_end_date = start_date - timedelta(days=1)
        previous_start_date = previous_end_date - timedelta(days=window_days - 1)
        previous_start_at = _local_midnight_to_utc_naive(previous_start_date, timezone_info)
        previous_end_at = _local_midnight_to_utc_naive(previous_end_date + timedelta(days=1), timezone_info)

        summary = await self._repository.aggregate_summary(start_at, end_at, account_ids, model, useragent_group)
        previous_summary = await self._repository.aggregate_summary(
            previous_start_at,
            previous_end_at,
            account_ids,
            model,
            useragent_group,
        )
        earliest_activity_at = await self._repository.earliest_report_activity_at(account_ids, model, useragent_group)
        daily_rows = await self._repository.aggregate_daily_rows(
            start_date,
            end_date,
            timezone_info,
            account_ids,
            model,
            useragent_group,
        )
        daily = [
            DailyReportRow(
                date=row.date,
                requests=row.requests,
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cached_input_tokens=row.cached_input_tokens,
                cost_usd=round(row.cost_usd, 4),
                active_accounts=row.active_accounts,
                conversations=row.conversation_count,
                error_count=row.error_count,
                median_ttft_ms=round(row.median_ttft_ms, 2),
                median_tps=round(row.median_tps, 2),
                median_queue_ms=round(row.median_queue_ms, 2),
            )
            for row in daily_rows
        ]
        by_model = await self._repository.aggregate_by_model(start_at, end_at, account_ids, model, useragent_group)
        by_account = await self._repository.aggregate_by_account(start_at, end_at, account_ids, model, useragent_group)
        by_useragent = await self._repository.aggregate_by_useragent(
            start_at,
            end_at,
            account_ids,
            model,
            useragent_group,
        )

        day_count = max((end_at.date() - start_at.date()).days, 1)

        model_total = sum(m.cost_usd for m in by_model)
        useragent_total = sum(u.cost_usd for u in by_useragent)
        comparison = ReportComparison(
            can_compare=earliest_activity_at is not None and earliest_activity_at <= previous_start_at,
            previous=ReportComparisonPrevious(
                total_cost_usd=round(previous_summary.total_cost_usd, 4),
                total_tokens=previous_summary.total_input_tokens + previous_summary.total_output_tokens,
                total_requests=previous_summary.total_requests,
            ),
        )

        return ReportsResponse(
            summary=ReportSummary(
                total_cost_usd=round(summary.total_cost_usd, 4),
                total_input_tokens=summary.total_input_tokens,
                total_output_tokens=summary.total_output_tokens,
                total_cached_tokens=summary.total_cached_tokens,
                total_requests=summary.total_requests,
                total_errors=summary.total_errors,
                active_accounts=summary.active_accounts,
                total_conversations=summary.conversation_count,
                avg_cost_per_day=round(summary.total_cost_usd / day_count, 4),
                avg_requests_per_day=round(summary.total_requests / day_count, 2),
            ),
            comparison=comparison,
            daily=daily,
            by_model=[
                ModelCostEntry(
                    model=m.model,
                    cost_usd=round(m.cost_usd, 4),
                    requests=m.request_count,
                    percentage=round((m.cost_usd / model_total * 100), 1) if model_total > 0 else 0,
                )
                for m in by_model
            ],
            by_account=[
                AccountCostEntry(
                    account_id=a.account_id,
                    alias=a.alias,
                    cost_usd=round(a.cost_usd, 4),
                    requests=a.request_count,
                )
                for a in by_account
            ],
            by_useragent=[
                UserAgentCostEntry(
                    useragent=u.useragent_group,
                    cost_usd=round(u.cost_usd, 4),
                    requests=u.request_count,
                    percentage=round((u.cost_usd / useragent_total * 100), 1) if useragent_total > 0 else 0,
                )
                for u in by_useragent
            ],
        )

    async def get_usage_stats(
        self,
        range_key: str = "7d",
        report_timezone: str | None = None,
        metric: str = "tokens",
    ) -> UsageStatsResponse:
        if range_key not in USAGE_STATS_RANGES:
            raise ValueError(f"range must be one of {', '.join(USAGE_STATS_RANGES)}")
        if metric not in USAGE_STATS_METRICS:
            raise ValueError(f"metric must be one of {', '.join(USAGE_STATS_METRICS)}")

        timezone_info = _resolve_timezone(report_timezone)
        now = utcnow().replace(tzinfo=timezone.utc).astimezone(timezone_info)
        today = now.date()
        if range_key == "today":
            start_date = today
            end_date = today
            bucket = USAGE_BUCKET_HOUR
        elif range_key == "30d":
            start_date = today - timedelta(days=29)
            end_date = today
            bucket = USAGE_BUCKET_DAY
        else:
            start_date = today - timedelta(days=6)
            end_date = today
            bucket = USAGE_BUCKET_DAY

        start_at = _local_midnight_to_utc_naive(start_date, timezone_info)
        end_at = _local_midnight_to_utc_naive(end_date + timedelta(days=1), timezone_info)

        summary = await self._repository.aggregate_summary(start_at, end_at)
        model_rows = await self._repository.aggregate_usage_by_model(start_at, end_at)
        bucket_ranges = build_usage_bucket_ranges(start_date, end_date, timezone_info, bucket)
        # The window_credits view charts per-window bucket series and the
        # accounts view charts per-account bars, so the model bucket query
        # is skipped for both.
        series_rows = (
            []
            if metric in ("window_credits", "accounts")
            else await self._repository.aggregate_usage_by_model_bucket(bucket_ranges)
        )

        # Real subscription credit consumption (weekly window = the actual
        # budget), attributed from account usage-window snapshot deltas.
        credits_by_model: dict[str, float] = {}
        total_credits = 0.0
        attributed_requests = 0
        attributed_tokens_by_model: dict[str, int] = {}
        attributed_requests_by_model: dict[str, int] = {}
        credit_bucket_rows = []
        window_series: list[UsageWindowSeriesBucket] = []
        total_primary_credits = 0.0
        pool_primary_capacity = 0.0
        pool_secondary_capacity = 0.0
        account_credit_rows: list[CreditAccountAggregateRow] = []
        if self._credit_repository is not None:
            credit_rows = await self._credit_repository.aggregate_credits_by_model(start_at, end_at, CREDITS_WINDOW)
            credits_by_model = {row.model: row.credits_sum for row in credit_rows}
            total_credits = sum(credits_by_model.values())
            attributed_requests = sum(row.request_count for row in credit_rows)
            attributed_tokens_by_model = {row.model: row.input_tokens + row.output_tokens for row in credit_rows}
            attributed_requests_by_model = {row.model: row.request_count for row in credit_rows}
            if metric == "window_credits":
                window_series, total_primary_credits = await self._build_window_series(bucket_ranges)
                pool_primary_capacity, pool_secondary_capacity = await self._pool_window_capacities()
            elif metric == "accounts":
                account_credit_rows = await self._credit_repository.aggregate_credits_by_account(
                    start_at, end_at, CREDITS_WINDOW
                )
            else:
                credit_bucket_rows = await self._credit_repository.aggregate_credits_by_model_bucket(
                    bucket_ranges, CREDITS_WINDOW
                )

        accounts: list[UsageAccountEntry] = []
        if metric == "accounts":
            account_rows: list[UsageAccountAggregateRow] = await self._repository.aggregate_usage_by_account(
                start_at, end_at
            )
            usage_by_account = {row.account_id: row for row in account_rows}
            credits_by_account_id = {row.account_id: row.credits_sum for row in account_credit_rows}
            profiles = {account.id: account for account in await self._repository.list_accounts()}
            for account_id in usage_by_account.keys() | credits_by_account_id.keys():
                row = usage_by_account.get(account_id)
                profile = profiles.get(account_id)
                plan_type = profile.plan_type if profile else None
                # quota_percent divides by the plan's nominal weekly capacity.
                capacity = usage_core.capacity_for_plan(plan_type, "secondary") or 0.0
                credits = credits_by_account_id.get(account_id, 0.0)
                input_tokens = row.input_tokens if row else 0
                output_tokens = row.output_tokens if row else 0
                accounts.append(
                    UsageAccountEntry(
                        account_id=account_id,
                        name=(profile.alias or profile.email or account_id[:8]) if profile else account_id[:8],
                        plan_type=plan_type,
                        requests=row.requests if row else 0,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cached_input_tokens=row.cached_input_tokens if row else 0,
                        total_tokens=input_tokens + output_tokens,
                        cost_usd=round(row.cost_usd, 4) if row else 0.0,
                        credits=round(credits, 2),
                        capacity_credits=capacity,
                        quota_percent=round(credits / capacity * 100, 2) if capacity > 0 else 0.0,
                    )
                )
            accounts.sort(key=lambda entry: entry.total_tokens, reverse=True)
            # Reuse the window_credits capacity field: the accounts stat card
            # shows the charted accounts' total nominal weekly capacity.
            pool_secondary_capacity = sum(entry.capacity_credits for entry in accounts)

        # cached_input_tokens is a subset of input_tokens, so token totals
        # count input + output only — adding cached would double-count it.
        total_tokens = summary.total_input_tokens + summary.total_output_tokens
        total_cost_usd = sum(row.cost_usd for row in model_rows)
        all_models = {row.model for row in model_rows} | set(credits_by_model)

        metric_totals: dict[str, float] = {
            "tokens": total_tokens,
            "cost": total_cost_usd,
            "credits": total_credits,
            "window_credits": total_credits,
            "accounts": total_tokens,
        }
        metric_total = metric_totals[metric]

        def metric_value(model: str) -> float:
            if metric == "cost":
                return next((row.cost_usd for row in model_rows if row.model == model), 0.0)
            if metric in ("credits", "window_credits"):
                return credits_by_model.get(model, 0.0)
            return next(
                (row.input_tokens + row.output_tokens for row in model_rows if row.model == model),
                0,
            )

        by_model = [
            UsageModelEntry(
                model=model,
                requests=next((row.requests for row in model_rows if row.model == model), 0),
                input_tokens=next((row.input_tokens for row in model_rows if row.model == model), 0),
                output_tokens=next((row.output_tokens for row in model_rows if row.model == model), 0),
                cached_input_tokens=next((row.cached_input_tokens for row in model_rows if row.model == model), 0),
                total_tokens=next(
                    (row.input_tokens + row.output_tokens for row in model_rows if row.model == model),
                    0,
                ),
                percentage=round(metric_value(model) / metric_total * 100, 1) if metric_total > 0 else 0.0,
                cost_usd=round(next((row.cost_usd for row in model_rows if row.model == model), 0.0), 4),
                credits=round(credits_by_model.get(model, 0.0), 2),
                attributed_tokens=attributed_tokens_by_model.get(model, 0),
                attributed_requests=attributed_requests_by_model.get(model, 0),
            )
            for model in all_models
        ]
        by_model.sort(key=lambda entry: metric_value(entry.model), reverse=True)

        values_by_bucket: dict[str, dict[str, float]] = {}
        if metric == "credits":
            # Credit rows come from their own CTE over the same bucket ranges;
            # build the series directly from them so models without token
            # rows in a bucket are not lost.
            for row in credit_bucket_rows:
                if row.request_count == 0 and row.model == UNKNOWN_MODEL_BUCKET:
                    continue
                values_by_bucket.setdefault(row.bucket_key, {})[row.model] = row.credits_sum
        else:
            for row in series_rows:
                if row.requests == 0 and row.model == UNKNOWN_MODEL_BUCKET:
                    continue
                bucket_values = values_by_bucket.setdefault(row.bucket_key, {})
                if metric == "cost":
                    bucket_values[row.model] = row.cost_usd
                else:
                    bucket_values[row.model] = row.input_tokens + row.output_tokens

        series = [
            UsageSeriesBucket(
                bucket=bucket_key,
                label=bucket_label,
                values={model: round(value, 2) for model, value in values_by_bucket.get(bucket_key, {}).items()},
            )
            for bucket_key, bucket_label, _bucket_start, _bucket_end in bucket_ranges
        ]

        window_days = (end_date - start_date).days + 1
        return UsageStatsResponse(
            range=range_key,
            bucket=bucket,
            timezone=str(timezone_info),
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            summary=UsageStatsSummary(
                total_tokens=total_tokens,
                total_input_tokens=summary.total_input_tokens,
                total_output_tokens=summary.total_output_tokens,
                total_cached_tokens=summary.total_cached_tokens,
                total_requests=summary.total_requests,
                total_errors=summary.total_errors,
                model_count=len(by_model),
                avg_tokens_per_day=round(total_tokens / window_days, 1) if total_tokens > 0 else 0.0,
                total_cost_usd=round(total_cost_usd, 4),
                total_credits=round(total_credits, 2),
                total_primary_credits=round(total_primary_credits, 2),
                primary_capacity_credits=round(pool_primary_capacity, 2),
                secondary_capacity_credits=round(pool_secondary_capacity, 2),
                attributed_requests=attributed_requests,
                total_attributed_tokens=sum(attributed_tokens_by_model.values()),
                account_count=len(accounts),
            ),
            by_model=by_model,
            series=series,
            window_series=window_series,
            accounts=accounts,
            metric=metric,
        )

    async def _build_window_series(
        self,
        bucket_ranges: list[tuple[str, str, datetime, datetime]],
    ) -> tuple[list[UsageWindowSeriesBucket], float]:
        """Build per-bucket attributed credits for the 5h and weekly windows."""
        assert self._credit_repository is not None  # guarded by the caller
        credits_by_bucket: dict[str, dict[str, float]] = {window: {} for window in WINDOW_CREDITS_SERIES_WINDOWS}
        for window in WINDOW_CREDITS_SERIES_WINDOWS:
            rows = await self._credit_repository.aggregate_credits_by_model_bucket(bucket_ranges, window)
            for row in rows:
                bucket_totals = credits_by_bucket[window]
                bucket_totals[row.bucket_key] = bucket_totals.get(row.bucket_key, 0.0) + row.credits_sum
        series = [
            UsageWindowSeriesBucket(
                bucket=bucket_key,
                label=bucket_label,
                primary_credits=round(credits_by_bucket["primary"].get(bucket_key, 0.0), 2),
                secondary_credits=round(credits_by_bucket["secondary"].get(bucket_key, 0.0), 2),
            )
            for bucket_key, bucket_label, _bucket_start, _bucket_end in bucket_ranges
        ]
        total_primary = sum(bucket.primary_credits for bucket in series)
        return series, total_primary

    async def _pool_window_capacities(self) -> tuple[float, float]:
        """Pool quota capacities, mirroring the dashboard overview cards.

        Latest usage rows per account get the weekly-only remap and the
        nominal plan-capacity conversion the dashboard window summaries use,
        so the chart's capacity denominator matches the cards operators
        compare against.
        """
        if self._usage_repository is None:
            return 0.0, 0.0
        accounts = await self._repository.list_accounts()
        account_map = {account.id: account for account in accounts}
        primary_rows = [
            usage_history_to_window_row(entry)
            for entry in (await self._usage_repository.latest_by_account("primary")).values()
        ]
        secondary_rows = [
            usage_history_to_window_row(entry)
            for entry in (await self._usage_repository.latest_by_account("secondary")).values()
        ]
        primary_rows, secondary_rows = usage_core.normalize_weekly_only_rows(primary_rows, secondary_rows)
        primary_summary = usage_core.normalize_usage_window(
            usage_core.summarize_usage_window(primary_rows, account_map, "primary")
        )
        secondary_summary = usage_core.normalize_usage_window(
            usage_core.summarize_usage_window(secondary_rows, account_map, "secondary")
        )
        return primary_summary.capacity_credits, secondary_summary.capacity_credits


def _resolve_timezone(timezone_name: str | None) -> ZoneInfo | timezone:
    if not timezone_name:
        return timezone.utc
    try:
        return ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError):
        return timezone.utc


def _local_midnight_to_utc_naive(value: date, timezone_info: ZoneInfo | timezone) -> datetime:
    return to_utc_naive(datetime.combine(value, datetime.min.time(), tzinfo=timezone_info))
