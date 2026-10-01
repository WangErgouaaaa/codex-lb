from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.crypto import TokenEncryptor
from app.db.models import Account, AccountStatus, RequestLog
from app.db.session import SessionLocal

pytestmark = pytest.mark.integration


def _make_account(account_id: str, email: str) -> Account:
    encryptor = TokenEncryptor()
    return Account(
        id=account_id,
        email=email,
        plan_type="plus",
        access_token_encrypted=encryptor.encrypt("access"),
        refresh_token_encrypted=encryptor.encrypt("refresh"),
        id_token_encrypted=encryptor.encrypt("id"),
        last_refresh=datetime.now(timezone.utc).replace(tzinfo=None),
        status=AccountStatus.ACTIVE,
        deactivation_reason=None,
    )


def _naive_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=None)


async def test_usage_stats_api_returns_hourly_model_series_for_today(async_client, db_setup, monkeypatch):
    fixed_now = datetime(2026, 6, 12, 15, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("app.modules.reports.service.utcnow", lambda: fixed_now)
    async with SessionLocal() as session:
        session.add(_make_account("acc_usage_stats", "usage-stats@example.com"))
        session.add_all(
            [
                RequestLog(
                    account_id="acc_usage_stats",
                    request_id="usage-stats-astra-a",
                    requested_at=_naive_utc(datetime(2026, 6, 12, 10, 5, 0, tzinfo=timezone.utc)),
                    model="gpt-astra",
                    status="success",
                    input_tokens=1000,
                    output_tokens=200,
                    cached_input_tokens=300,
                    cost_usd=0.5,
                ),
                RequestLog(
                    account_id="acc_usage_stats",
                    request_id="usage-stats-astra-b",
                    requested_at=_naive_utc(datetime(2026, 6, 12, 10, 45, 0, tzinfo=timezone.utc)),
                    model="gpt-astra",
                    status="success",
                    input_tokens=100,
                    output_tokens=20,
                    cached_input_tokens=30,
                    cost_usd=0.05,
                ),
                RequestLog(
                    account_id="acc_usage_stats",
                    request_id="usage-stats-sol",
                    requested_at=_naive_utc(datetime(2026, 6, 12, 11, 10, 0, tzinfo=timezone.utc)),
                    model="gpt-sol",
                    status="error",
                    error_code="upstream_error",
                    input_tokens=50,
                    output_tokens=10,
                    cached_input_tokens=0,
                    cost_usd=0.0,
                ),
                RequestLog(
                    account_id="acc_usage_stats",
                    request_id="usage-stats-warmup",
                    requested_at=_naive_utc(datetime(2026, 6, 12, 10, 20, 0, tzinfo=timezone.utc)),
                    model="gpt-astra",
                    status="success",
                    input_tokens=9999,
                    output_tokens=9999,
                    cached_input_tokens=0,
                    cost_usd=9.0,
                    source="limit_warmup",
                ),
                RequestLog(
                    account_id="acc_usage_stats",
                    request_id="usage-stats-blank-model",
                    requested_at=_naive_utc(datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)),
                    model="",
                    status="success",
                    input_tokens=7,
                    output_tokens=3,
                    cached_input_tokens=0,
                    cost_usd=0.0,
                ),
            ]
        )
        await session.commit()

    response = await async_client.get(
        "/api/reports/usage-stats",
        params={"range": "today", "timezone": "UTC"},
    )
    assert response.status_code == 200

    payload = response.json()
    assert payload["range"] == "today"
    assert payload["bucket"] == "hour"
    assert payload["startDate"] == "2026-06-12"
    assert payload["endDate"] == "2026-06-12"
    assert payload["timezone"] == "UTC"

    series = payload["series"]
    assert len(series) == 24
    assert series[0]["bucket"] == "2026-06-12T00"
    assert series[0]["label"] == "00:00"
    assert series[10]["bucket"] == "2026-06-12T10"
    # gpt-astra at 10:00 UTC: (1000+200+300) + (100+20+30) = 1650 tokens.
    assert series[10]["values"] == {"gpt-astra": 1650}
    assert series[11]["values"] == {"gpt-sol": 60}
    assert series[12]["values"] == {"unknown": 10}
    # Buckets without traffic stay present but empty.
    assert series[0]["values"] == {}

    summary = payload["summary"]
    assert summary["totalRequests"] == 4
    assert summary["totalErrors"] == 1
    assert summary["totalInputTokens"] == 1157
    assert summary["totalOutputTokens"] == 233
    assert summary["totalCachedTokens"] == 330
    assert summary["totalTokens"] == 1720
    assert summary["modelCount"] == 3
    assert summary["avgTokensPerDay"] == 1720.0

    by_model = payload["byModel"]
    assert [entry["model"] for entry in by_model] == ["gpt-astra", "gpt-sol", "unknown"]
    assert by_model[0]["totalTokens"] == 1650
    assert by_model[0]["percentage"] == 95.9
    assert by_model[0]["requests"] == 2
    assert by_model[2]["model"] == "unknown"
    assert by_model[2]["totalTokens"] == 10


