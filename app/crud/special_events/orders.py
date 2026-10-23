"""Event tickets: seat holds while paying, manual sales, cancellations and listings.

One order per participant. While a customer pays, the order is ``awaiting_payment`` and holds a
specific bike until ``hold_expires_at`` (the event's ``hold_minutes``, also the payment link's
expiry at Mercado Pago). Availability counts those live holds, so two customers can never be
sold the last spot; the partial unique indexes (seat, participant) back that up in the database.

A ticket is independent of memberships: creating or paying an order never creates, renews or
consumes a subscription, and a buyer who is not in the system is not turned into a member.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.locks import lock_special_event
from app.crud.memberships import payments as payments_crud
from app.models import People, Seat
from app.models.specialEventModel import (
    EVENT_CANCELED,
    EVENT_COMPLETED,
    ORDER_ACTIVE_STATUSES,
    ORDER_AWAITING_PAYMENT,
    ORDER_CANCELED,
    ORDER_CHANNEL_ADMIN,
    ORDER_CHANNEL_WHATSAPP,
    ORDER_EXPIRED,
    ORDER_NEEDS_REVIEW,
    ORDER_PAID,
    SpecialEventOrder,
)

from .common import SpecialEventError, participant_key, utcnow
from .events import availability, expire_stale_holds, get_event

MANUAL_PAYMENT_METHODS = ("cash", "card", "transfer", "courtesy")


def new_external_reference() -> str:
    """Reference echoed back by Mercado Pago; the ``event-`` prefix routes the webhook."""
    return f"event-{uuid.uuid4().hex}"


async def find_person_by_phone(db: AsyncSession, phone: Optional[str]) -> Optional[int]:
    """Link an existing person (member or not) whose phone/WhatsApp matches. Never creates one."""
    from app.crud.whatsappCrud import _phone_match_keys

    keys = list(_phone_match_keys(phone))
    if not keys:
        return None
    phone_digits = func.regexp_replace(func.coalesce(People.phone_number, ""), r"\D", "", "g")
    wa_digits = func.regexp_replace(func.coalesce(People.wa_id, ""), r"\D", "", "g")
    row = (
        await db.execute(
            select(People.id)
            .where(People.deleted_at.is_(None))
            .where(or_(phone_digits.in_(keys), wa_digits.in_(keys)))
            .order_by(People.id)
            .limit(1)
        )
    ).first()
    return row[0] if row else None


async def get_order(db: AsyncSession, order_id: int, *, for_update: bool = False) -> Optional[SpecialEventOrder]:
    stmt = select(SpecialEventOrder).where(SpecialEventOrder.id == int(order_id))
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalars().first()


async def get_order_by_reference(
    db: AsyncSession, reference: str, *, for_update: bool = False
) -> Optional[SpecialEventOrder]:
    stmt = select(SpecialEventOrder).where(SpecialEventOrder.external_reference == reference)
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalars().first()


@dataclass
class HoldResult:
    order: SpecialEventOrder
    # True when the participant already had a live hold or a paid ticket (nothing new created).
    reused: bool


async def create_hold(
    db: AsyncSession,
    event_id: int,
    *,
    participant_name: str,
    participant_phone: Optional[str],
    conversation_id: Optional[int] = None,
    buyer_wa_id: Optional[str] = None,
) -> HoldResult:
    """Create a timed hold or an open payment order after checking availability.

    Under the event lock: expire stale holds, reuse the participant's live order if any (a retry
    must not create a second hold or a second payment link), otherwise check availability and
    insert. Open links deduplicate the participant without reserving a bike or capacity.
    The caller commits (releasing the lock) and then attaches the payment link.
    """
    name = " ".join((participant_name or "").split())
    if not name:
        raise SpecialEventError("Necesito el nombre completo del participante.")
    key = participant_key(participant_phone)
    if not key:
        raise SpecialEventError("Necesito el número de teléfono del participante.")

    await lock_special_event(db, event_id)
    event = await get_event(db, event_id)
    if event is None:
        raise SpecialEventError("No encontré ese evento.")
    now = utcnow()
    await expire_stale_holds(db, event_id=event.id, now=now)

    existing = (
        await db.execute(
            select(SpecialEventOrder).where(
                SpecialEventOrder.event_id == event.id,
                SpecialEventOrder.participant_key == key,
                SpecialEventOrder.status.in_(ORDER_ACTIVE_STATUSES),
            )
        )
    ).scalars().first()
    if existing is not None:
        if existing.status == ORDER_AWAITING_PAYMENT and not existing.payment_link_expires:
            avail = await availability(db, event, now=now)
            if not avail.sales_open:
                raise SpecialEventError(avail.sales_closed_reason or "No hay lugares disponibles; no se reenviará el enlace.")
        return HoldResult(order=existing, reused=True)

    avail = await availability(db, event, now=now)
    if not avail.sales_open:
        raise SpecialEventError(avail.sales_closed_reason or "Este evento no está a la venta.")
    seat_id = None
    if avail.offered_seats:
        if not avail.free_seat_ids:
            raise SpecialEventError("Ya no hay lugares disponibles.")
        if event.payment_link_expires:
            seat_id = avail.free_seat_ids[0]

    order = SpecialEventOrder(
        event_id=event.id,
        person_id=await find_person_by_phone(db, participant_phone),
        participant_name=name[:200],
        participant_phone=(participant_phone or "").strip()[:32] or None,
        participant_key=key,
        seat_id=seat_id,
        channel=ORDER_CHANNEL_WHATSAPP,
        conversation_id=conversation_id,
        buyer_wa_id=buyer_wa_id,
        status=ORDER_AWAITING_PAYMENT,
        amount=Decimal(event.price),
        currency=event.currency,
        external_reference=new_external_reference(),
        payment_link_expires=event.payment_link_expires,
        hold_expires_at=now + timedelta(minutes=int(event.hold_minutes)) if event.payment_link_expires else None,
        created_at=now,
        updated_at=now,
    )
    db.add(order)
    await db.flush()
    return HoldResult(order=order, reused=False)


async def add_manual_participant(
    db: AsyncSession,
    event_id: int,
    *,
    actor_account_id: Optional[int],
    participant_name: str,
    participant_phone: Optional[str],
    payment_method: str,
    amount=None,
    seat_id: Optional[int] = None,
    notes: Optional[str] = None,
) -> SpecialEventOrder:
    """Register a participant paid at the front desk (cash/card/transfer) or as a courtesy.

    Same capacity, seat and duplicate rules as the WhatsApp purchase; records the Payment so the
    money shows up in finances, but never touches a membership.
    """
    method = (payment_method or "").strip().lower()
    if method not in MANUAL_PAYMENT_METHODS:
        raise SpecialEventError("Método de pago no válido (efectivo, tarjeta, transferencia o cortesía).")
    name = " ".join((participant_name or "").split())
    if not name:
        raise SpecialEventError("Indica el nombre del participante.")
    key = participant_key(participant_phone)
    if not key:
        raise SpecialEventError("Indica el teléfono del participante.")

    await lock_special_event(db, event_id)
    event = await get_event(db, event_id)
    if event is None:
        raise SpecialEventError("No encontré ese evento.")
    if event.status in (EVENT_CANCELED, EVENT_COMPLETED):
        raise SpecialEventError("El evento está cancelado o terminado.")
    if event.capacity is None:
        raise SpecialEventError("Define el cupo del evento antes de registrar participantes.")
    now = utcnow()
    await expire_stale_holds(db, event_id=event.id, now=now)

    duplicate = (
        await db.execute(
            select(SpecialEventOrder.id).where(
                SpecialEventOrder.event_id == event.id,
                SpecialEventOrder.participant_key == key,
                SpecialEventOrder.status.in_(ORDER_ACTIVE_STATUSES),
            )
        )
    ).first()
    if duplicate is not None:
        raise SpecialEventError("Ese participante ya tiene un lugar apartado o pagado en este evento.")

    avail = await availability(db, event, now=now)
    if not avail.available:
        raise SpecialEventError("Ya no hay lugares disponibles.")
    if avail.offered_seats:
        if seat_id is None:
            seat_id = avail.free_seat_ids[0]
        elif int(seat_id) not in avail.free_seat_ids:
            raise SpecialEventError("Esa bicicleta no está disponible para este evento.")
    else:
        seat_id = None

    if method == "courtesy":
        value = Decimal("0.00")
    else:
        value = Decimal(str(amount)) if amount is not None else Decimal(event.price)
        if value < 0:
            raise SpecialEventError("El importe no puede ser negativo.")

    person_id = await find_person_by_phone(db, participant_phone)
    order = SpecialEventOrder(
        event_id=event.id,
        person_id=person_id,
        participant_name=name[:200],
        participant_phone=(participant_phone or "").strip()[:32] or None,
        participant_key=key,
        seat_id=int(seat_id) if seat_id is not None else None,
        channel=ORDER_CHANNEL_ADMIN,
        status=ORDER_PAID,
        amount=value,
        currency=event.currency,
        external_reference=new_external_reference(),
        payment_method=method,
        paid_at=now,
        notes=(notes or "").strip() or None,
        created_by=actor_account_id,
        updated_by=actor_account_id,
        created_at=now,
        updated_at=now,
    )
    db.add(order)
    await db.flush()
    if value > 0:
        payment = await payments_crud.create_payment(
            db,
            person_id=person_id,
            amount=value,
            method=method,
            external_reference=order.external_reference,
            comment=f"Evento especial «{event.name}» · {name}",
            recorded_by=actor_account_id,
            commit=False,
        )
        order.payment_id = payment.id
        await db.flush()
    return order


async def cancel_order(
    db: AsyncSession, order_id: int, *, actor_account_id: Optional[int], reason: str
) -> SpecialEventOrder:
    """Void an unpaid order (frees its bike). Paid tickets go through the refund flow."""
    reason = (reason or "").strip() or "Cancelado por el personal"
    order = await get_order(db, order_id)
    if order is None:
        raise SpecialEventError("No encontré esa compra.")
    await lock_special_event(db, order.event_id)
    order = await get_order(db, order_id, for_update=True)
    if order.status == ORDER_CANCELED:
        return order
    if order.status == ORDER_PAID or (order.status == ORDER_NEEDS_REVIEW and order.mp_payment_id):
        raise SpecialEventError(
            "Esta entrada tiene un pago registrado; usa «Reembolsar» para cancelarla."
        )
    if order.status not in (ORDER_AWAITING_PAYMENT, ORDER_EXPIRED, ORDER_NEEDS_REVIEW):
        raise SpecialEventError("Esta compra ya no se puede cancelar.")
    now = utcnow()
    order.status = ORDER_CANCELED
    order.canceled_at = now
    order.cancel_reason = reason
    order.updated_by = actor_account_id
    order.updated_at = now
    await db.flush()
    return order


async def live_orders_for_conversation(
    db: AsyncSession, *, conversation_id: Optional[int], wa_id: Optional[str]
) -> List[SpecialEventOrder]:
    """Orders the WhatsApp buyer can see/act on (scoped to their conversation or number)."""
    conditions = []
    if conversation_id is not None:
        conditions.append(SpecialEventOrder.conversation_id == int(conversation_id))
    if wa_id:
        conditions.append(SpecialEventOrder.buyer_wa_id == wa_id)
        key = participant_key(wa_id)
        if key:
            conditions.append(SpecialEventOrder.participant_key == key)
    if not conditions:
        return []
    rows = await db.execute(
        select(SpecialEventOrder)
        .where(or_(*conditions))
        .order_by(SpecialEventOrder.created_at.desc())
        .limit(20)
    )
    return list(rows.scalars().all())


@dataclass
class OrderRow:
    id: int
    event_id: int
    participant_name: str
    participant_phone: Optional[str]
    person_id: Optional[int]
    seat_id: Optional[int]
    seat_label: Optional[str]
    status: str
    channel: str
    amount: Decimal
    currency: str
    payment_method: Optional[str]
    paid_at: Optional[datetime]
    hold_expires_at: Optional[datetime]
    payment_link_expires: bool
    manual_refund_required: bool
    refund_reviewed_by: Optional[int]
    refund_reviewed_at: Optional[datetime]
    mp_status: Optional[str]
    mp_payment_id: Optional[str]
    external_reference: Optional[str]
    review_reason: Optional[str]
    cancel_reason: Optional[str]
    refunded_at: Optional[datetime]
    notes: Optional[str]
    created_at: datetime


async def list_orders(db: AsyncSession, event_id: int) -> List[OrderRow]:
    rows = (
        await db.execute(
            select(SpecialEventOrder, Seat.label)
            .outerjoin(Seat, Seat.id == SpecialEventOrder.seat_id)
            .where(SpecialEventOrder.event_id == int(event_id))
            .order_by(SpecialEventOrder.created_at)
        )
    ).all()
    return [
        OrderRow(
            id=o.id,
            event_id=o.event_id,
            participant_name=o.participant_name,
            participant_phone=o.participant_phone,
            person_id=o.person_id,
            seat_id=o.seat_id,
            seat_label=label,
            status=o.status,
            channel=o.channel,
            amount=o.amount,
            currency=o.currency,
            payment_method=o.payment_method,
            paid_at=o.paid_at,
            hold_expires_at=o.hold_expires_at,
            payment_link_expires=o.payment_link_expires,
            manual_refund_required=o.manual_refund_required,
            refund_reviewed_by=o.refund_reviewed_by,
            refund_reviewed_at=o.refund_reviewed_at,
            mp_status=o.mp_status,
            mp_payment_id=o.mp_payment_id,
            external_reference=o.external_reference,
            review_reason=o.review_reason,
            cancel_reason=o.cancel_reason,
            refunded_at=o.refunded_at,
            notes=o.notes,
            created_at=o.created_at,
        )
        for o, label in rows
    ]
