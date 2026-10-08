from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.auth.dependencies import (
    set_dashboard_error_format,
    validate_dashboard_session,
)
from app.core.exceptions import DashboardBadRequestError
from app.dependencies import ReportsContext, get_reports_context
from app.modules.reports.repository import DailyReportRangeTooLargeError
from app.modules.reports.schemas import ReportsResponse, UsageStatsResponse
from app.modules.reports.service import InvalidReportDateRangeError

router = APIRouter(
    prefix="/api/reports",
    tags=["dashboard"],
    dependencies=[Depends(validate_dashboard_session), Depends(set_dashboard_error_format)],
)


@router.get("", response_model=ReportsResponse)
async def get_reports(
    context: ReportsContext = Depends(get_reports_context),
    start_date: Annotated[date | None, Query()] = None,
    end_date: Annotated[date | None, Query()] = None,
    report_timezone: Annotated[str | None, Query(alias="timezone")] = None,
    account_id: Annotated[list[str] | None, Query()] = None,
    model: Annotated[str | None, Query()] = None,
    useragent_group: Annotated[str | None, Query()] = None,
) -> ReportsResponse:
    try:
        return await context.service.get_reports(
            start_date=start_date,
            end_date=end_date,
            report_timezone=report_timezone,
            account_ids=account_id,
            model=model,
            useragent_group=useragent_group,
        )
    except InvalidReportDateRangeError as exc:
        raise DashboardBadRequestError(str(exc), code="invalid_report_date_range") from exc
    except DailyReportRangeTooLargeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/usage-stats", response_model=UsageStatsResponse)
async def get_usage_stats(
    context: ReportsContext = Depends(get_reports_context),
    range: Annotated[Literal["today", "7d", "30d"], Query()] = "7d",
    metric: Annotated[
        Literal["tokens", "cost", "credits", "window_credits", "accounts"], Query()
    ] = "tokens",
    report_timezone: Annotated[str | None, Query(alias="timezone")] = None,
) -> UsageStatsResponse:
    return await context.service.get_usage_stats(
        range_key=range,
        report_timezone=report_timezone,
        metric=metric,
    )
