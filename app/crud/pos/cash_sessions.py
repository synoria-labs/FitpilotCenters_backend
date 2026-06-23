"""Cash register (caja) CRUD: open, movements, close + corte de caja math."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.payment_methods import CASH_METHODS
from app.models.posModel import CashSession, CashMovement, Sale, SalePayment


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


async def get_open_cash_session(db: AsyncSession) -> Optional[CashSession]:
    """The single currently-open caja, or None."""
    result = await db.execute(
        select(CashSession)
        .where(CashSession.status == "open")
        .order_by(CashSession.opened_at.desc())
    )
    return result.scalars().first()


async def get_cash_session_by_id(db: AsyncSession, cash_session_id: int) -> Optional[CashSession]:
    return await db.get(CashSession, cash_session_id)


async def open_cash_session(
    db: AsyncSession,
    *,
    opened_by: int,
    opening_float: Decimal | float = Decimal("0"),
    notes: Optional[str] = None,
    commit: bool = True,
) -> CashSession:
    """Open the shared caja. Fails if one is already open."""
    existing = await get_open_cash_session(db)
    if existing is not None:
        raise ValueError("Ya hay una caja abierta. Ciérrala antes de abrir otra.")

    session = CashSession(
        opened_by=opened_by,
        opening_float=_dec(opening_float),
        notes=notes,
        status="open",
    )
    db.add(session)
    await db.flush()
    if commit:
        await db.commit()
        await db.refresh(session)
    return session


async def record_cash_movement(
    db: AsyncSession,
    *,
    cash_session_id: int,
    direction: str,
    amount: Decimal | float,
    reason: Optional[str] = None,
    created_by: Optional[int] = None,
    commit: bool = True,
) -> CashMovement:
    """Record a manual cash movement (retiro='out' / ingreso='in') against an open caja."""
    if direction not in ("in", "out"):
        raise ValueError("Dirección de movimiento inválida (usa 'in' u 'out').")
    if _dec(amount) <= 0:
        raise ValueError("El monto del movimiento debe ser mayor a cero.")

    session = await db.get(CashSession, cash_session_id)
    if session is None or session.status != "open":
        raise ValueError("La caja no está abierta.")

    movement = CashMovement(
        cash_session_id=cash_session_id,
        direction=direction,
        amount=_dec(amount),
        reason=reason,
        created_by=created_by,
    )
    db.add(movement)
    await db.flush()
    if commit:
        await db.commit()
        await db.refresh(movement)
    return movement


async def _sum_cash_sale_payments(db: AsyncSession, cash_session_id: int) -> Decimal:
    """Sum of cash tenders booked to completed sales of this caja."""
    cash_methods = [m.lower() for m in CASH_METHODS]
    stmt = (
        select(func.coalesce(func.sum(SalePayment.amount), 0))
        .select_from(SalePayment)
        .join(Sale, SalePayment.sale_id == Sale.id)
        .where(Sale.cash_session_id == cash_session_id)
        .where(Sale.status == "completed")
        .where(func.lower(SalePayment.method).in_(cash_methods))
    )
    return _dec((await db.execute(stmt)).scalar_one() or 0)


async def _sum_movements(db: AsyncSession, cash_session_id: int, direction: str) -> Decimal:
    stmt = (
        select(func.coalesce(func.sum(CashMovement.amount), 0))
        .where(CashMovement.cash_session_id == cash_session_id)
        .where(CashMovement.direction == direction)
    )
    return _dec((await db.execute(stmt)).scalar_one() or 0)


async def compute_expected_cash(db: AsyncSession, session: CashSession) -> Decimal:
    """fondo inicial + ventas en efectivo + ingresos − retiros."""
    cash_sales = await _sum_cash_sale_payments(db, session.id)
    movements_in = await _sum_movements(db, session.id, "in")
    movements_out = await _sum_movements(db, session.id, "out")
    return _dec(session.opening_float) + cash_sales + movements_in - movements_out


async def close_cash_session(
    db: AsyncSession,
    *,
    cash_session_id: int,
    counted_cash: Decimal | float,
    closed_by: Optional[int] = None,
    notes: Optional[str] = None,
    commit: bool = True,
) -> CashSession:
    """Close the caja: compute expected cash, store the counted amount + difference."""
    session = await db.get(CashSession, cash_session_id)
    if session is None:
        raise ValueError("Caja no encontrada.")
    if session.status != "open":
        raise ValueError("La caja ya está cerrada.")

    expected = await compute_expected_cash(db, session)
    counted = _dec(counted_cash)

    session.expected_cash = expected
    session.counted_cash = counted
    session.difference = counted - expected
    session.closed_by = closed_by
    session.closed_at = datetime.now(timezone.utc)
    session.status = "closed"
    if notes:
        session.notes = notes

    await db.flush()
    if commit:
        await db.commit()
        await db.refresh(session)
    return session
