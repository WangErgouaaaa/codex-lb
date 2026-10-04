from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from itertools import batched

from sqlalchemy import and_, func, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RequestCreditAttribution, RequestLog
from app.modules.reports.repository import _normal_traffic_clause

# SQLite caps compound SELECTs at 500 terms, so long bucket series are
# executed in chunks instead of building a single oversized UNION ALL.
_SQLITE_COMPOUND_SELECT_LIMIT = 500
UNKNOWN_MODEL_BUCKET = "unknown"
_VALID_WINDOWS = ("primary", "secondary")


@dataclass(frozen=True)
class CreditModelAggregateRow:
    model: str
    credits_sum: float
    request_count: int
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class CreditModelBucketRow:
    bucket_key: str
    bucket_label: str
    model: str
    credits_sum: float
    request_count: int


class CreditAttributionRepository:
    """Read-side aggregates over attributed real credit consumption.

    Pure repository functions (unit-testable SQL) for a future reports
    endpoint: they join ``request_credit_attributions`` to ``request_logs``
    with the SAME normal-traffic clause the reports module uses, so credit
    aggregates line up with the existing cost/token report views.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def aggregate_credits_by_model(
        self,
        start_at: datetime,
        end_at: datetime,
        window: str,
    ) -> list[CreditModelAggregateRow]:
        _validate_window(window)
        model_bucket = _credit_model_bucket_expr()
        # Attributed token sums mirror the reports module's semantics: output
        # falls back to reasoning, cached stays a subset of input (never added).
        attributed_output_tokens = func.coalesce(RequestLog.output_tokens, RequestLog.reasoning_tokens, 0)
        stmt = (
            select(
                model_bucket.label("model"),
                func.coalesce(func.sum(RequestCreditAttribution.credits), 0.0).label("credits_sum"),
                func.count().label("request_count"),
                func.coalesce(func.sum(RequestLog.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(attributed_output_tokens), 0).label("output_tokens"),
            )
            .select_from(RequestCreditAttribution)
            .join(RequestLog, RequestCreditAttribution.request_log_id == RequestLog.id)
            .where(
                RequestCreditAttribution.window == window,
                RequestLog.requested_at >= start_at,
                RequestLog.requested_at < end_at,
                _normal_traffic_clause(),
            )
            .group_by(model_bucket)
            .order_by(func.coalesce(func.sum(RequestCreditAttribution.credits), 0.0).desc())
        )
        result = await self._session.execute(stmt)
        return [
            CreditModelAggregateRow(
                model=row.model,
                credits_sum=float(row.credits_sum),
                request_count=int(row.request_count),
                input_tokens=int(row.input_tokens or 0),
                output_tokens=int(row.output_tokens or 0),
            )
            for row in result.all()
        ]

    async def aggregate_credits_by_model_bucket(
        self,
        bucket_ranges: list[tuple[str, str, datetime, datetime]],
        window: str,
    ) -> list[CreditModelBucketRow]:
        _validate_window(window)
        rows: list[CreditModelBucketRow] = []
        for bucket_ranges_batch in batched(bucket_ranges, _SQLITE_COMPOUND_SELECT_LIMIT):
            result = await self._session.execute(_credit_bucket_rows_stmt(list(bucket_ranges_batch), window))
            rows.extend(
                CreditModelBucketRow(
                    bucket_key=row.bucket_key,
                    bucket_label=row.bucket_label,
                    model=row.model,
                    credits_sum=float(row.credits_sum),
                    request_count=int(row.request_count),
                )
                for row in result.all()
            )
        return rows


def _validate_window(window: str) -> None:
    if window not in _VALID_WINDOWS:
        raise ValueError(f"window must be one of {_VALID_WINDOWS}, got {window!r}")


def _credit_model_bucket_expr():
    # Mirrors the reports module's usage-stats model bucket: blank models
    # collapse into a single "unknown" slice instead of disappearing.
    return func.coalesce(func.nullif(RequestLog.model, ""), literal(UNKNOWN_MODEL_BUCKET))


def _credit_bucket_ranges_cte(bucket_ranges: list[tuple[str, str, datetime, datetime]]):
    bucket_range_rows = [
        select(
            literal(bucket_key).label("bucket_key"),
            literal(bucket_label).label("bucket_label"),
            literal(bucket_start).label("bucket_start"),
            literal(bucket_end).label("bucket_end"),
        )
        for bucket_key, bucket_label, bucket_start, bucket_end in bucket_ranges
    ]
    bucket_ranges_query = bucket_range_rows[0] if len(bucket_range_rows) == 1 else union_all(*bucket_range_rows)
    return bucket_ranges_query.cte("credit_buckets")


def _credit_bucket_rows_stmt(bucket_ranges: list[tuple[str, str, datetime, datetime]], window: str):
    model_bucket = _credit_model_bucket_expr()
    bucket_ranges_cte = _credit_bucket_ranges_cte(bucket_ranges)
    # Pre-join attributions to request logs with every traffic filter applied;
    # the OUTER JOIN keeps buckets with no attributed credits so the series
    # stays continuous (same shape as the reports usage bucket query).
    credit_usage_cte = (
        select(
            RequestLog.requested_at.label("requested_at"),
            RequestLog.id.label("request_log_id"),
            model_bucket.label("model"),
            RequestCreditAttribution.credits.label("credits"),
        )
        .select_from(RequestCreditAttribution)
        .join(RequestLog, RequestCreditAttribution.request_log_id == RequestLog.id)
        .where(
            RequestCreditAttribution.window == window,
            _normal_traffic_clause(),
        )
        .cte("credit_usage")
    )
    return (
        select(
            bucket_ranges_cte.c.bucket_key,
            bucket_ranges_cte.c.bucket_label,
            # Empty buckets outer-join no credit rows and would group under a
            # NULL model; coalescing keeps them on the same "unknown" slice
            # label the reports usage query produces.
            func.coalesce(credit_usage_cte.c.model, literal(UNKNOWN_MODEL_BUCKET)).label("model"),
            func.coalesce(func.sum(credit_usage_cte.c.credits), 0.0).label("credits_sum"),
            func.count(credit_usage_cte.c.request_log_id).label("request_count"),
        )
        .select_from(
            bucket_ranges_cte.outerjoin(
                credit_usage_cte,
                and_(
                    credit_usage_cte.c.requested_at >= bucket_ranges_cte.c.bucket_start,
                    credit_usage_cte.c.requested_at < bucket_ranges_cte.c.bucket_end,
                ),
            )
        )
        .group_by(
            bucket_ranges_cte.c.bucket_key,
            bucket_ranges_cte.c.bucket_label,
            credit_usage_cte.c.model,
        )
        .order_by(bucket_ranges_cte.c.bucket_key, credit_usage_cte.c.model)
    )
