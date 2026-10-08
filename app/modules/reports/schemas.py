from __future__ import annotations

from pydantic import Field

from app.modules.shared.schemas import DashboardModel


class DailyReportRow(DashboardModel):
    date: str
    requests: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost_usd: float
    active_accounts: int
    conversations: int = 0
    error_count: int = 0
    median_ttft_ms: float = 0.0
    median_tps: float = 0.0
    median_queue_ms: float = 0.0


class ModelCostEntry(DashboardModel):
    model: str
    cost_usd: float
    requests: int = 0
    percentage: float = 0.0


class AccountCostEntry(DashboardModel):
    account_id: str | None
    alias: str | None = None
    cost_usd: float = 0.0
    requests: int = 0


class UserAgentCostEntry(DashboardModel):
    useragent: str
    cost_usd: float = 0.0
    requests: int = 0
    percentage: float = 0.0


class ReportSummary(DashboardModel):
    total_cost_usd: float
    total_input_tokens: int
    total_output_tokens: int
    total_cached_tokens: int
    total_requests: int
    total_errors: int
    active_accounts: int
    total_conversations: int = 0
    avg_cost_per_day: float = 0.0
    avg_requests_per_day: float = 0.0


class ReportComparisonPrevious(DashboardModel):
    total_cost_usd: float
    total_tokens: int
    total_requests: int


class ReportComparison(DashboardModel):
    can_compare: bool
    previous: ReportComparisonPrevious


class ReportsResponse(DashboardModel):
    summary: ReportSummary
    comparison: ReportComparison
    daily: list[DailyReportRow] = Field(default_factory=list)
    by_model: list[ModelCostEntry] = Field(default_factory=list)
    by_account: list[AccountCostEntry] = Field(default_factory=list)
    by_useragent: list[UserAgentCostEntry] = Field(default_factory=list)


class UsageStatsSummary(DashboardModel):
    total_tokens: int
    total_input_tokens: int
    total_output_tokens: int
    total_cached_tokens: int
    total_requests: int
    total_errors: int
    model_count: int
    avg_tokens_per_day: float
    total_cost_usd: float = 0.0
    total_credits: float = 0.0
    attributed_requests: int = 0
    total_attributed_tokens: int = 0
    total_primary_credits: float = 0.0
    primary_capacity_credits: float = 0.0
    secondary_capacity_credits: float = 0.0
    account_count: int = 0


class UsageModelEntry(DashboardModel):
    model: str
    requests: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    total_tokens: int
    percentage: float
    cost_usd: float = 0.0
    credits: float = 0.0
    attributed_tokens: int = 0
    attributed_requests: int = 0


class UsageAccountEntry(DashboardModel):
    account_id: str
    name: str
    plan_type: str | None = None
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    credits: float = 0.0
    capacity_credits: float = 0.0
    quota_percent: float = 0.0

class UsageSeriesBucket(DashboardModel):
    bucket: str
    label: str
    values: dict[str, float]


class UsageWindowSeriesBucket(DashboardModel):
    """Per-bucket attributed credits for each quota window.

    ``primary`` is the 5-hour window and ``secondary`` the weekly window;
    values are raw credits so clients can render share-of-range percentages.
    """

    bucket: str
    label: str
    primary_credits: float = 0.0
    secondary_credits: float = 0.0


class UsageStatsResponse(DashboardModel):
    range: str
    bucket: str
    timezone: str
    start_date: str
    end_date: str
    summary: UsageStatsSummary
    by_model: list[UsageModelEntry] = Field(default_factory=list)
    series: list[UsageSeriesBucket] = Field(default_factory=list)
    window_series: list[UsageWindowSeriesBucket] = Field(default_factory=list)
    accounts: list[UsageAccountEntry] = Field(default_factory=list)
    metric: str = "tokens"
