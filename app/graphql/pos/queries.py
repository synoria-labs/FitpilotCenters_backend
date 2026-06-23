from datetime import datetime
from typing import List, Optional

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud.permissions import (
    MANAGE_CASH_SESSION,
    MANAGE_PRODUCTS,
    OPERATE_POS,
    VIEW_POS_REPORTS,
)
from app.crud.posCrud import (
    get_cash_session_report,
    get_open_cash_session,
    get_products,
    get_sale,
    get_sales,
)
from app.graphql.auth.permissions import IsAuthenticated, require_any_capability

# Anyone who runs the checkout, manages the caja or reads reports may see the
# open caja / its live corte; the writes (open/close/movement) stay gated on
# manage_cash_session in the mutations.
_CAJA_VIEW_CAPS = [MANAGE_CASH_SESSION, OPERATE_POS, VIEW_POS_REPORTS]
from app.graphql.pos.types import (
    CashSessionReportType,
    CashSessionType,
    ProductType,
    SaleType,
)


@strawberry.type
class PosQuery:
    @strawberry.field(permission_classes=[IsAuthenticated])
    async def open_cash_session(self, info: Info) -> Optional[CashSessionType]:
        """The currently-open caja, or null."""
        db: AsyncSession = info.context.db
        if await require_any_capability(info, _CAJA_VIEW_CAPS):
            return None
        session = await get_open_cash_session(db)
        return CashSessionType.from_model(session) if session else None

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def cash_session_report(self, info: Info, cash_session_id: int) -> Optional[CashSessionReportType]:
        """Corte de caja report for a session (works live for an open caja)."""
        db: AsyncSession = info.context.db
        if await require_any_capability(info, _CAJA_VIEW_CAPS):
            return None
        data = await get_cash_session_report(db, cash_session_id)
        return CashSessionReportType.from_data(data) if data else None

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def sales(
        self,
        info: Info,
        limit: int = 100,
        offset: int = 0,
        cash_session_id: Optional[int] = None,
        status: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> List[SaleType]:
        db: AsyncSession = info.context.db
        if await require_any_capability(info, [OPERATE_POS, VIEW_POS_REPORTS]):
            return []
        rows = await get_sales(
            db,
            limit=limit,
            offset=offset,
            cash_session_id=cash_session_id,
            status=status,
            start_date=start_date,
            end_date=end_date,
        )
        return [SaleType.from_model(s) for s in rows]

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def sale(self, info: Info, sale_id: int) -> Optional[SaleType]:
        db: AsyncSession = info.context.db
        if await require_any_capability(info, [OPERATE_POS, VIEW_POS_REPORTS]):
            return None
        s = await get_sale(db, sale_id)
        return SaleType.from_model(s) if s else None

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def products(
        self, info: Info, include_inactive: bool = False, search: Optional[str] = None
    ) -> List[ProductType]:
        """Product catalog. Readable by POS operators and by catalog managers."""
        db: AsyncSession = info.context.db
        if await require_any_capability(info, [OPERATE_POS, MANAGE_PRODUCTS]):
            return []
        rows = await get_products(db, include_inactive=include_inactive, search=search)
        return [ProductType.from_model(p) for p in rows]
