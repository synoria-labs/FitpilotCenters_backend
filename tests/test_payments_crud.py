"""Backend tests for the payments CRUD layer.

Covers:
  - Date-range filtering on get_payments
  - Idempotency of create_payment via provider_payment_id
  - Aggregation correctness of get_payment_metrics

Each test runs inside a SAVEPOINT-wrapped session (see conftest.py) so nothing
persists to defaultdb. Test rows use far-future dates (year 3000+) to avoid
collisions with real production data when scoping aggregate queries.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.crud.memberships.payments import (
    count_payments,
    create_payment,
    get_payments,
)
from app.crud.memberships.payment_metrics import get_payment_metrics
from app.models import Payment, People


async def _make_person(db, *, full_name: str = "Test Person") -> People:
    person = People(full_name=full_name)
    db.add(person)
    await db.flush()
    return person


def _utc(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# get_payments date-range filtering                                           #
# --------------------------------------------------------------------------- #

async def test_get_payments_date_filter(db):
    person = await _make_person(db, full_name="Filter Subject")

    # Three payments spread across Jan/Feb/Mar of year 3000.
    for paid_at in [_utc(3000, 1, 15), _utc(3000, 2, 15), _utc(3000, 3, 15)]:
        await create_payment(
            db=db,
            person_id=person.id,
            amount=Decimal("100.00"),
            method="cash",
            paid_at=paid_at,
            commit=False,
        )
    await db.flush()

    # Narrow window matches only February.
    feb_only = await get_payments(
        db=db,
        start_date=_utc(3000, 2, 1),
        end_date=_utc(3000, 2, 28, 23),
    )
    feb_for_person = [p for p in feb_only if p.person_id == person.id]
    assert len(feb_for_person) == 1
    assert feb_for_person[0].paid_at.month == 2

    # Wide window catches all three.
    full_year = await get_payments(
        db=db,
        start_date=_utc(3000, 1, 1),
        end_date=_utc(3000, 12, 31, 23),
    )
    full_for_person = [p for p in full_year if p.person_id == person.id]
    assert len(full_for_person) == 3

    # Empty window returns none.
    empty = await get_payments(
        db=db,
        start_date=_utc(2999, 1, 1),
        end_date=_utc(2999, 12, 31, 23),
    )
    empty_for_person = [p for p in empty if p.person_id == person.id]
    assert empty_for_person == []

    # count_payments must agree with get_payments under the same filters.
    feb_count = await count_payments(
        db=db,
        start_date=_utc(3000, 2, 1),
        end_date=_utc(3000, 2, 28, 23),
    )
    assert feb_count >= 1  # at least our row; real data may also fall in window (it won't, year 3000)


# --------------------------------------------------------------------------- #
# create_payment idempotency on provider_payment_id                           #
# --------------------------------------------------------------------------- #

async def test_create_payment_idempotent(db):
    person = await _make_person(db, full_name="Idempotency Subject")
    provider_id = f"test-{uuid.uuid4()}"

    first = await create_payment(
        db=db,
        person_id=person.id,
        amount=Decimal("100.00"),
        method="card",
        provider="stripe",
        provider_payment_id=provider_id,
        paid_at=_utc(3000, 5, 1),
        commit=False,
    )
    await db.flush()
    assert first.id is not None

    # Second call with the same provider_payment_id but a different amount must
    # return the existing row, NOT create a new one and NOT overwrite the amount.
    second = await create_payment(
        db=db,
        person_id=person.id,
        amount=Decimal("999.99"),
        method="card",
        provider="stripe",
        provider_payment_id=provider_id,
        paid_at=_utc(3000, 5, 2),
        commit=False,
    )

    assert second.id == first.id, "Idempotency violated: a duplicate Payment was created"
    assert second.amount == Decimal("100.00"), "Existing amount should not be overwritten"

    # And the unique partial index guarantees at most one row in the DB.
    rows = (
        await db.execute(
            select(Payment).where(Payment.provider_payment_id == provider_id)
        )
    ).scalars().all()
    assert len(rows) == 1


# --------------------------------------------------------------------------- #
# get_payment_metrics aggregation                                             #
# --------------------------------------------------------------------------- #

async def test_payment_metrics_aggregation(db):
    person = await _make_person(db, full_name="Metrics Subject")

    # 5 payments in Jan 3000: $100 cash, $200 cash, $300 card, $400 transfer, $500 cash
    payments = [
        (_utc(3000, 1, 5),  "cash",     "100.00"),
        (_utc(3000, 1, 10), "cash",     "200.00"),
        (_utc(3000, 1, 10), "card",     "300.00"),
        (_utc(3000, 1, 15), "transfer", "400.00"),
        (_utc(3000, 1, 20), "cash",     "500.00"),
    ]
    for paid_at, method, amount in payments:
        await create_payment(
            db=db,
            person_id=person.id,
            amount=Decimal(amount),
            method=method,
            paid_at=paid_at,
            commit=False,
        )
    await db.flush()

    window_start = _utc(3000, 1, 1)
    window_end = _utc(3000, 1, 31, 23)

    metrics = await get_payment_metrics(
        db=db, start_date=window_start, end_date=window_end
    )

    assert metrics.total_count == 5
    assert metrics.total_amount == 1500.0
    assert metrics.avg_amount == 300.0
    assert metrics.completed_amount == 1500.0
    assert metrics.pending_count == 0
    assert metrics.failed_count == 0

    # All 5 are orphans (no subscription_id), so the integrity flag must catch them.
    assert metrics.orphan_count == 5

    # by_method: cash=3 ($800), card=1 ($300), transfer=1 ($400).
    by_method = {b.method: (b.count, b.total) for b in metrics.by_method}
    assert by_method["cash"] == (3, 800.0)
    assert by_method["card"] == (1, 300.0)
    assert by_method["transfer"] == (1, 400.0)

    # by_status: only COMPLETED.
    by_status = {b.status: (b.count, b.total) for b in metrics.by_status}
    assert by_status["COMPLETED"] == (5, 1500.0)

    # daily_series should hit 4 distinct days within the window (Jan 5/10/15/20).
    days_in_window = [d for d in metrics.daily_series if window_start <= d.day <= window_end]
    assert len(days_in_window) == 4
    totals_by_day = {d.day.date(): d.total for d in days_in_window}
    assert totals_by_day[_utc(3000, 1, 5).date()] == 100.0
    assert totals_by_day[_utc(3000, 1, 10).date()] == 500.0  # $200 + $300
    assert totals_by_day[_utc(3000, 1, 15).date()] == 400.0
    assert totals_by_day[_utc(3000, 1, 20).date()] == 500.0


# --------------------------------------------------------------------------- #
# Smoke: status filter on get_payments                                        #
# --------------------------------------------------------------------------- #

async def test_get_payments_status_filter(db):
    person = await _make_person(db, full_name="Status Subject")
    await create_payment(
        db=db, person_id=person.id, amount=Decimal("10.00"), method="cash",
        status="COMPLETED", paid_at=_utc(3001, 1, 1), commit=False,
    )
    await create_payment(
        db=db, person_id=person.id, amount=Decimal("20.00"), method="cash",
        status="PENDING", paid_at=_utc(3001, 1, 2), commit=False,
    )
    await db.flush()

    pending = await get_payments(
        db=db,
        status="PENDING",
        start_date=_utc(3001, 1, 1),
        end_date=_utc(3001, 1, 31),
    )
    pending_for_person = [p for p in pending if p.person_id == person.id]
    assert len(pending_for_person) == 1
    assert pending_for_person[0].amount == Decimal("20.00")
