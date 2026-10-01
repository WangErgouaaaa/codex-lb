from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

import app.core.retention.job as retention_job
from app.core.config.settings import get_settings
from app.core.crypto import TokenEncryptor
from app.core.utils.time import utcnow
from app.db.models import Account, AccountStatus, RequestCreditAttribution, RequestLog, UsageWindowSnapshot
from app.db.session import SessionLocal
from app.modules.usage.credit_attribution import CreditAttributionEngine

pytestmark = pytest.mark.integration


def _make_account(account_id: str, email: str, *, plan_type: str | None = "plus") -> Account:
    encryptor = TokenEncryptor()
    return Account(
        id=account_id,
        email=email,
        plan_type=plan_type,
        access_token_encrypted=encryptor.encrypt("access"),
        refresh_token_encrypted=encryptor.encrypt("refresh"),
        id_token_encrypted=encryptor.encrypt("id"),
        last_refresh=utcnow(),
        status=AccountStatus.ACTIVE,
    )


def _snapshot(
    account_id: str,
    captured_at: datetime,
    *,
    primary: tuple[float, datetime] | None,
    secondary: tuple[float, datetime] | None,
) -> UsageWindowSnapshot:
    return UsageWindowSnapshot(
        account_id=account_id,
        captured_at=captured_at,
        source="response",
        primary_used_percent=primary[0] if primary else None,
        primary_reset_at=primary[1] if primary else None,
        secondary_used_percent=secondary[0] if secondary else None,
        secondary_reset_at=secondary[1] if secondary else None,
    )


def _request(account_id: str, request_id: str, requested_at: datetime, **tokens) -> RequestLog:
    return RequestLog(
        account_id=account_id,
        request_id=request_id,
        requested_at=requested_at,
        model="gpt-astra",
        status="success",
        **tokens,
    )


