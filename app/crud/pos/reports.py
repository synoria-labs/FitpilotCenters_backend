"""Cash-session (corte de caja) reporting.

Reuses the aggregation style of ``memberships/payment_metrics.py`` but scoped to a
single caja via ``sale_payments -> sales WHERE cash_session_id = ?``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.payment_methods import CASH_METHODS
from app.models.posModel import CashSession, CashMovement, Sale, SalePayment

from .cash_sessions import compute_expected_cash


@dataclass
class MethodTotalData:
    method: str
    count: int
    total: float


@dataclass
class CashSessionReportData:
    session_id: int
    status: str
    opened_by: Optional[int]
    opened_at: datetime
    closed_at: Optional[datetime]
    opening_float: float
    sales_count: int
    sales_total: float
    cash_in: float          # manual ingresos
    cash_out: float         # manual retiros
    cash_sales_total: float  # cash tenders only
    computed_expected_cash: float  # live expected (works for open caja)
    expected_cash: Optional[float]  # stored at close
    counted_cash: Optional[float]
    difference: Optional[float]
    by_method: List[MethodTotalData] = field(default_factory=list)


async def get_cash_session_report(
    db: AsyncSession, cash_session_id: int
) -> Optional[CashSessionReportData]:
    session = await db.get(CashSession, cash_session_id)
    if session is None:
        return None

    # Tenders grouped by method (completed sales of this caja).
    by_method_stmt = (
        select(
            SalePayment.method,
            func.count(SalePayment.id).label("count"),
            func.coalesce(func.sum(SalePayment.amount), 0).label("total"),
        )
        .select_from(SalePayment)
        .join(Sale, SalePayment.sale_id == Sale.id)
        .where(Sale.cash_session_id == cash_session_id)
        .where(Sale.status == "completed")
        .group_by(SalePayment.method)
        .order_by(func.sum(SalePayment.amount).desc())
    )
    by_method = [
        MethodTotalData(method=r.method, count=int(r.count), total=float(r.total))
        for r in (await db.execute(by_method_stmt)).all()
    ]
    cash_methods = {m.lower() for m in CASH_METHODS}
    cash_sales_total = sum(
        (b.total for b in by_method if (b.method or "").lower() in cash_methods), 0.0
    )

    # Sales count / total (over distinct sales, not the tender join).
    sales_stmt = (
        select(
            func.count(Sale.id).label("count"),
            func.coalesce(func.sum(Sale.total), 0).label("total"),
        )
        .where(Sale.cash_session_id == cash_session_id)
        .where(Sale.status == "completed")
    )
    sales_row = (await db.execute(sales_stmt)).one()

    # Manual movements.
    movements_stmt = (
        select(
            CashMovement.direction,
            func.coalesce(func.sum(CashMovement.amount), 0).label("total"),
        )
        .where(CashMovement.cash_session_id == cash_session_id)
        .group_by(CashMovement.direction)
    )
    movements = {r.direction: float(r.total) for r in (await db.execute(movements_stmt)).all()}

    computed_expected = float(await compute_expected_cash(db, session))

    return CashSessionReportData(
        session_id=session.id,
        status=session.status,
        opened_by=session.opened_by,
        opened_at=session.opened_at,
        closed_at=session.closed_at,
        opening_float=float(session.opening_float or 0),
        sales_count=int(sales_row.count),
        sales_total=float(sales_row.total),
        cash_in=movements.get("in", 0.0),
        cash_out=movements.get("out", 0.0),
        cash_sales_total=float(cash_sales_total),
        computed_expected_cash=computed_expected,
        expected_cash=float(session.expected_cash) if session.expected_cash is not None else None,
        counted_cash=float(session.counted_cash) if session.counted_cash is not None else None,
        difference=float(session.difference) if session.difference is not None else None,
        by_method=by_method,
    )
