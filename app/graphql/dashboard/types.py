"""GraphQL types for the dashboard tab.

Re-exports DailyPoint and PlanBucket from the memberships GraphQL module so
the dashboard can share the same shapes for the revenue / new members /
membership distribution series. ClassBucket is dashboard-specific.
"""
from __future__ import annotations

import strawberry

from app.graphql.memberships.types import DailyPoint, PlanBucket


@strawberry.type
class ClassBucket:
    """Occupancy breakdown by class type."""

    class_name: str
    capacity: int
    reserved: int
    occupancy_pct: float


@strawberry.type
class DashboardMetrics:
    """All headline KPIs and chart series for the dashboard tab."""

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
    revenue_by_day: list[DailyPoint]
    occupancy_by_class: list[ClassBucket]
    new_members_by_day: list[DailyPoint]
    membership_distribution: list[PlanBucket]
    top_membership_sales_all_time: PlanBucket | None
    top_membership_sales_period: PlanBucket | None

    @classmethod
    def from_data(cls, data) -> "DashboardMetrics":
        top_all_time = data.top_membership_sales_all_time
        top_period = data.top_membership_sales_period
        return cls(
            total_members=data.total_members,
            active_members=data.active_members,
            new_members=data.new_members,
            period_reservations=data.period_reservations,
            period_revenue=data.period_revenue,
            avg_occupancy=data.avg_occupancy,
            total_members_prev=data.total_members_prev,
            active_members_prev=data.active_members_prev,
            new_members_prev=data.new_members_prev,
            reservations_prev=data.reservations_prev,
            revenue_prev=data.revenue_prev,
            avg_occupancy_prev=data.avg_occupancy_prev,
            revenue_by_day=[
                DailyPoint(day=p.day, count=p.count, total=p.total)
                for p in data.revenue_by_day
            ],
            occupancy_by_class=[
                ClassBucket(
                    class_name=b.class_name,
                    capacity=b.capacity,
                    reserved=b.reserved,
                    occupancy_pct=b.occupancy_pct,
                )
                for b in data.occupancy_by_class
            ],
            new_members_by_day=[
                DailyPoint(day=p.day, count=p.count, total=p.total)
                for p in data.new_members_by_day
            ],
            membership_distribution=[
                PlanBucket(
                    plan_id=b.plan_id,
                    plan_name=b.plan_name,
                    count=b.count,
                    total=b.total,
                )
                for b in data.membership_distribution
            ],
            top_membership_sales_all_time=(
                PlanBucket(
                    plan_id=top_all_time.plan_id,
                    plan_name=top_all_time.plan_name,
                    count=top_all_time.count,
                    total=top_all_time.total,
                )
                if top_all_time
                else None
            ),
            top_membership_sales_period=(
                PlanBucket(
                    plan_id=top_period.plan_id,
                    plan_name=top_period.plan_name,
                    count=top_period.count,
                    total=top_period.total,
                )
                if top_period
                else None
            ),
        )