async def _attributions() -> list[RequestCreditAttribution]:
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(RequestCreditAttribution).order_by(
                        RequestCreditAttribution.request_log_id, RequestCreditAttribution.window
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            session.expunge(row)
        return list(rows)


async def _attribution_credits() -> dict[tuple[int, str], float]:
    return {(row.request_log_id, row.window): row.credits for row in await _attributions()}


async def test_attribution_splits_window_delta_by_token_weight(db_setup) -> None:
    del db_setup
    base = utcnow() - timedelta(minutes=30)
    t0 = base
    t1 = base + timedelta(minutes=1)
    primary_reset = base + timedelta(hours=3)
    secondary_reset = base + timedelta(days=5)
    async with SessionLocal() as session:
        session.add(_make_account("acc_attr_weight", "attr-weight@example.com"))
        request_a = _request(
            "acc_attr_weight",
            "attr-weight-a",
            t0 + timedelta(seconds=10),
            input_tokens=200,
            output_tokens=100,
        )
        request_b = _request(
            "acc_attr_weight",
            "attr-weight-b",
            t0 + timedelta(seconds=20),
            input_tokens=100,
        )
        session.add_all([request_a, request_b])
        session.add_all(
            [
                _snapshot(
                    "acc_attr_weight",
                    t0,
                    primary=(10.0, primary_reset),
                    secondary=(5.0, secondary_reset),
                ),
                _snapshot(
                    "acc_attr_weight",
                    t1,
                    primary=(20.0, primary_reset),
                    secondary=(7.0, secondary_reset),
                ),
            ]
        )
        await session.commit()
        request_a_id, request_b_id = request_a.id, request_b.id

    engine = CreditAttributionEngine()
    stats = await engine.run_pass()

    # plus plan: primary 225 credits, secondary 7560 credits.
    # primary: +10% -> 22.5 credits -> 300/400 and 100/400 shares.
    # secondary: +2% -> 151.2 credits -> same shares.
    credits = await _attribution_credits()
    assert credits == {
        (request_a_id, "primary"): pytest.approx(16.875),
        (request_b_id, "primary"): pytest.approx(5.625),
        (request_a_id, "secondary"): pytest.approx(113.4),
        (request_b_id, "secondary"): pytest.approx(37.8),
    }
    assert stats["attributions"] == 4
    rows = await _attributions()
    assert all(row.snapshot_id > 0 for row in rows)
    assert all(row.account_id == "acc_attr_weight" for row in rows)


async def test_window_reset_between_snapshots_is_not_attributed(db_setup) -> None:
    del db_setup
    base = utcnow() - timedelta(minutes=30)
    async with SessionLocal() as session:
        session.add(_make_account("acc_attr_reset", "attr-reset@example.com"))
        session.add(
            _request(
                "acc_attr_reset",
                "attr-reset-a",
                base + timedelta(seconds=10),
                input_tokens=100,
                output_tokens=50,
            )
        )
        session.add_all(
            [
                _snapshot(
                    "acc_attr_reset",
                    base,
                    primary=(10.0, base + timedelta(hours=3)),
                    secondary=None,
                ),
                # The window rolled over between snapshots: the percent
                # difference is meaningless and must not be attributed.
                _snapshot(
                    "acc_attr_reset",
                    base + timedelta(minutes=1),
                    primary=(30.0, base + timedelta(hours=9)),
                    secondary=None,
                ),
            ]
        )
        await session.commit()

    stats = await CreditAttributionEngine().run_pass()

    assert stats["attributions"] == 0
    assert await _attributions() == []


async def test_usage_drop_between_snapshots_is_not_attributed(db_setup) -> None:
    del db_setup
    base = utcnow() - timedelta(minutes=30)
    reset = base + timedelta(hours=3)
    async with SessionLocal() as session:
        session.add(_make_account("acc_attr_drop", "attr-drop@example.com"))
        session.add(
            _request(
                "acc_attr_drop",
                "attr-drop-a",
                base + timedelta(seconds=10),
                input_tokens=100,
            )
        )
        session.add_all(
            [
                _snapshot("acc_attr_drop", base, primary=(20.0, reset), secondary=None),
                _snapshot(
                    "acc_attr_drop",
                    base + timedelta(minutes=1),
                    primary=(15.0, reset),
                    secondary=None,
                ),
            ]
        )
        await session.commit()

    stats = await CreditAttributionEngine().run_pass()

    assert stats["attributions"] == 0
    assert await _attributions() == []


async def test_interval_uses_backdated_start_and_inclusive_end(db_setup) -> None:
    del db_setup
    base = utcnow() - timedelta(minutes=30)
    reset = base + timedelta(hours=3)
    async with SessionLocal() as session:
        session.add(_make_account("acc_attr_bounds", "attr-bounds@example.com"))
        # Inside the 30s backdate of the interval start.
        inside_backdate = _request(
            "acc_attr_bounds",
            "attr-bounds-back",
            base - timedelta(seconds=10),
            input_tokens=100,
        )
        # Outside the backdated start: not attributed.
        outside = _request(
            "acc_attr_bounds",
            "attr-bounds-out",
            base - timedelta(seconds=40),
            input_tokens=100,
        )
        # Exactly at the interval end (snapshot capture): inclusive.
        at_end = _request(
            "acc_attr_bounds",
            "attr-bounds-end",
            base + timedelta(minutes=1),
            input_tokens=100,
        )
        session.add_all([inside_backdate, outside, at_end])
        session.add_all(
            [
                _snapshot("acc_attr_bounds", base, primary=(10.0, reset), secondary=None),
                _snapshot(
                    "acc_attr_bounds",
                    base + timedelta(minutes=1),
                    primary=(20.0, reset),
                    secondary=None,
                ),
            ]
        )
        await session.commit()
        inside_id, at_end_id = inside_backdate.id, at_end.id

    await CreditAttributionEngine().run_pass()

    credits = await _attribution_credits()
    # Equal weights: the 22.5-credit delta splits across the two matched
    # requests (the backdated start admits the -10s request, the inclusive
    # end admits the request at capture time); the -40s request gets nothing.
    assert set(credits) == {(inside_id, "primary"), (at_end_id, "primary")}
    assert credits[(inside_id, "primary")] == pytest.approx(11.25)
    assert credits[(at_end_id, "primary")] == pytest.approx(11.25)


async def test_zero_token_weights_split_delta_equally(db_setup) -> None:
    del db_setup
    base = utcnow() - timedelta(minutes=30)
    reset = base + timedelta(hours=3)
    async with SessionLocal() as session:
        session.add(_make_account("acc_attr_equal", "attr-equal@example.com"))
        request_a = _request("acc_attr_equal", "attr-equal-a", base + timedelta(seconds=5))
        request_b = _request("acc_attr_equal", "attr-equal-b", base + timedelta(seconds=15))
        session.add_all([request_a, request_b])
        session.add_all(
            [
                _snapshot("acc_attr_equal", base, primary=(0.0, reset), secondary=None),
                _snapshot(
                    "acc_attr_equal",
                    base + timedelta(minutes=1),
                    primary=(10.0, reset),
                    secondary=None,
                ),
            ]
        )
        await session.commit()
        request_a_id, request_b_id = request_a.id, request_b.id

    await CreditAttributionEngine().run_pass()

    credits = await _attribution_credits()
    assert credits == {
        (request_a_id, "primary"): pytest.approx(11.25),
        (request_b_id, "primary"): pytest.approx(11.25),
    }


async def test_delta_without_requests_is_left_unattributed(db_setup) -> None:
    del db_setup
    base = utcnow() - timedelta(minutes=30)
    reset = base + timedelta(hours=3)
    async with SessionLocal() as session:
        session.add(_make_account("acc_attr_warm", "attr-warm@example.com"))
        session.add_all(
            [
                _snapshot("acc_attr_warm", base, primary=(10.0, reset), secondary=None),
                _snapshot(
                    "acc_attr_warm",
                    base + timedelta(minutes=1),
                    primary=(20.0, reset),
                    secondary=None,
                ),
            ]
        )
        await session.commit()

    stats = await CreditAttributionEngine().run_pass()

    assert stats["attributions"] == 0
    assert stats["unattributed_windows"] >= 1
    assert await _attributions() == []


async def test_unknown_plan_capacity_skips_attribution(db_setup) -> None:
    del db_setup
    base = utcnow() - timedelta(minutes=30)
    reset = base + timedelta(hours=3)
    async with SessionLocal() as session:
        # plan_type is NOT NULL; an unrecognized plan resolves to no capacity.
        session.add(_make_account("acc_attr_noplan", "attr-noplan@example.com", plan_type="mystery"))
        session.add(_request("acc_attr_noplan", "attr-noplan-a", base + timedelta(seconds=10), input_tokens=100))
        session.add_all(
            [
                _snapshot("acc_attr_noplan", base, primary=(10.0, reset), secondary=None),
                _snapshot(
                    "acc_attr_noplan",
                    base + timedelta(minutes=1),
                    primary=(20.0, reset),
                    secondary=None,
                ),
            ]
        )
        await session.commit()

    stats = await CreditAttributionEngine().run_pass()

    assert stats["attributions"] == 0
    assert await _attributions() == []


async def test_pass_is_idempotent_across_reruns_and_restart(db_setup) -> None:
    del db_setup
    base = utcnow() - timedelta(minutes=30)
    reset = base + timedelta(hours=3)
    async with SessionLocal() as session:
        session.add(_make_account("acc_attr_idem", "attr-idem@example.com"))
        session.add_all(
            [
                _request("acc_attr_idem", "attr-idem-a", base + timedelta(seconds=10), input_tokens=300),
                _request("acc_attr_idem", "attr-idem-b", base + timedelta(seconds=20), input_tokens=100),
            ]
        )
        session.add_all(
            [
                _snapshot("acc_attr_idem", base, primary=(10.0, reset), secondary=None),
                _snapshot(
                    "acc_attr_idem",
                    base + timedelta(minutes=1),
                    primary=(20.0, reset),
                    secondary=None,
                ),
            ]
        )
        await session.commit()

    engine = CreditAttributionEngine()
    await engine.run_pass()
    first = await _attribution_credits()
    await engine.run_pass()
    second = await _attribution_credits()

    # A fresh engine mimics a restart: the ~2h reprocess window overlaps the
    # same pairs, and the unique (request, window) key must keep rows stable.
    await CreditAttributionEngine().run_pass()
    third = await _attribution_credits()

    assert first == second == third
    assert len(first) == 2
    assert sum(first.values()) == pytest.approx(22.5)


async def test_second_pass_only_processes_new_snapshots(db_setup) -> None:
    del db_setup
    base = utcnow() - timedelta(minutes=30)
    reset = base + timedelta(hours=3)
    async with SessionLocal() as session:
        session.add(_make_account("acc_attr_chain", "attr-chain@example.com"))
        # request_a falls into the first pair's interval, request_b into the
        # second pair's (they do not overlap past the 30s backdate).
        session.add_all(
            [
                _request("acc_attr_chain", "attr-chain-a", base + timedelta(seconds=10), input_tokens=100),
                _request("acc_attr_chain", "attr-chain-b", base + timedelta(seconds=75), input_tokens=100),
            ]
        )
        session.add_all(
            [
                _snapshot("acc_attr_chain", base, primary=(10.0, reset), secondary=None),
                _snapshot(
                    "acc_attr_chain",
                    base + timedelta(minutes=1),
                    primary=(20.0, reset),
                    secondary=None,
                ),
            ]
        )
        await session.commit()
        request_a_id = (
            await session.execute(select(RequestLog.id).where(RequestLog.request_id == "attr-chain-a"))
        ).scalar_one()
        request_b_id = (
            await session.execute(select(RequestLog.id).where(RequestLog.request_id == "attr-chain-b"))
        ).scalar_one()

    engine = CreditAttributionEngine()
    await engine.run_pass()
    async with SessionLocal() as session:
        session.add(
            _snapshot(
                "acc_attr_chain",
                base + timedelta(minutes=2),
                primary=(25.0, reset),
                secondary=None,
            )
        )
        await session.commit()

    stats = await engine.run_pass()

    assert stats["attributions"] == 1
    credits = await _attribution_credits()
    # First pair: 10%->20% = 22.5 credits to request_a. Second pair: 20%->25%
    # = 11.25 credits to request_b. The first pair's row is untouched.
    assert credits == {
        (request_a_id, "primary"): pytest.approx(22.5),
        (request_b_id, "primary"): pytest.approx(11.25),
    }
    assert sum(credits.values()) == pytest.approx(33.75)


async def test_retention_prunes_snapshots_on_request_log_window(monkeypatch, db_setup) -> None:
    del db_setup
    now = utcnow()
    async with SessionLocal() as session:
        session.add(_make_account("acc_attr_keep", "attr-keep@example.com"))
        session.add_all(
            [
                _snapshot(
                    "acc_attr_keep",
                    now - timedelta(days=400),
                    primary=(10.0, now - timedelta(days=397)),
                    secondary=None,
                ),
                _snapshot(
                    "acc_attr_keep",
                    now - timedelta(days=1),
                    primary=(20.0, now + timedelta(hours=3)),
                    secondary=None,
                ),
            ]
        )
        await session.commit()
    monkeypatch.setenv("CODEX_LB_REQUEST_LOG_RETENTION_DAYS", "365")
    monkeypatch.setenv("CODEX_LB_USAGE_HISTORY_RETENTION_DAYS", "0")
    get_settings.cache_clear()
    try:
        deleted = await retention_job.run_retention_pass()
    finally:
        get_settings.cache_clear()

    assert deleted["usage_window_snapshots"] == 1
    async with SessionLocal() as session:
        remaining = (await session.execute(select(func.count()).select_from(UsageWindowSnapshot))).scalar_one()
    assert remaining == 1
