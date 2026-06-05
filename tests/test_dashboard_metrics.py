"""Backend tests for the dashboard metrics CRUD layer.

Covers:
  - count_active_members: respects deleted_at and as_of windowing
  - count_active_subscriptions: respects status + start/end window
  - count_new_members: range filter via PersonRole.created_at
  - count_reservations: only counts reserved/checked_in for sessions in window
  - calculate_occupancy_avg: weighted avg, div-by-zero safe

Tests use far-future timestamps (year 3000+) so they don't see real defaultdb
data, and run inside the savepoint fixture so nothing persists.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.crud.dashboard_metrics import (
    calculate_occupancy_avg,
    count_active_members,
    count_active_subscriptions,
    count_new_members,
    count_reservations,
)
from app.models import (
    MembershipPlan,
    MembershipSubscription,
    People,
)
from app.models.classModel import ClassSession, ClassType, Reservation
from app.models.userModel import PersonRole, Role


def _utc(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc)


async def _ensure_member_role(db) -> Role:
    """Get-or-create the 'member' role and return it."""
    from sqlalchemy import select

    existing = (
        await db.execute(select(Role).where(Role.code == "member"))
    ).scalar_one_or_none()
    if existing:
        return existing
    role = Role(code="member", description="Member")
    db.add(role)
    await db.flush()
    return role


async def _make_member(
    db,
    *,
    role: Role,
    full_name: str,
    role_assigned_at: datetime,
    deleted_at: datetime | None = None,
) -> People:
    person = People(full_name=full_name, deleted_at=deleted_at)
    db.add(person)
    await db.flush()
    pr = PersonRole(person_id=person.id, role_id=role.id, created_at=role_assigned_at)
    db.add(pr)
    await db.flush()
    return person


# --------------------------------------------------------------------------- #
# count_active_members                                                         #
# --------------------------------------------------------------------------- #


async def test_count_active_members_excludes_deleted_and_future_assignments(db):
    role = await _ensure_member_role(db)

    # Baseline: capture the count of pre-existing real-data members so we can
    # assert deltas instead of absolute values.
    baseline = await count_active_members(db, as_of=_utc(3000, 6, 15))

    # Test fixtures, all assigned within year 3000:
    await _make_member(db, role=role, full_name="Active 1", role_assigned_at=_utc(3000, 1, 1))
    await _make_member(db, role=role, full_name="Active 2", role_assigned_at=_utc(3000, 2, 1))
    await _make_member(
        db,
        role=role,
        full_name="Deleted 1",
        role_assigned_at=_utc(3000, 1, 1),
        deleted_at=_utc(3000, 5, 1),
    )
    await _make_member(
        db,
        role=role,
        full_name="Future 1",
        role_assigned_at=_utc(3000, 7, 1),
    )

    # As of 3000-06-15: Active1 + Active2 should be counted; Deleted1 already
    # gone; Future1 not yet assigned. So delta is +2.
    count = await count_active_members(db, as_of=_utc(3000, 6, 15))
    assert count - baseline == 2

    # As of 3000-04-15 (before Deleted1 was deleted): Active1, Active2, Deleted1.
    # Future1 still not assigned. So delta is +3.
    early = await count_active_members(db, as_of=_utc(3000, 4, 15))
    early_baseline = await count_active_members(db, as_of=_utc(3000, 4, 15))  # idempotent
    # baseline at this earlier date might be the same as later one, but we
    # test the delta against itself by re-querying with the early as_of:
    # we just make sure Deleted1 was counted. Easier: compare to as_of=now.
    # The delta of (early - count) should be exactly +1 (Deleted1 reappears).
    assert early - count == 1


# --------------------------------------------------------------------------- #
# count_active_subscriptions                                                   #
# --------------------------------------------------------------------------- #


async def test_count_active_subscriptions_window_and_status(db):
    role = await _ensure_member_role(db)
    person = await _make_member(
        db, role=role, full_name="Sub Holder", role_assigned_at=_utc(3001, 1, 1)
    )

    plan = MembershipPlan(
        name="Test Plan 3001",
        price=100,
        duration_value=1,
        duration_unit="month",
    )
    db.add(plan)
    await db.flush()

    baseline = await count_active_subscriptions(db, as_of=_utc(3001, 6, 15))

    # Active sub spanning June.
    db.add(
        MembershipSubscription(
            person_id=person.id,
            plan_id=plan.id,
            start_at=_utc(3001, 6, 1),
            end_at=_utc(3001, 7, 1),
            status="active",
        )
    )
    # Active sub but expired by June 15 (end_at = June 10).
    db.add(
        MembershipSubscription(
            person_id=person.id,
            plan_id=plan.id,
            start_at=_utc(3001, 5, 1),
            end_at=_utc(3001, 6, 10),
            status="active",
        )
    )
    # Status canceled — should not count.
    db.add(
        MembershipSubscription(
            person_id=person.id,
            plan_id=plan.id,
            start_at=_utc(3001, 6, 1),
            end_at=_utc(3001, 7, 1),
            status="canceled",
        )
    )
    await db.flush()

    count = await count_active_subscriptions(db, as_of=_utc(3001, 6, 15))
    assert count - baseline == 1


# --------------------------------------------------------------------------- #
# count_new_members                                                            #
# --------------------------------------------------------------------------- #


async def test_count_new_members_filters_by_role_assignment_date(db):
    role = await _ensure_member_role(db)

    await _make_member(db, role=role, full_name="In window 1", role_assigned_at=_utc(3002, 5, 5))
    await _make_member(db, role=role, full_name="In window 2", role_assigned_at=_utc(3002, 5, 20))
    await _make_member(db, role=role, full_name="Before", role_assigned_at=_utc(3002, 4, 30))
    await _make_member(db, role=role, full_name="After", role_assigned_at=_utc(3002, 6, 1))

    count = await count_new_members(
        db, start_date=_utc(3002, 5, 1), end_date=_utc(3002, 5, 31, 23)
    )
    # Among test data only, exactly 2 are in May 3002. No real data falls in year 3002.
    assert count == 2


# --------------------------------------------------------------------------- #
# count_reservations                                                           #
# --------------------------------------------------------------------------- #


async def _create_session(db, *, start_at: datetime, capacity: int = 10) -> ClassSession:
    """Create a minimal scheduled session, depending on a class_type+venue."""
    from sqlalchemy import select

    from app.models.venueModel import Venue

    # Get-or-create class type
    ctype = (
        await db.execute(select(ClassType).where(ClassType.code == "test_dash"))
    ).scalar_one_or_none()
    if not ctype:
        ctype = ClassType(code="test_dash", name="Dashboard Test")
        db.add(ctype)
        await db.flush()

    # Use any existing venue (real DB has at least one)
    venue = (await db.execute(select(Venue).limit(1))).scalar_one_or_none()
    assert venue is not None, "test requires at least one Venue in defaultdb"

    sess = ClassSession(
        class_type_id=ctype.id,
        venue_id=venue.id,
        start_at=start_at,
        end_at=start_at.replace(hour=start_at.hour + 1),
        capacity=capacity,
        status="scheduled",
    )
    db.add(sess)
    await db.flush()
    return sess


async def test_count_reservations_window_and_status(db):
    role = await _ensure_member_role(db)
    person = await _make_member(
        db, role=role, full_name="Resv Holder", role_assigned_at=_utc(3003, 1, 1)
    )

    sess_in = await _create_session(db, start_at=_utc(3003, 6, 10, 9))
    sess_out = await _create_session(db, start_at=_utc(3003, 7, 10, 9))

    # 2 valid reservations on the in-window session
    db.add(Reservation(session_id=sess_in.id, person_id=person.id, status="reserved"))
    person2 = await _make_member(
        db, role=role, full_name="Resv Holder 2", role_assigned_at=_utc(3003, 1, 1)
    )
    db.add(Reservation(session_id=sess_in.id, person_id=person2.id, status="checked_in"))
    # 1 cancelled on in-window — should not count
    person3 = await _make_member(
        db, role=role, full_name="Resv Holder 3", role_assigned_at=_utc(3003, 1, 1)
    )
    db.add(Reservation(session_id=sess_in.id, person_id=person3.id, status="canceled"))
    # 1 valid on out-of-window session — should not count
    db.add(Reservation(session_id=sess_out.id, person_id=person.id, status="reserved"))
    await db.flush()

    count = await count_reservations(
        db, start_date=_utc(3003, 6, 1), end_date=_utc(3003, 6, 30, 23)
    )
    assert count == 2


# --------------------------------------------------------------------------- #
# calculate_occupancy_avg                                                      #
# --------------------------------------------------------------------------- #


async def test_occupancy_avg_weighted(db):
    role = await _ensure_member_role(db)
    person = await _make_member(
        db, role=role, full_name="Occ Holder", role_assigned_at=_utc(3004, 1, 1)
    )

    # Session A: capacity 10, 5 reserved, 1 checked_in, 1 canceled
    sess_a = await _create_session(db, start_at=_utc(3004, 6, 10, 9), capacity=10)
    for i in range(5):
        p = await _make_member(
            db, role=role, full_name=f"A-r{i}", role_assigned_at=_utc(3004, 1, 1)
        )
        db.add(Reservation(session_id=sess_a.id, person_id=p.id, status="reserved"))
    p_check = await _make_member(
        db, role=role, full_name="A-check", role_assigned_at=_utc(3004, 1, 1)
    )
    db.add(Reservation(session_id=sess_a.id, person_id=p_check.id, status="checked_in"))
    p_cancel = await _make_member(
        db, role=role, full_name="A-cancel", role_assigned_at=_utc(3004, 1, 1)
    )
    db.add(Reservation(session_id=sess_a.id, person_id=p_cancel.id, status="canceled"))

    # Session B: capacity 5, 0 reserved
    sess_b = await _create_session(db, start_at=_utc(3004, 6, 11, 10), capacity=5)
    await db.flush()

    # A: 6/10 occupied. B: 0/5. Total occupied 6, total capacity 15. Avg = 40.0%.
    occ = await calculate_occupancy_avg(
        db, start_date=_utc(3004, 6, 1), end_date=_utc(3004, 6, 30, 23)
    )
    assert occ == 40.0


async def test_occupancy_avg_zero_capacity_window(db):
    """Empty window returns 0.0 instead of erroring."""
    occ = await calculate_occupancy_avg(
        db, start_date=_utc(3010, 1, 1), end_date=_utc(3010, 1, 2)
    )
    assert occ == 0.0


# --------------------------------------------------------------------------- #
# get_dashboard_metrics orchestrator                                           #
# --------------------------------------------------------------------------- #


async def test_dashboard_metrics_previous_window_arithmetic(db):
    """Verify _previous_window: prev_end == start, prev_start == start - delta."""
    from app.crud.dashboard_metrics import _previous_window

    start = _utc(3005, 5, 1)
    end = _utc(3005, 5, 31, 23, 59, 59) if False else _utc(3005, 5, 31)
    prev_start, prev_end = _previous_window(start, end)
    assert prev_end == start
    assert prev_end - prev_start == end - start


async def test_dashboard_metrics_orchestrator_full(db):
    """End-to-end: seed 2 windows of data, verify current vs previous deltas."""
    from app.crud.dashboard_metrics import get_dashboard_metrics

    role = await _ensure_member_role(db)

    # Seed members in current window (May 3006) and previous window (Apr 3006)
    for i in range(5):
        await _make_member(
            db, role=role, full_name=f"May {i}", role_assigned_at=_utc(3006, 5, 5 + i)
        )
    for i in range(2):
        await _make_member(
            db, role=role, full_name=f"Apr {i}", role_assigned_at=_utc(3006, 4, 5 + i)
        )

    # Seed payments in both windows
    from decimal import Decimal

    from app.models import Payment

    member_for_pay = await _make_member(
        db, role=role, full_name="Payer", role_assigned_at=_utc(3006, 1, 1)
    )
    # Current period: $1500 in 3 payments
    for amt in (500, 500, 500):
        db.add(
            Payment(
                person_id=member_for_pay.id,
                amount=Decimal(amt),
                method="cash",
                status="COMPLETED",
                paid_at=_utc(3006, 5, 10),
            )
        )
    # Previous period: $800 in 2 payments
    for amt in (300, 500):
        db.add(
            Payment(
                person_id=member_for_pay.id,
                amount=Decimal(amt),
                method="cash",
                status="COMPLETED",
                paid_at=_utc(3006, 4, 10),
            )
        )
    await db.flush()

    # Window: May 3006 (full month). Previous window auto = April 3006.
    start = _utc(3006, 5, 1, 0)
    end = _utc(3006, 5, 31, 23)

    metrics = await get_dashboard_metrics(db, start_date=start, end_date=end)

    # Flow KPIs: current vs previous
    assert metrics.new_members == 5
    assert metrics.new_members_prev == 2
    assert metrics.period_revenue == 1500.0
    assert metrics.revenue_prev == 800.0

    # Stock KPIs (snapshot at end vs at start). After all seeds, total_members
    # at end = baseline_at_start + 5 (May new) + 2 (Apr new) + 1 (Payer in Jan).
    # At start (May 1) = baseline_at_start + 2 (Apr) + 1 (Payer in Jan).
    # Delta total_members - total_members_prev should be exactly 5.
    assert metrics.total_members - metrics.total_members_prev == 5

    # Series shapes
    assert isinstance(metrics.revenue_by_day, list)
    assert isinstance(metrics.occupancy_by_class, list)
    assert isinstance(metrics.new_members_by_day, list)
    assert isinstance(metrics.membership_distribution, list)


async def test_dashboard_metrics_handles_empty_window(db):
    """Year 5000+ window has no data — orchestrator returns zeros gracefully."""
    from app.crud.dashboard_metrics import get_dashboard_metrics

    metrics = await get_dashboard_metrics(
        db, start_date=_utc(5000, 1, 1), end_date=_utc(5000, 1, 31, 23)
    )
    assert metrics.new_members == 0
    assert metrics.period_reservations == 0
    assert metrics.period_revenue == 0.0
    assert metrics.avg_occupancy == 0.0
    assert metrics.revenue_by_day == []
    assert metrics.occupancy_by_class == []
    assert metrics.new_members_by_day == []
    # membership_distribution may have real-data entries (snapshot at year 5000
    # picks up subs created today that are still active in the future), but
    # this is an edge case we don't strictly assert.
