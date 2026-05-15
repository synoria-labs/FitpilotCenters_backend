"""Aggregations for the finances panel.

Returns totals, breakdowns by method/plan/status, daily series, and integrity
flags (orphan payments, suspected duplicates) in a small number of round trips.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MembershipPlan, MembershipSubscription, Payment

from .utils import _normalize_to_utc


@dataclass
class MethodBucketData:
    method: str
    count: int
    total: float


@dataclass
class PlanBucketData:
    plan_id: Optional[int]
    plan_name: Optional[str]
    count: int
    total: float


@dataclass
class StatusBucketData:
    status: str
    count: int
    total: float


@dataclass
class DailyPointData:
    day: datetime
    count: int
    total: float


@dataclass
class PaymentMetricsData:
    total_amount: float
    total_count: int
    avg_amount: float
    completed_amount: float
    pending_count: int
    pending_amount: float
    failed_count: int
    refunded_count: int
    orphan_count: int
    duplicate_suspect_count: int
    by_method: list[MethodBucketData] = field(default_factory=list)
    by_plan: list[PlanBucketData] = field(default_factory=list)
    by_status: list[StatusBucketData] = field(default_factory=list)
    daily_series: list[DailyPointData] = field(default_factory=list)


def _build_filters(
    start_date: Optional[datetime],
    end_date: Optional[datetime],
    status: Optional[str],
    method: Optional[str],
):
    conditions = []
    if start_date is not None:
        conditions.append(Payment.paid_at >= _normalize_to_utc(start_date))
    if end_date is not None:
        conditions.append(Payment.paid_at <= _normalize_to_utc(end_date))
    if status:
        conditions.append(Payment.status == status)
    if method:
        conditions.append(Payment.method == method)
    return conditions


async def get_payment_metrics(
    db: AsyncSession,
    *,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    status: Optional[str] = None,
    method: Optional[str] = None,
) -> PaymentMetricsData:
    """Compute aggregated metrics for the payments matching the given filters.

    Issues 5 SQL queries: totals + status counters (with FILTER clauses),
    by_method, by_status, by_plan, daily_series, plus a duplicate-detection
    window function query.
    """
    conditions = _build_filters(start_date, end_date, status, method)

    def _where(stmt):
        return stmt.where(and_(*conditions)) if conditions else stmt

    totals_stmt = _where(
        select(
            func.count(Payment.id).label("total_count"),
            func.coalesce(func.sum(Payment.amount), 0).label("total_amount"),
            func.coalesce(func.avg(Payment.amount), 0).label("avg_amount"),
            func.coalesce(
                func.sum(Payment.amount).filter(Payment.status == "COMPLETED"), 0
            ).label("completed_amount"),
            func.count(Payment.id)
            .filter(Payment.status == "PENDING")
            .label("pending_count"),
            func.coalesce(
                func.sum(Payment.amount).filter(Payment.status == "PENDING"), 0
            ).label("pending_amount"),
            func.count(Payment.id)
            .filter(Payment.status == "FAILED")
            .label("failed_count"),
            func.count(Payment.id)
            .filter(Payment.status == "REFUNDED")
            .label("refunded_count"),
            func.count(Payment.id)
            .filter(Payment.subscription_id.is_(None))
            .label("orphan_count"),
        )
    )
    totals_row = (await db.execute(totals_stmt)).one()

    by_method_stmt = _where(
        select(
            Payment.method,
            func.count(Payment.id).label("count"),
            func.coalesce(func.sum(Payment.amount), 0).label("total"),
        )
    ).group_by(Payment.method).order_by(func.sum(Payment.amount).desc())
    by_method = [
        MethodBucketData(method=r.method, count=int(r.count), total=float(r.total))
        for r in (await db.execute(by_method_stmt)).all()
    ]

    by_status_stmt = _where(
        select(
            Payment.status,
            func.count(Payment.id).label("count"),
            func.coalesce(func.sum(Payment.amount), 0).label("total"),
        )
    ).group_by(Payment.status).order_by(func.sum(Payment.amount).desc())
    by_status = [
        StatusBucketData(status=r.status, count=int(r.count), total=float(r.total))
        for r in (await db.execute(by_status_stmt)).all()
    ]

    by_plan_stmt = _where(
        select(
            MembershipPlan.id.label("plan_id"),
            MembershipPlan.name.label("plan_name"),
            func.count(Payment.id).label("count"),
            func.coalesce(func.sum(Payment.amount), 0).label("total"),
        )
        .select_from(Payment)
        .outerjoin(
            MembershipSubscription,
            Payment.subscription_id == MembershipSubscription.id,
        )
        .outerjoin(MembershipPlan, MembershipSubscription.plan_id == MembershipPlan.id)
    ).group_by(MembershipPlan.id, MembershipPlan.name).order_by(
        func.sum(Payment.amount).desc()
    ).limit(20)
    by_plan = [
        PlanBucketData(
            plan_id=r.plan_id,
            plan_name=r.plan_name,
            count=int(r.count),
            total=float(r.total),
        )
        for r in (await db.execute(by_plan_stmt)).all()
    ]

    day_col = func.date_trunc("day", Payment.paid_at).label("day")
    daily_stmt = _where(
        select(
            day_col,
            func.count(Payment.id).label("count"),
            func.coalesce(func.sum(Payment.amount), 0).label("total"),
        )
    ).group_by(day_col).order_by(day_col)
    daily_series = [
        DailyPointData(day=r.day, count=int(r.count), total=float(r.total))
        for r in (await db.execute(daily_stmt)).all()
    ]

    # Suspected duplicates: same person_id + amount within 5 minutes.
    # Window function LAG over the filtered set.
    dup_subq = _where(
        select(
            Payment.paid_at.label("paid_at"),
            func.lag(Payment.paid_at)
            .over(
                partition_by=(Payment.person_id, Payment.amount),
                order_by=Payment.paid_at,
            )
            .label("prev_paid_at"),
        )
    ).subquery()
    dup_stmt = select(func.count()).select_from(dup_subq).where(
        and_(
            dup_subq.c.prev_paid_at.isnot(None),
            (dup_subq.c.paid_at - dup_subq.c.prev_paid_at)
            < text("INTERVAL '5 minutes'"),
        )
    )
    duplicate_suspect_count = int(
        (await db.execute(dup_stmt)).scalar_one() or 0
    )

    return PaymentMetricsData(
        total_amount=float(totals_row.total_amount),
        total_count=int(totals_row.total_count),
        avg_amount=float(totals_row.avg_amount),
        completed_amount=float(totals_row.completed_amount),
        pending_count=int(totals_row.pending_count),
        pending_amount=float(totals_row.pending_amount),
        failed_count=int(totals_row.failed_count),
        refunded_count=int(totals_row.refunded_count),
        orphan_count=int(totals_row.orphan_count),
        duplicate_suspect_count=duplicate_suspect_count,
        by_method=by_method,
        by_plan=by_plan,
        by_status=by_status,
        daily_series=daily_series,
    )