async def test_usage_stats_api_returns_daily_series_for_7d(async_client, db_setup, monkeypatch):
    fixed_now = datetime(2026, 6, 12, 15, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("app.modules.reports.service.utcnow", lambda: fixed_now)
    async with SessionLocal() as session:
        session.add(_make_account("acc_usage_stats_7d", "usage-stats-7d@example.com"))
        session.add_all(
            [
                RequestLog(
                    account_id="acc_usage_stats_7d",
                    request_id="usage-stats-7d-edge-start",
                    requested_at=_naive_utc(datetime(2026, 6, 6, 0, 0, 0, tzinfo=timezone.utc)),
                    model="gpt-sol",
                    status="success",
                    input_tokens=10,
                    output_tokens=5,
                    cached_input_tokens=0,
                    cost_usd=0.0,
                ),
                RequestLog(
                    account_id="acc_usage_stats_7d",
                    request_id="usage-stats-7d-day-before",
                    requested_at=_naive_utc(datetime(2026, 6, 5, 23, 59, 59, tzinfo=timezone.utc)),
                    model="gpt-sol",
                    status="success",
                    input_tokens=999,
                    output_tokens=999,
                    cached_input_tokens=0,
                    cost_usd=9.0,
                ),
                RequestLog(
                    account_id="acc_usage_stats_7d",
                    request_id="usage-stats-7d-edge-end",
                    requested_at=_naive_utc(datetime(2026, 6, 12, 23, 0, 0, tzinfo=timezone.utc)),
                    model="gpt-astra",
                    status="success",
                    input_tokens=20,
                    output_tokens=8,
                    cached_input_tokens=2,
                    cost_usd=0.0,
                ),
            ]
        )
        await session.commit()

    response = await async_client.get(
        "/api/reports/usage-stats",
        params={"range": "7d", "timezone": "UTC"},
    )
    assert response.status_code == 200

    payload = response.json()
    assert payload["bucket"] == "day"
    assert payload["startDate"] == "2026-06-06"
    assert payload["endDate"] == "2026-06-12"

    series = payload["series"]
    assert len(series) == 7
    assert series[0]["bucket"] == "2026-06-06"
    assert series[0]["label"] == "06-06"
    assert series[0]["values"] == {"gpt-sol": 15}
    assert series[6]["values"] == {"gpt-astra": 30}
    assert payload["summary"]["totalTokens"] == 45
    assert payload["summary"]["avgTokensPerDay"] == 6.4


async def test_usage_stats_api_interprets_today_in_requested_timezone(async_client, db_setup, monkeypatch):
    fixed_now = datetime(2026, 6, 12, 2, 0, 0, tzinfo=timezone.utc)  # 10:00 Asia/Shanghai
    monkeypatch.setattr("app.modules.reports.service.utcnow", lambda: fixed_now)
    async with SessionLocal() as session:
        session.add(_make_account("acc_usage_stats_tz", "usage-stats-tz@example.com"))
        session.add_all(
            [
                RequestLog(
                    account_id="acc_usage_stats_tz",
                    request_id="usage-stats-tz-local-morning",
                    # 2026-06-12 09:30 Asia/Shanghai == 2026-06-12 01:30 UTC.
                    requested_at=_naive_utc(datetime(2026, 6, 12, 1, 30, 0, tzinfo=timezone.utc)),
                    model="gpt-sol",
                    status="success",
                    input_tokens=40,
                    output_tokens=10,
                    cached_input_tokens=0,
                    cost_usd=0.0,
                ),
                RequestLog(
                    account_id="acc_usage_stats_tz",
                    request_id="usage-stats-tz-previous-local-day",
                    # 2026-06-11 22:00 Asia/Shanghai == 2026-06-11 14:00 UTC: outside today's window.
                    requested_at=_naive_utc(datetime(2026, 6, 11, 14, 0, 0, tzinfo=timezone.utc)),
                    model="gpt-sol",
                    status="success",
                    input_tokens=999,
                    output_tokens=999,
                    cached_input_tokens=0,
                    cost_usd=9.0,
                ),
            ]
        )
        await session.commit()

    response = await async_client.get(
        "/api/reports/usage-stats",
        params={"range": "today", "timezone": "Asia/Shanghai"},
    )
    assert response.status_code == 200

    payload = response.json()
    assert payload["timezone"] == "Asia/Shanghai"
    assert payload["summary"]["totalTokens"] == 50
    # 09:00-10:00 local bucket holds the morning request.
    assert payload["series"][9]["values"] == {"gpt-sol": 50}


async def test_usage_stats_api_rejects_unknown_range(async_client, db_setup):
    response = await async_client.get(
        "/api/reports/usage-stats",
        params={"range": "yesterday"},
    )
    assert response.status_code == 422


async def test_usage_stats_api_defaults_to_7d(async_client, db_setup, monkeypatch):
    fixed_now = datetime(2026, 6, 12, 15, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("app.modules.reports.service.utcnow", lambda: fixed_now)

    response = await async_client.get("/api/reports/usage-stats")

    assert response.status_code == 200
    payload = response.json()
    assert payload["range"] == "7d"
    assert payload["bucket"] == "day"
    assert len(payload["series"]) == 7
    assert payload["summary"]["totalRequests"] == 0
    assert payload["byModel"] == []
