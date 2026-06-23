"""Sales (POS tickets) CRUD.

``create_sale`` is the heart of the POS. It *wraps* the existing enrollment /
renewal CRUD so a membership line produces a subscription + payment exactly like
today, then records the line items and the tender ledger and commits once.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.payment_methods import MIXED, is_cash
from app.crud.memberships.enrollment import (
    create_member_enrollment_with_standing_booking,
    renew_subscription_with_standing_booking,
)
from app.crud.memberships.payments import create_payment
from app.models.posModel import Sale, SaleLineItem, SalePayment

from .cash_sessions import get_open_cash_session


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


@dataclass
class SaleLineInput:
    """One line of a sale.

    line_type:
      * ``membership_new``      -> enrolls a brand-new member (needs full_name, plan_id)
      * ``membership_renewal``  -> renews an existing member (needs member_id, plan_id)
      * ``product``             -> a catalog product (Phase 2; needs product_id)
      * ``manual``              -> a free-form charge (needs description + unit_price)
    """

    line_type: str
    description: str = ""
    quantity: int = 1
    unit_price: Optional[Decimal | float] = None  # membership: overrides plan price when set
    discount: Decimal | float = 0
    # membership fields
    plan_id: Optional[int] = None
    member_id: Optional[int] = None          # renewal
    full_name: Optional[str] = None          # new enrollment
    email: Optional[str] = None
    phone_number: Optional[str] = None
    start_at: Optional[datetime] = None
    template_id: Optional[int] = None
    seat_id: Optional[int] = None
    # product field (Phase 2)
    product_id: Optional[int] = None


@dataclass
class SalePaymentInput:
    """A single tender (how part of the ticket was paid)."""

    method: str
    amount: Decimal | float
    provider: Optional[str] = None
    provider_payment_id: Optional[str] = None
    external_reference: Optional[str] = None


def _sale_query_with_relations():
    return select(Sale).options(
        selectinload(Sale.person),
        selectinload(Sale.line_items),
        selectinload(Sale.sale_payments),
        selectinload(Sale.cash_session),
    )


async def get_sale(db: AsyncSession, sale_id: int) -> Optional[Sale]:
    result = await db.execute(_sale_query_with_relations().where(Sale.id == sale_id))
    return result.scalars().first()


async def get_sales(
    db: AsyncSession,
    *,
    limit: int = 100,
    offset: int = 0,
    cash_session_id: Optional[int] = None,
    status: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> List[Sale]:
    query = _sale_query_with_relations()
    if cash_session_id is not None:
        query = query.where(Sale.cash_session_id == cash_session_id)
    if status:
        query = query.where(Sale.status == status)
    if start_date is not None:
        query = query.where(Sale.created_at >= start_date)
    if end_date is not None:
        query = query.where(Sale.created_at <= end_date)
    query = query.order_by(Sale.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    return list(result.scalars().all())


async def create_sale(
    db: AsyncSession,
    *,
    line_items: List[SaleLineInput],
    tenders: List[SalePaymentInput],
    person_id: Optional[int] = None,
    sold_by: Optional[int] = None,
    note: Optional[str] = None,
    require_open_cash_for_cash: bool = True,
    commit: bool = True,
) -> Sale:
    """Create a POS sale atomically, reusing enrollment/renewal for membership lines."""
    if not line_items:
        raise ValueError("La venta no tiene conceptos.")
    nonzero_tenders = [t for t in tenders if _dec(t.amount) > 0]
    if not nonzero_tenders:
        raise ValueError("La venta no tiene pagos.")

    open_session = await get_open_cash_session(db)
    has_cash_tender = any(is_cash(t.method) for t in nonzero_tenders)
    if require_open_cash_for_cash and has_cash_tender and open_session is None:
        raise ValueError("Abre la caja antes de cobrar en efectivo.")

    # A single tender keeps its own method; split tenders mark the membership
    # anchor payment as 'mixed' (the real breakdown lives in sale_payments).
    primary_method = nonzero_tenders[0].method if len(nonzero_tenders) == 1 else MIXED

    sale = Sale(
        person_id=person_id,
        cash_session_id=open_session.id if open_session else None,
        status="open",
        sold_by=sold_by,
        note=note,
    )
    db.add(sale)
    await db.flush()

    subtotal = Decimal("0")
    anchor_payment_id: Optional[int] = None

    for li in line_items:
        if li.line_type in ("membership_new", "membership_renewal"):
            item, line_total, payment_id = await _process_membership_line(
                db, sale, li, primary_method, sold_by
            )
        elif li.line_type == "manual":
            item, line_total, payment_id = await _process_manual_line(
                db, sale, li, primary_method, sold_by
            )
        elif li.line_type == "product":
            item, line_total, payment_id = await _process_product_line(
                db, sale, li, primary_method, sold_by
            )
        else:
            raise ValueError(f"Tipo de línea desconocido: {li.line_type}")

        db.add(item)
        subtotal += line_total
        if anchor_payment_id is None:
            anchor_payment_id = payment_id

    total = subtotal  # Phase 1: no sale-level discount/tax
    paid = sum((_dec(t.amount) for t in nonzero_tenders), Decimal("0"))
    if paid + Decimal("0.01") < total:
        raise ValueError(f"El pago (${paid:.2f}) es menor al total (${total:.2f}).")

    # Change is given out of cash, so the cash booked to the tender ledger must be
    # the NET kept in the drawer (tendered - change). Otherwise the corte de caja
    # over-counts expected cash by the change handed back. sale.amount_paid keeps
    # the gross tendered (for the receipt's change line).
    remaining_change = max(paid - total, Decimal("0"))
    for t in nonzero_tenders:
        amount = _dec(t.amount)
        if remaining_change > 0 and is_cash(t.method):
            reduction = min(remaining_change, amount)
            amount -= reduction
            remaining_change -= reduction
        if amount <= 0:
            continue
        db.add(
            SalePayment(
                sale_id=sale.id,
                payment_id=anchor_payment_id,
                amount=amount,
                method=t.method,
            )
        )

    sale.subtotal = subtotal
    sale.total = total
    sale.amount_paid = paid
    sale.status = "completed"
    sale.completed_at = datetime.now(timezone.utc)

    await db.flush()
    if commit:
        await db.commit()

    refreshed = await get_sale(db, sale.id)
    return refreshed if refreshed is not None else sale


async def _process_membership_line(db, sale, li: SaleLineInput, primary_method, sold_by):
    if not li.plan_id:
        raise ValueError("La línea de membresía requiere un plan.")

    if li.line_type == "membership_new":
        if not li.full_name:
            raise ValueError("El alta de membresía requiere el nombre del socio.")
        person, subscription, payment, plan, sb_id, stats = (
            await create_member_enrollment_with_standing_booking(
                db=db,
                full_name=li.full_name,
                email=li.email,
                phone_number=li.phone_number,
                plan_id=li.plan_id,
                start_at=li.start_at,
                payment_method=primary_method,
                payment_amount=li.unit_price,
                payment_status="COMPLETED",
                recorded_by=sold_by,
                template_id=li.template_id,
                seat_id=li.seat_id,
                auto_materialize=True,
                commit=False,
            )
        )
        if sale.person_id is None:
            sale.person_id = person.id
        description = li.description or f"Alta de membresía: {plan.name}"
    else:  # membership_renewal
        if not li.member_id:
            raise ValueError("La renovación requiere un socio.")
        subscription, payment, plan, sb_id, stats = (
            await renew_subscription_with_standing_booking(
                db=db,
                member_id=li.member_id,
                plan_id=li.plan_id,
                template_id=li.template_id,
                seat_id=li.seat_id,
                start_at=li.start_at,
                payment_method=primary_method,
                payment_amount=li.unit_price,
                payment_status="COMPLETED",
                recorded_by=sold_by,
                commit=False,
            )
        )
        if sale.person_id is None:
            sale.person_id = li.member_id
        description = li.description or f"Renovación de membresía: {plan.name}"

    line_total = _dec(payment.amount)
    meta = {
        "template_id": li.template_id,
        "seat_id": li.seat_id,
        "standing_booking_id": sb_id,
        "plan_name": plan.name,
    }
    item = SaleLineItem(
        sale_id=sale.id,
        line_type=li.line_type,
        description=description,
        quantity=1,
        unit_price=line_total,
        discount=Decimal("0"),
        line_total=line_total,
        plan_id=li.plan_id,
        subscription_id=subscription.id,
        payment_id=payment.id,
        meta=meta,
    )
    return item, line_total, payment.id


async def _process_manual_line(db, sale, li: SaleLineInput, primary_method, sold_by):
    if sale.person_id is None:
        raise ValueError("Un cargo manual requiere un socio.")
    line_total = _dec(li.unit_price) * (li.quantity or 1) - _dec(li.discount)
    if line_total <= 0:
        raise ValueError("El cargo manual debe tener un importe mayor a cero.")
    payment = await create_payment(
        db=db,
        person_id=sale.person_id,
        amount=line_total,
        method=primary_method,
        status="COMPLETED",
        comment=li.description or "Cargo POS",
        recorded_by=sold_by,
        commit=False,
    )
    item = SaleLineItem(
        sale_id=sale.id,
        line_type="manual",
        description=li.description or "Cargo manual",
        quantity=li.quantity or 1,
        unit_price=_dec(li.unit_price),
        discount=_dec(li.discount),
        line_total=line_total,
        payment_id=payment.id,
    )
    return item, line_total, payment.id


async def _process_product_line(db, sale, li: SaleLineInput, primary_method, sold_by):
    from app.crud.pos.products import adjust_stock, get_product_by_id

    if not li.product_id:
        raise ValueError("La línea de producto requiere un producto.")
    product = await get_product_by_id(db, li.product_id)
    if product is None or not product.is_active:
        raise ValueError("Producto no disponible.")

    qty = li.quantity or 1
    unit_price = _dec(li.unit_price) if li.unit_price is not None else _dec(product.price)
    line_total = unit_price * qty - _dec(li.discount)
    if line_total <= 0:
        raise ValueError("El importe del producto debe ser mayor a cero.")

    if product.track_stock:
        if product.stock_qty is None or product.stock_qty < qty:
            raise ValueError(f"Stock insuficiente de {product.name}.")
        await adjust_stock(db, product.id, -qty, commit=False)

    payment = await create_payment(
        db=db,
        person_id=sale.person_id,  # may be None (walk-in)
        amount=line_total,
        method=primary_method,
        status="COMPLETED",
        comment=f"Producto: {product.name}",
        recorded_by=sold_by,
        commit=False,
    )
    item = SaleLineItem(
        sale_id=sale.id,
        line_type="product",
        description=li.description or product.name,
        quantity=qty,
        unit_price=unit_price,
        discount=_dec(li.discount),
        line_total=line_total,
        product_id=product.id,
        payment_id=payment.id,
    )
    return item, line_total, payment.id


async def void_sale(db: AsyncSession, sale_id: int, *, commit: bool = True) -> Optional[Sale]:
    """Mark a sale as voided. Does not auto-reverse memberships (manual for v1)."""
    sale = await db.get(Sale, sale_id)
    if sale is None:
        return None
    if sale.status == "voided":
        return sale
    sale.status = "voided"
    await db.flush()
    if commit:
        await db.commit()
    return await get_sale(db, sale_id)
