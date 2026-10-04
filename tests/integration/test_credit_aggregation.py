from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.crypto import TokenEncryptor
from app.db.models import Account, AccountStatus, RequestCreditAttribution, RequestLog
from app.db.session import SessionLocal
from app.modules.usage.credit_aggregation import CreditAttributionRepository

pytestmark = pytest.mark.integration


def _naive_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=None)


def _hour(value: datetime) -> tuple[str, str, datetime, datetime]:
    naive = _naive_utc(value).replace(minute=0, second=0, microsecond=0)
    key = f"{naive.date().isoformat()}T{naive.hour:02d}"
    return key, f"{naive.hour:02d}:00", naive, naive.replace(hour=naive.hour + 1)


async def _seed_traffic(db_setup) -> dict[str, int]:
    del db_setup
    fixed_now = datetime(2026, 6, 12, 10, 30, 0, tzinfo=timezone.utc)
    encryptor = TokenEncryptor()
    async with SessionLocal() as session:
        session.add(
            Account(
                id="acc_credit_agg",
                email="credit-agg@example.com",
                plan_type="plus",
                access_token_encrypted=encryptor.encrypt("access"),
                refresh_token_encrypted=encryptor.encrypt("refresh"),
                id_token_encrypted=encryptor.encrypt("id"),
                last_refresh=_naive_utc(fixed_now),
                status=AccountStatus.ACTIVE,
            )
        )
        normal = RequestLog(
            account_id="acc_credit_agg",
            request_id="credit-agg-normal",
            requested_at=_naive_utc(fixed_now),
            model="gpt-astra",
            status="success",
            input_tokens=100,
            output_tokens=40,
            cached_input_tokens=25,
        )
        warmup = RequestLog(
            account_id="acc_credit_agg",
            request_id="credit-agg-warmup",
            requested_at=_naive_utc(fixed_now),
            model="gpt-astra",
            status="success",
            source="limit_warmup",
            input_tokens=999,
        )
        blank_model = RequestLog(
            account_id="acc_credit_agg",
            request_id="credit-agg-blank",
            requested_at=_naive_utc(fixed_now),
            model="",
            status="success",
            input_tokens=10,
            # No output count: the attributed output sum falls back to
            # reasoning tokens, mirroring the reports module's semantics.
            reasoning_tokens=50,
        )
        session.add_all([normal, warmup, blank_model])
        await session.commit()
        session.add_all(
            [
                RequestCreditAttribution(
                    request_log_id=normal.id,
                    account_id="acc_credit_agg",
                    window="primary",
                    credits=5.0,
                    snapshot_id=1,
                    attributed_at=_naive_utc(fixed_now),
                ),
                # Warmup traffic is attributed (it consumes credits) but the
                # aggregate views exclude it like every other report view.
                RequestCreditAttribution(
                    request_log_id=warmup.id,
                    account_id="acc_credit_agg",
                    window="primary",
                    credits=3.0,
                    snapshot_id=1,
                    attributed_at=_naive_utc(fixed_now),
                ),
                RequestCreditAttribution(
                    request_log_id=blank_model.id,
                    account_id="acc_credit_agg",
                    window="primary",
                    credits=2.0,
                    snapshot_id=1,
                    attributed_at=_naive_utc(fixed_now),
                ),
                RequestCreditAttribution(
                    request_log_id=normal.id,
                    account_id="acc_credit_agg",
                    window="secondary",
                    credits=7.0,
                    snapshot_id=1,
                    attributed_at=_naive_utc(fixed_now),
                ),
            ]
        )
        await session.commit()
        return {
            "normal": normal.id,
            "warmup": warmup.id,
            "blank": blank_model.id,
            "bucket": _hour(fixed_now),
        }


async def test_aggregate_credits_by_model_excludes_warmup_and_filters_window(db_setup) -> None:
    seeded = await _seed_traffic(db_setup)
    del seeded
    start = datetime(2026, 6, 12, 0, 0, 0)
    end = datetime(2026, 6, 13, 0, 0, 0)
    async with SessionLocal() as session:
        repo = CreditAttributionRepository(session)
        primary_rows = await repo.aggregate_credits_by_model(start, end, "primary")
        secondary_rows = await repo.aggregate_credits_by_model(start, end, "secondary")

    assert [(row.model, row.credits_sum, row.request_count) for row in primary_rows] == [
        ("gpt-astra", pytest.approx(5.0), 1),
        ("unknown", pytest.approx(2.0), 1),
    ]
    assert [(row.model, row.credits_sum, row.request_count) for row in secondary_rows] == [
        ("gpt-astra", pytest.approx(7.0), 1)
    ]

    # Attributed token sums follow the same filters: warmup is excluded, the
    # blank model lands on "unknown", cached stays inside input (not added),
    # and a missing output count falls back to reasoning tokens.
    assert [(row.model, row.input_tokens, row.output_tokens) for row in primary_rows] == [
        ("gpt-astra", 100, 40),
        ("unknown", 10, 50),
    ]
    assert [(row.model, row.input_tokens, row.output_tokens) for row in secondary_rows] == [("gpt-astra", 100, 40)]


async def test_aggregate_credits_by_model_bucket_keeps_empty_buckets(db_setup) -> None:
    seeded = await _seed_traffic(db_setup)
    busy_bucket = seeded["bucket"]
    empty_hour = datetime(2026, 6, 12, 23, 30, 0)
    empty_bucket = (
        f"{empty_hour.date().isoformat()}T23",
        "23:00",
        datetime(2026, 6, 12, 23, 0, 0),
        datetime(2026, 6, 13, 0, 0, 0),
    )
    async with SessionLocal() as session:
        repo = CreditAttributionRepository(session)
        rows = await repo.aggregate_credits_by_model_bucket([busy_bucket, empty_bucket], "primary")

    assert [(row.bucket_key, row.model, row.credits_sum, row.request_count) for row in rows] == [
        (busy_bucket[0], "gpt-astra", pytest.approx(5.0), 1),
        (busy_bucket[0], "unknown", pytest.approx(2.0), 1),
        (empty_bucket[0], "unknown", 0.0, 0),
    ]


async def test_aggregate_credits_rejects_unknown_window(db_setup) -> None:
    del db_setup
    async with SessionLocal() as session:
        repo = CreditAttributionRepository(session)
        with pytest.raises(ValueError, match="window"):
            await repo.aggregate_credits_by_model(datetime(2026, 6, 12), datetime(2026, 6, 13), "monthly")
        with pytest.raises(ValueError, match="window"):
            await repo.aggregate_credits_by_model_bucket([], "monthly")


async def test_aggregate_credits_returns_empty_without_attributions(db_setup) -> None:
    del db_setup
    async with SessionLocal() as session:
        repo = CreditAttributionRepository(session)
        assert await repo.aggregate_credits_by_model(datetime(2026, 6, 12), datetime(2026, 6, 13), "primary") == []
        rows = await repo.aggregate_credits_by_model_bucket(
            [("b", "label", datetime(2026, 6, 12), datetime(2026, 6, 13))], "primary"
        )
    assert [(row.bucket_key, row.model, row.credits_sum, row.request_count) for row in rows] == [
        ("b", "unknown", 0.0, 0)
    ]
