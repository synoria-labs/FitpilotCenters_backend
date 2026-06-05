"""Aggregations for the Dashboard tab.

Returns headline KPIs (totals, active, revenue, reservations, occupancy, new
members), their previous-period counterparts for trend deltas, and series for
the four charts (revenue / occupancy by class / new members / membership
distribution).

Stock KPIs (totalMembers, activeMembers) are evaluated as snapshots at the
end-of-period; flow KPIs are filtered to the (start, end) window. Occupancy
is treated as a flow KPI even though the plan classified it as stock — the
metric only makes sense over a window, and the previous-period delta is
useful.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.memberships.payment_metrics import DailyPointData, PlanBucketData
from app.crud.memberships.utils import _normalize_to_utc
from app.models import (
    MembershipPlan,
    MembershipSubscription,
    People,
)
from app.models.classModel import ClassSession, ClassType, Reservation
from app.models.userModel import PersonRole, Role


# Reservation statuses that occupy a seat.
_OCCUPIED_RESERVATION_STATUSES = ("reserved", "checked_in")
# Sessions that count toward occupancy (skip cancelled).
_VALID_SESSION_STATUSES = ("scheduled", "completed")


@dataclass
class ClassBucketData:
    class_name: str
    capacity: int
    reserved: int
    occupancy_pct: float


@dataclass
class DashboardMetricsData:
    # Stock KPIs (snapshot at end of current window)
    total_members: int
    active_members: int

    # Flow KPIs (current window)
    new_members: int
    period_reservations: int
    period_revenue: float
    avg_occupancy: float

    # Previous-period counterparts (same window size shifted back)
    total_members_prev: int
    active_members_prev: int
    new_members_prev: int
    reservations_prev: int
    revenue_prev: float
    avg_occupancy_prev: float

    # Series for the four charts
    revenue_by_day: list[DailyPointData] = field(default_factory=list)
    occupancy_by_class: list[ClassBucketData] = field(default_factory=list)
    new_members_by_day: list[DailyPointData] = field(default_factory=list)
    membership_distribution: list[PlanBucketData] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Count helpers                                                               #
# --------------------------------------------------------------------------- #


async def count_active_members(
    db: AsyncSession, *, as_of: Optional[datetime] = None
) -> int:
    """Count of people with role='member' who exist at ``as_of`` (default: now).

    A person is counted if:
      - the person_roles row was created on or before ``as_of``
      - the person has not been soft-deleted by ``as_of``
    """
    as_of_norm = _normalize_to_utc(as_of) if as_of else datetime.now(timezone.utc)

    stmt = (
        select(func.count(func.distinct(People.id)))
        .select_from(People)
        .join(PersonRole, PersonRole.person_id == People.id)
        .join(Role, Role.id == PersonRole.role_id)
        .where(
            Role.code == "member",
            PersonRole.created_at <= as_of_norm,
            or_(People.deleted_at.is_(None), People.deleted_at > as_of_norm),
        )
    )
    result = await db.execute(stmt)
    return int(result.scalar_one() or 0)


async def count_active_subscriptions(
    db: AsyncSession, *, as_of: Optional[datetime] = None
) -> int:
    """Count of membership subscriptions that are active at ``as_of``.

    "Active" = status='active' AND start_at <= as_of < end_at.
    """
    as_of_norm = _normalize_to_utc(as_of) if as_of else datetime.now(timezone.utc)

    stmt = select(func.count(MembershipSubscription.id)).where(
        MembershipSubscription.status == "active",
        MembershipSubscription.start_at <= as_of_norm,
        MembershipSubscription.end_at > as_of_norm,
    )
    result = await db.execute(stmt)
    return int(result.scalar_one() or 0)


async def count_new_members(
    db: AsyncSession,
    *,
    start_date: datetime,
    end_date: datetime,
) -> int:
    """Count of people that were assigned the 'member' role inside the window."""
    start = _normalize_to_utc(start_date)
    end = _normalize_to_utc(end_date)

    stmt = (
        select(func.count(func.distinct(People.id)))
        .select_from(People)
        .join(PersonRole, PersonRole.person_id == People.id)
        .join(Role, Role.id == PersonRole.role_id)
        .where(
            Role.code == "member",
            PersonRole.created_at >= start,
            PersonRole.created_at <= end,
            People.deleted_at.is_(None),
        )
    )
    result = await db.execute(stmt)
    return int(result.scalar_one() or 0)


async def count_reservations(
    db: AsyncSession,
    *,
    start_date: datetime,
    end_date: datetime,
) -> int:
    """Count of reservations for sessions starting in the window.

    Excludes canceled, no_show, and waitlisted reservations — only seats that
    were actually held (reserved or checked_in).
    """
    start = _normalize_to_utc(start_date)
    end = _normalize_to_utc(end_date)

    stmt = (
        select(func.count(Reservation.id))
        .select_from(Reservation)
        .join(ClassSession, ClassSession.id == Reservation.session_id)
        .where(
            ClassSession.start_at >= start,
            ClassSession.start_at <= end,
            Reservation.status.in_(_OCCUPIED_RESERVATION_STATUSES),
        )
    )
    result = await db.execute(stmt)
    return int(result.scalar_one() or 0)


async def calculate_occupancy_avg(
    db: AsyncSession,
    *,
    start_date: datetime,
    end_date: datetime,
) -> float:
    """Average occupancy percent across non-cancelled sessions in the window.

    Computed as: SUM(reserved) / SUM(capacity) * 100, weighted by capacity.
    Returns 0.0 if no sessions or zero total capacity (avoids div-by-zero).
    """
    start = _normalize_to_utc(start_date)
    end = _normalize_to_utc(end_date)

    # Per-session subquery: how many seats are occupied
    occupied_subq = (
        select(
            Reservation.session_id.label("session_id"),
            func.count(Reservation.id).label("reserved"),
        )
        .where(Reservation.status.in_(_OCCUPIED_RESERVATION_STATUSES))
        .group_by(Reservation.session_id)
        .subquery()
    )

    stmt = (
        select(
            func.coalesce(func.sum(ClassSession.capacity), 0).label("total_capacity"),
            func.coalesce(func.sum(occupied_subq.c.reserved), 0).label("total_reserved"),
        )
        .select_from(ClassSession)
        .outerjoin(occupied_subq, occupied_subq.c.session_id == ClassSession.id)
        .where(
            ClassSession.start_at >= start,
            ClassSession.start_at <= end,
            ClassSession.status.in_(_VALID_SESSION_STATUSES),
        )
    )
    row = (await db.execute(stmt)).one()
    capacity = int(row.total_capacity or 0)
    if capacity == 0:
        return 0.0
    return round(float(row.total_reserved or 0) / capacity * 100, 2)


# --------------------------------------------------------------------------- #
# Series for charts                                                            #
# --------------------------------------------------------------------------- #


async def occupancy_by_class(
    db: AsyncSession,
    *,
    start_date: datetime,
    end_date: datetime,
) -> list[ClassBucketData]:
    """Occupancy breakdown grouped by ClassType.name within the window."""
    start = _normalize_to_utc(start_date)
    end = _normalize_to_utc(end_date)

    occupied_subq = (
        select(
            Reservation.session_id.label("session_id"),
            func.count(Reservation.id).label("reserved"),
        )
        .where(Reservation.status.in_(_OCCUPIED_RESERVATION_STATUSES))
        .group_by(Reservation.session_id)
        .subquery()
    )

    stmt = (
        select(
            ClassType.name.label("class_name"),
            func.coalesce(func.sum(ClassSession.capacity), 0).label("capacity"),
            func.coalesce(func.sum(occupied_subq.c.reserved), 0).label("reserved"),
        )
        .select_from(ClassSession)
        .join(ClassType, ClassType.id == ClassSession.class_type_id)
        .outerjoin(occupied_subq, occupied_subq.c.session_id == ClassSession.id)
        .where(
            ClassSession.start_at >= start,
            ClassSession.start_at <= end,
            ClassSession.status.in_(_VALID_SESSION_STATUSES),
        )
        .group_by(ClassType.name)
        .order_by(func.sum(ClassSession.capacity).desc())
    )
    rows = (await db.execute(stmt)).all()
    out: list[ClassBucketData] = []
    for r in rows:
        cap = int(r.capacity or 0)
        res = int(r.reserved or 0)
        pct = round(res / cap * 100, 2) if cap > 0 else 0.0
        out.append(
            ClassBucketData(
                class_name=r.class_name or "(sin nombre)",
                capacity=cap,
                reserved=res,
                occupancy_pct=pct,
            )
        )
    return out


async def new_members_series(
    db: AsyncSession,
    *,
    start_date: datetime,
    end_date: datetime,
) -> list[DailyPointData]:
    """Daily count of new member-role assignments inside the window."""
    start = _normalize_to_utc(start_date)
    end = _normalize_to_utc(end_date)

    day_col = func.date_trunc("day", PersonRole.created_at).label("day")
    stmt = (
        select(
            day_col,
            func.count(func.distinct(People.id)).label("count"),
        )
        .select_from(People)
        .join(PersonRole, PersonRole.person_id == People.id)
        .join(Role, Role.id == PersonRole.role_id)
        .where(
            Role.code == "member",
            PersonRole.created_at >= start,
            PersonRole.created_at <= end,
            People.deleted_at.is_(None),
        )
        .group_by(day_col)
        .order_by(day_col)
    )
    rows = (await db.execute(stmt)).all()
    return [
        DailyPointData(day=r.day, count=int(r.count or 0), total=float(r.count or 0))
        for r in rows
    ]


async def revenue_by_day(
    db: AsyncSession,
    *,
    start_date: datetime,
    end_date: datetime,
) -> list[DailyPointData]:
    """Daily revenue series for the window. Thin wrapper over Payment table.

    Kept here (instead of importing payment_metrics.get_payment_metrics) to
    avoid the round-trip cost of computing all the other payment aggregations
    we don't need for the dashboard chart.
    """
    from app.models import Payment

    start = _normalize_to_utc(start_date)
    end = _normalize_to_utc(end_date)

    day_col = func.date_trunc("day", Payment.paid_at).label("day")
    stmt = (
        select(
            day_col,
            func.count(Payment.id).label("count"),
            func.coalesce(func.sum(Payment.amount), 0).label("total"),
        )
        .where(
            Payment.paid_at >= start,
            Payment.paid_at <= end,
            Payment.status == "COMPLETED",
        )
        .group_by(day_col)
        .order_by(day_col)
    )
    rows = (await db.execute(stmt)).all()
    return [
        DailyPointData(day=r.day, count=int(r.count or 0), total=float(r.total or 0))
        for r in rows
    ]


async def membership_distribution(
    db: AsyncSession,
    *,
    as_of: Optional[datetime] = None,
) -> list[PlanBucketData]:
    """Active subscriptions grouped by plan, snapshot at ``as_of`` (default now).

    "Active" uses the same predicate as count_active_subscriptions. ``count``
    is the number of subscriptions; ``total`` is the cumulative monthly value
    (price × count) — useful for an "MRR by plan" donut.
    """
    as_of_norm = _normalize_to_utc(as_of) if as_of else datetime.now(timezone.utc)

    stmt = (
        select(
            MembershipPlan.id.label("plan_id"),
            MembershipPlan.name.label("plan_name"),
            func.count(MembershipSubscription.id).label("count"),
            func.coalesce(
                func.sum(MembershipPlan.price), 0
            ).label("total"),
        )
        .select_from(MembershipSubscription)
        .join(MembershipPlan, MembershipPlan.id == MembershipSubscription.plan_id)
        .where(
            MembershipSubscription.status == "active",
            MembershipSubscription.start_at <= as_of_norm,
            MembershipSubscription.end_at > as_of_norm,
        )
        .group_by(MembershipPlan.id, MembershipPlan.name)
        .order_by(func.count(MembershipSubscription.id).desc())
    )
    rows = (await db.execute(stmt)).all()
    return [
        PlanBucketData(
            plan_id=r.plan_id,
            plan_name=r.plan_name,
            count=int(r.count or 0),
            total=float(r.total or 0),
        )
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# Sum helper (single-shot SUM(amount) for previous period revenue)            #
# --------------------------------------------------------------------------- #


async def _sum_revenue(
    db: AsyncSession, *, start_date: datetime, end_date: datetime
) -> float:
    """SUM(payments.amount) for COMPLETED payments in the window."""
    from app.models import Payment

    start = _normalize_to_utc(start_date)
    end = _normalize_to_utc(end_date)
    stmt = select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.paid_at >= start,
        Payment.paid_at <= end,
        Payment.status == "COMPLETED",
    )
    return float((await db.execute(stmt)).scalar_one() or 0)


# --------------------------------------------------------------------------- #
# Orchestrator                                                                 #
# --------------------------------------------------------------------------- #


def _previous_window(
    start_date: datetime, end_date: datetime
) -> tuple[datetime, datetime]:
    """Return (prev_start, prev_end) of the same length, ending just before start.

    Example: window [Sep 1 00:00, Sep 30 23:59] → prev [Aug 2 00:00, Sep 1 00:00).
    The previous window ends at exactly start_date so there is no overlap.
    """
    delta = end_date - start_date
    prev_end = start_date
    prev_start = prev_end - delta
    return prev_start, prev_end


async def get_dashboard_metrics(
    db: AsyncSession,
    *,
    start_date: datetime,
    end_date: datetime,
) -> DashboardMetricsData:
    """Compute all dashboard KPIs + chart series in one call.

    Stock KPIs (totalMembers, activeMembers) are snapshotted at ``end_date``;
    their "previous" counterparts use ``start_date`` as the snapshot point so
    the trend reflects "how many we had at the start vs the end of the window".

    Flow KPIs (revenue, reservations, new members, occupancy) and their prev
    counterparts use the (start, end) and (prev_start, prev_end) windows.
    """
    start = _normalize_to_utc(start_date)
    end = _normalize_to_utc(end_date)
    prev_start, prev_end = _previous_window(start, end)

    # Stock — snapshot at end_date and at start_date (= prev_end)
    total_members = await count_active_members(db, as_of=end)
    total_members_prev = await count_active_members(db, as_of=start)
    active_members = await count_active_subscriptions(db, as_of=end)
    active_members_prev = await count_active_subscriptions(db, as_of=start)

    # Flow — current and previous windows
    new_members = await count_new_members(db, start_date=start, end_date=end)
    new_members_prev = await count_new_members(
        db, start_date=prev_start, end_date=prev_end
    )

    period_reservations = await count_reservations(db, start_date=start, end_date=end)
    reservations_prev = await count_reservations(
        db, start_date=prev_start, end_date=prev_end
    )

    period_revenue = await _sum_revenue(db, start_date=start, end_date=end)
    revenue_prev = await _sum_revenue(db, start_date=prev_start, end_date=prev_end)

    avg_occupancy = await calculate_occupancy_avg(
        db, start_date=start, end_date=end
    )
    avg_occupancy_prev = await calculate_occupancy_avg(
        db, start_date=prev_start, end_date=prev_end
    )

    # Series
    revenue_series = await revenue_by_day(db, start_date=start, end_date=end)
    occupancy_series = await occupancy_by_class(db, start_date=start, end_date=end)
    new_members_serie = await new_members_series(db, start_date=start, end_date=end)
    plan_distribution = await membership_distribution(db, as_of=end)

    return DashboardMetricsData(
        total_members=total_members,
        active_members=active_members,
        new_members=new_members,
        period_reservations=period_reservations,
        period_revenue=period_revenue,
        avg_occupancy=avg_occupancy,
        total_members_prev=total_members_prev,
        active_members_prev=active_members_prev,
        new_members_prev=new_members_prev,
        reservations_prev=reservations_prev,
        revenue_prev=revenue_prev,
        avg_occupancy_prev=avg_occupancy_prev,
        revenue_by_day=revenue_series,
        occupancy_by_class=occupancy_series,
        new_members_by_day=new_members_serie,
        membership_distribution=plan_distribution,
    )
