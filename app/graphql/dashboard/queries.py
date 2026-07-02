"""GraphQL queries for the dashboard tab."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud.dashboard_metrics import get_dashboard_metrics
from app.crud.permissions import VIEW_FINANCES
from app.graphql.auth.permissions import IsAuthenticated, require_capability
from app.graphql.dashboard.types import DashboardMetrics


def _empty_dashboard_metrics() -> DashboardMetrics:
    """Zeroed KPIs returned when the requester lacks view_finances."""
    return DashboardMetrics(
        total_members=0, active_members=0, new_members=0, period_reservations=0,
        period_revenue=0.0, avg_occupancy=0.0, total_members_prev=0,
        active_members_prev=0, new_members_prev=0, reservations_prev=0,
        revenue_prev=0.0, avg_occupancy_prev=0.0,
        revenue_by_day=[], occupancy_by_class=[], new_members_by_day=[],
        membership_distribution=[], top_membership_sales_all_time=None,
        top_membership_sales_period=None,
    )


@strawberry.type
class DashboardQuery:
    @strawberry.field(permission_classes=[IsAuthenticated])
    async def dashboard_metrics(
        self,
        info: Info,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> DashboardMetrics:
        """Aggregated KPIs and chart series for the dashboard.

        Defaults to "this month" (1st of month → now) if no range is provided.
        Stock KPIs are snapshotted at ``end_date``; flow KPIs are filtered to
        the (start, end) window. Previous-period values use a same-sized
        window shifted back so the UI can render trend deltas.
        """
        if await require_capability(info, VIEW_FINANCES):
            return _empty_dashboard_metrics()
        db: AsyncSession = info.context.db

        if end_date is None:
            end_date = datetime.now(timezone.utc)
        if start_date is None:
            start_date = end_date.replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )

        data = await get_dashboard_metrics(
            db=db, start_date=start_date, end_date=end_date
        )
        return DashboardMetrics.from_data(data)
