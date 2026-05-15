"""GraphQL queries for the dashboard tab."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud.dashboard_metrics import get_dashboard_metrics
from app.graphql.auth.permissions import IsAuthenticated
from app.graphql.dashboard.types import DashboardMetrics


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
