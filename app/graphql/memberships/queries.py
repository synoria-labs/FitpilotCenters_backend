from datetime import datetime
from typing import List, Optional

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud.membershipsCrud import (
    get_membership_plans, get_membership_plan_by_id,
    get_active_subscriptions, get_expiring_subscriptions, get_membership_subscriptions
)
from app.graphql.memberships.types import (
    MembershipPlan,
    Subscription,
    PaymentRecord,
    PaginatedPayments,
    PaymentMetrics,
)
from app.graphql.auth.permissions import IsAuthenticated, require_capability
from app.crud.permissions import VIEW_FINANCES
from app.core.conversions import coerce_int


def _empty_payment_metrics() -> PaymentMetrics:
    """Zeroed metrics returned when the requester lacks view_finances."""
    return PaymentMetrics(
        total_amount=0.0, total_count=0, avg_amount=0.0, completed_amount=0.0,
        pending_count=0, pending_amount=0.0, failed_count=0, refunded_count=0,
        orphan_count=0, duplicate_suspect_count=0,
        by_method=[], by_plan=[], by_status=[], daily_series=[],
    )


@strawberry.type
class MembershipsQuery:
    @strawberry.field(permission_classes=[IsAuthenticated])
    async def membership_plans(self, info: Info, include_inactive: bool = False) -> List[MembershipPlan]:
        """Get membership plans. By default only active plans are returned."""
        db: AsyncSession = info.context.db
        plans_data = await get_membership_plans(db=db, include_inactive=include_inactive)
        return [MembershipPlan.from_data(plan_data) for plan_data in plans_data]

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def membership_plan(self, info: Info, plan_id: int) -> Optional[MembershipPlan]:
        """Get membership plan by ID"""
        db: AsyncSession = info.context.db

        plan_id = coerce_int(plan_id)
        if plan_id is None:
            return None

        plan_data = await get_membership_plan_by_id(db=db, plan_id=plan_id)
        return MembershipPlan.from_data(plan_data) if plan_data else None

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def active_subscriptions(self, info: Info, limit: int = 100) -> List[Subscription]:
        """Get list of active subscriptions"""
        db: AsyncSession = info.context.db
        subscriptions_data = await get_active_subscriptions(db=db, limit=limit)
        return [Subscription.from_data(sub_data) for sub_data in subscriptions_data]

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def expiring_subscriptions(self, info: Info, days_ahead: int = 7) -> List[Subscription]:
        """Get subscriptions expiring in the next N days"""
        db: AsyncSession = info.context.db
        subscriptions_data = await get_expiring_subscriptions(db=db, days_ahead=days_ahead)
        return [Subscription.from_data(sub_data) for sub_data in subscriptions_data]

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def membership_subscriptions(
        self,
        info: Info,
        limit: int = 100,
        offset: int = 0,
        status: Optional[str] = None,
        search: Optional[str] = None
    ) -> List[Subscription]:
        """Get membership subscriptions with optional filters"""
        db: AsyncSession = info.context.db
        subscriptions_data = await get_membership_subscriptions(
            db=db,
            limit=limit,
            offset=offset,
            status=status,
            search=search
        )
        return [Subscription.from_data(sub_data) for sub_data in subscriptions_data]

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def payments(
        self,
        info: Info,
        limit: int = 100,
        offset: int = 0,
        search: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        status: Optional[str] = None,
        method: Optional[str] = None,
    ) -> PaginatedPayments:
        """Get paginated payments with optional search, date, status and method filters.

        Returns both the page items and the total row count matching the filters,
        so the UI can render an accurate "X of Y" footer without a second query.
        """
        if await require_capability(info, VIEW_FINANCES):
            return PaginatedPayments(items=[], total=0)
        db: AsyncSession = info.context.db
        from app.crud.membershipsCrud import get_payments, count_payments

        filters = dict(
            search=search,
            start_date=start_date,
            end_date=end_date,
            status=status,
            method=method,
        )
        payments_data = await get_payments(db=db, limit=limit, offset=offset, **filters)
        total = await count_payments(db=db, **filters)
        return PaginatedPayments(
            items=[PaymentRecord.from_model(p) for p in payments_data],
            total=total,
        )

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def payment_metrics(
        self,
        info: Info,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        status: Optional[str] = None,
        method: Optional[str] = None,
    ) -> PaymentMetrics:
        """Aggregated metrics for the finances panel.

        Returns totals, ticket average, breakdowns by method/plan/status,
        a daily series, and integrity flags (orphan payments, suspected
        duplicates within 5 minutes). Filter parameters are the same as the
        `payments` query so the panel stays in sync with the table.
        """
        if await require_capability(info, VIEW_FINANCES):
            return _empty_payment_metrics()
        db: AsyncSession = info.context.db
        from app.crud.membershipsCrud import get_payment_metrics

        data = await get_payment_metrics(
            db=db,
            start_date=start_date,
            end_date=end_date,
            status=status,
            method=method,
        )
        return PaymentMetrics.from_data(data)
