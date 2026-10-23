"""Mercado Pago Checkout Pro for special-event tickets.

Policy, in one place:

* **Holds.** Starting a purchase holds one bike for ``event.hold_minutes`` (30 by default). The
  payment link expires at the same instant, only accepts methods that settle immediately (cards,
  Mercado Pago balance; no OXXO/ATM vouchers or bank transfers) and runs in ``binary_mode``, so a
  payment is approved or rejected while the seat is still held.
* **Confirmation** happens only here, from Mercado Pago's own record of the payment (fetched by
  id, never trusted from the webhook body), after checking reference, amount and currency. The
  WhatsApp confirmation is sent by this module — the chatbot never confirms a ticket by itself.
* **Idempotency.** Every notification is settled under the event lock with the order row locked;
  a repeated notification for the same payment is a no-op, a Payment row is unique per Mercado
  Pago id, and refunds carry an idempotency key.
* **Late or orphan money is never kept silently.** A payment approved after the hold expired is
  honored if a spot is still free, otherwise refunded; a payment for a canceled purchase or a
  canceled event, or a second payment for a ticket already paid, is refunded; anything the
  system cannot settle (amount mismatch, failed refund) becomes ``needs_review`` and is logged
  in ``webhook_logs`` for reconciliation, reusing the existing refund/reconciliation flow.
* **No Mercado Pago, no sale.** If the gateway is not configured the purchase is refused with an
  explicit message; it is never turned into a ticket confirmed by text.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.mercadopago_config import mercadopago_config
from app.crud import whatsappCrud
from app.crud.locks import lock_special_event
from app.crud.special_events import events as events_crud
from app.crud.special_events import orders as orders_crud
from app.crud.special_events.common import (
    SpecialEventError,
    local_schedule_label,
    money_label,
    utcnow,
)
from app.db.postgresql import async_session_factory
from app.models import Payment, Seat
from app.models.specialEventModel import (
    EVENT_CANCELED,
    ORDER_ACTIVE_STATUSES,
    ORDER_AWAITING_PAYMENT,
    ORDER_CANCELED,
    ORDER_EXPIRED,
    ORDER_NEEDS_REVIEW,
    ORDER_PAID,
    ORDER_REFUND_PENDING,
    ORDER_REFUNDED,
    SpecialEvent,
    SpecialEventOrder,
)
from app.services import mercadopago_service

logger = logging.getLogger(__name__)

# Voucher/transfer methods settle hours or days later — longer than any seat hold.
EXCLUDED_PAYMENT_TYPES = ("ticket", "atm", "bank_transfer")
REFERENCE_PREFIX = "event-"

FollowUp = Callable[[AsyncSession], Awaitable[None]]


def is_event_reference(reference: Optional[str]) -> bool:
    return bool(reference) and str(reference).startswith(REFERENCE_PREFIX)


# ---------------------------------------------------------------------------------------------
# Starting a purchase (chatbot)
# ---------------------------------------------------------------------------------------------
@dataclass
class PurchaseStart:
    ok: bool
    message: str
    order_id: Optional[int] = None
    init_point: Optional[str] = None
    hold_expires_at: Optional[datetime] = None
    already_paid: bool = False


async def start_whatsapp_purchase(
    db: AsyncSession,
    *,
    event_id: int,
    participant_name: str,
    participant_phone: Optional[str],
    conversation_id: Optional[int],
    wa_id: Optional[str],
) -> PurchaseStart:
    """Hold a seat and return the Mercado Pago link. Never confirms anything by itself."""
    if not mercadopago_config.is_configured():
        _reconcile(
            db,
            "special_event_mp_unavailable",
            None,
            None,
            detail={"event_id": event_id, "conversation_id": conversation_id},
        )
        await db.commit()
        logger.error("Special event purchase refused: Mercado Pago is not configured")
        return PurchaseStart(
            ok=False,
            message=(
                "No puedo generar el enlace de pago en este momento: el cobro en línea no está "
                "disponible. Avisé al equipo y un asesor te contactará. Tu lugar todavía NO está "
                "reservado."
            ),
        )

    try:
        hold = await orders_crud.create_hold(
            db,
            event_id,
            participant_name=participant_name,
            participant_phone=participant_phone,
            conversation_id=conversation_id,
            buyer_wa_id=wa_id,
        )
    except SpecialEventError as exc:
        await db.rollback()
        return PurchaseStart(ok=False, message=str(exc))

    order = hold.order
    event = await events_crud.get_event(db, order.event_id)
    if hold.reused and order.status == ORDER_PAID:
        await db.commit()
        return PurchaseStart(
            ok=True,
            already_paid=True,
            order_id=order.id,
            message=f"{order.participant_name} ya tiene su lugar pagado y confirmado para «{event.name}».",
        )
    if hold.reused and order.mp_init_point:
        await db.commit()
        return PurchaseStart(
            ok=True,
            order_id=order.id,
            init_point=order.mp_init_point,
            hold_expires_at=order.hold_expires_at,
            message="Ya había un lugar apartado para este participante; es el mismo enlace de pago.",
        )

    order_id = order.id
    reference = order.external_reference
    expires = order.hold_expires_at
    amount = order.amount
    currency = order.currency
    name = order.participant_name
    title = f"{event.name} - entrada"
    item_id = f"special-event-{event.id}"
    await db.commit()  # the hold is durable before we call out

    try:
        preference = await mercadopago_service.create_preference(
            amount=amount,
            description=title,
            external_reference=reference,
            payer_name=name,
            currency=currency,
            expires_at=expires,
            binary_mode=True,
            excluded_payment_types=EXCLUDED_PAYMENT_TYPES,
            item_id=item_id,
        )
    except Exception:  # noqa: BLE001 - any failure releases the seat right away
        logger.exception("Mercado Pago preference failed for special event order %s", order_id)
        try:
            await orders_crud.cancel_order(
                db, order_id, actor_account_id=None, reason="No se pudo generar el link de pago"
            )
            await db.commit()
        except Exception:  # noqa: BLE001
            await db.rollback()
            logger.exception("Could not release hold of order %s", order_id)
        return PurchaseStart(
            ok=False,
            message=(
                "No pude generar el enlace de pago en este momento. Intenta de nuevo en unos "
                "minutos; tu lugar no quedó apartado."
            ),
        )

    order = await orders_crud.get_order(db, order_id, for_update=True)
    order.mp_preference_id = preference.get("preference_id")
    order.mp_init_point = preference.get("init_point")
    order.updated_at = utcnow()
    await db.commit()
    return PurchaseStart(
        ok=True,
        order_id=order_id,
        init_point=order.mp_init_point,
        hold_expires_at=expires,
        message="Lugar apartado.",
    )


# ---------------------------------------------------------------------------------------------
# Settling Mercado Pago payments
# ---------------------------------------------------------------------------------------------
async def process_payment_notification(payment_id, payment: Optional[dict] = None) -> str:
    """Settle one Mercado Pago payment for an event order. Safe to call any number of times.

    Raises on transient failures (DB/network) so the webhook can ask Mercado Pago to retry.
    """
    payment_id = str(payment_id)
    if payment is None:
        payment = await mercadopago_service.get_payment(payment_id)
    async with async_session_factory() as db:
        outcome, follow_ups = await _settle(db, payment_id, payment)
        for follow_up in follow_ups:
            try:
                await follow_up(db)
            except Exception:  # noqa: BLE001 - the state is committed; a send/refund failure is logged
                await db.rollback()
                logger.exception("Special event follow-up failed (payment %s)", payment_id)
    logger.info("Special event payment %s settled: %s", payment_id, outcome)
    return outcome


async def _settle(db: AsyncSession, payment_id: str, payment: dict) -> Tuple[str, List[FollowUp]]:
    reference = str(payment.get("external_reference") or "")
    status = (payment.get("status") or "").lower()

    # Read the event id unlocked, take the event lock, THEN lock the row (see lock_special_event).
    head = (
        await db.execute(
            select(SpecialEventOrder.id, SpecialEventOrder.event_id).where(
                SpecialEventOrder.external_reference == reference
            )
        )
    ).first()
    if head is None:
        if status == "approved":
            _reconcile(
                db,
                "special_event_unmatched_payment",
                payment_id,
                reference,
                detail={"amount": payment.get("transaction_amount"), "status": status},
            )
            await db.commit()
            logger.error("Approved payment %s has no event order (ref=%s)", payment_id, reference)
            return "unmatched", []
        return "ignored", []

    order_id, event_id = head
    await lock_special_event(db, event_id)
    order = await orders_crud.get_order(db, order_id, for_update=True)
    event = await events_crud.get_event(db, event_id)
    now = utcnow()
    order.mp_status = status[:40] or None
    order.mp_status_detail = (str(payment.get("status_detail") or "")[:120]) or None
    order.updated_at = now

    if status == "approved":
        return await _settle_approved(db, order, event, payment_id, payment, now)

    if status in ("refunded", "charged_back"):
        if order.mp_payment_id == payment_id and order.status in (
            ORDER_PAID, ORDER_REFUND_PENDING, ORDER_NEEDS_REVIEW,
        ):
            if status == "refunded":
                order.status = ORDER_REFUNDED
                order.refunded_at = order.refunded_at or now
                await _mark_payment_refunded(db, order)
            else:
                order.status = ORDER_NEEDS_REVIEW
                order.review_reason = "Contracargo en Mercado Pago: revisa el caso."
                _reconcile(db, "special_event_chargeback", payment_id, reference)
        await db.commit()
        return status, []

    # rejected / cancelled / pending / in_process: nothing gets confirmed. A rejected card can be
    # retried with the same link while the hold lasts.
    await db.commit()
    return status or "unknown", []


async def _settle_approved(
    db: AsyncSession,
    order: SpecialEventOrder,
    event: SpecialEvent,
    payment_id: str,
    payment: dict,
    now: datetime,
) -> Tuple[str, List[FollowUp]]:
    order_id = order.id
    # Mercado Pago repeats notifications: the same payment again changes nothing.
    if order.mp_payment_id == payment_id and order.status != ORDER_AWAITING_PAYMENT:
        await db.commit()
        return "duplicate_notification", []

    if order.mp_payment_id not in (None, payment_id):
        # The order already carries another payment: this one is extra money -> give it back.
        _reconcile(
            db, "special_event_extra_payment", payment_id, order.external_reference,
            detail={"order_id": order_id, "kept_payment": order.mp_payment_id},
        )
        await db.commit()
        return "extra_payment", [_refund_extra(order_id, payment_id)]

    problem = _payment_problem(order, payment)
    if problem:
        order.mp_payment_id = payment_id
        order.status = ORDER_NEEDS_REVIEW
        order.review_reason = problem
        _reconcile(db, "special_event_payment_mismatch", payment_id, order.external_reference, detail=problem)
        await db.commit()
        return "needs_review", [_notify_review(order_id)]

    if event.status == EVENT_CANCELED:
        return await _begin_refund(db, order, payment_id, "El evento fue cancelado")
    if order.status == ORDER_CANCELED:
        return await _begin_refund(db, order, payment_id, "Tu compra ya estaba cancelada cuando se acreditó el pago")
    if order.status in (ORDER_REFUND_PENDING, ORDER_REFUNDED, ORDER_NEEDS_REVIEW):
        return await _begin_refund(db, order, payment_id, "Esta compra ya no estaba vigente")

    if order.status == ORDER_AWAITING_PAYMENT:
        # The seat is still held by this order (expired holds are flipped under this same lock),
        # so confirming cannot oversell even if the clock just passed hold_expires_at.
        await _confirm(db, order, event, payment_id, now)
        await db.commit()
        return "confirmed", [_notify_confirmed(order_id, late=False)]

    if order.status == ORDER_EXPIRED:
        # Late payment: honor it only if the participant can still get a spot.
        await events_crud.expire_stale_holds(db, event_id=event.id, now=now)
        clash = (
            await db.execute(
                select(SpecialEventOrder.id).where(
                    SpecialEventOrder.event_id == event.id,
                    SpecialEventOrder.participant_key == order.participant_key,
                    SpecialEventOrder.id != order.id,
                    SpecialEventOrder.status.in_(ORDER_ACTIVE_STATUSES),
                )
            )
        ).first()
        if clash is not None:
            return await _begin_refund(
                db, order, payment_id, "Ya existe otra compra vigente para el mismo participante"
            )
        avail = await events_crud.availability(db, event, now=now)
        if not avail.sales_open:
            return await _begin_refund(
                db, order, payment_id,
                "Tu pago llegó después de que venció tu apartado y ya no quedaban lugares",
            )
        if avail.offered_seats:
            seat = order.seat_id if order.seat_id in avail.free_seat_ids else avail.free_seat_ids[0]
            order.seat_id = seat
        await _confirm(db, order, event, payment_id, now)
        await db.commit()
        return "late_confirmed", [_notify_confirmed(order_id, late=True)]

    await db.commit()
    return f"unhandled:{order.status}", []


def _payment_problem(order: SpecialEventOrder, payment: dict) -> Optional[str]:
    """Reference, amount and currency must match the order before anything is confirmed."""
    if str(payment.get("external_reference") or "") != (order.external_reference or ""):
        return "La referencia del pago no coincide con la compra."
    currency = str(payment.get("currency_id") or "").upper()
    if currency and currency != (order.currency or "").upper():
        return f"Moneda distinta a la esperada ({currency} en lugar de {order.currency})."
    try:
        paid = Decimal(str(payment.get("transaction_amount"))).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return "El pago no indica un importe válido."
    if paid != Decimal(order.amount).quantize(Decimal("0.01")):
        return f"El importe pagado ({paid}) no coincide con el precio ({order.amount})."
    return None


async def _confirm(
    db: AsyncSession, order: SpecialEventOrder, event: SpecialEvent, payment_id: str, now: datetime
) -> None:
    from app.crud.memberships import payments as payments_crud

    order.status = ORDER_PAID
    order.paid_at = now
    order.mp_payment_id = payment_id
    order.payment_method = "mercadopago"
    order.review_reason = None
    payment = await payments_crud.create_payment(
        db,
        person_id=order.person_id,
        amount=Decimal(order.amount),
        method="mercadopago",
        provider="mercadopago",
        provider_payment_id=payment_id,
        external_reference=order.external_reference,
        comment=f"Evento especial «{event.name}» · {order.participant_name}",
        recorded_by=None,
        commit=False,
    )
    order.payment_id = payment.id
    await db.flush()


async def _begin_refund(
    db: AsyncSession, order: SpecialEventOrder, payment_id: str, reason: str
) -> Tuple[str, List[FollowUp]]:
    order.mp_payment_id = payment_id
    order.status = ORDER_REFUND_PENDING
    order.review_reason = reason
    order_id = order.id
    await db.commit()
    return "refund", [_refund_order_payment(order_id, payment_id, reason, notify=True)]


def _refund_order_payment(order_id: int, payment_id: str, reason: str, *, notify: bool) -> FollowUp:
    async def run(db: AsyncSession) -> None:
        await execute_refund(db, order_id, payment_id, reason, notify_customer=notify)

    return run


async def execute_refund(
    db: AsyncSession, order_id: int, payment_id: str, reason: str, *, notify_customer: bool
) -> bool:
    """Refund ``payment_id`` at Mercado Pago (idempotent key) and record the outcome."""
    refunded = False
    try:
        await mercadopago_service.refund_payment(
            payment_id, idempotency_key=f"event-refund-{payment_id}"
        )
        refunded = True
    except Exception:  # noqa: BLE001
        logger.exception("Mercado Pago refund failed for payment %s (order %s)", payment_id, order_id)

    order = await orders_crud.get_order(db, order_id, for_update=True)
    now = utcnow()
    if refunded:
        order.status = ORDER_REFUNDED
        order.refunded_at = now
        order.refund_reference = f"event-refund-{payment_id}"
        await _mark_payment_refunded(db, order)
    else:
        order.status = ORDER_NEEDS_REVIEW
        order.review_reason = f"{reason}. El reembolso automático falló: reembólsalo manualmente."
        _reconcile(db, "special_event_refund_failed", payment_id, order.external_reference, detail=reason)
    order.updated_at = now
    amount = order.amount
    currency = order.currency
    conversation_id = order.conversation_id
    await db.commit()

    if notify_customer and conversation_id:
        if refunded:
            text = (
                f"Lo sentimos 🙏 {reason}. Te reembolsamos {money_label(amount, currency)} "
                "automáticamente; puede tardar algunos días en reflejarse según tu banco."
            )
        else:
            text = (
                f"Lo sentimos 🙏 {reason}. Un asesor gestionará tu reembolso a la brevedad."
            )
        await _send(db, conversation_id, text)
    return refunded


def _refund_extra(order_id: int, payment_id: str) -> FollowUp:
    async def run(db: AsyncSession) -> None:
        ok = False
        try:
            await mercadopago_service.refund_payment(
                payment_id, idempotency_key=f"event-refund-{payment_id}"
            )
            ok = True
        except Exception:  # noqa: BLE001
            logger.exception("Refund of extra payment %s failed", payment_id)
        order = await orders_crud.get_order(db, order_id)
        _reconcile(
            db,
            "special_event_extra_payment_refunded" if ok else "special_event_refund_failed",
            payment_id,
            order.external_reference if order else None,
            detail={"order_id": order_id},
            processed=ok,
        )
        await db.commit()
        if order is not None and order.conversation_id:
            text = (
                "Detectamos un pago duplicado para tu entrada y lo reembolsamos automáticamente. "
                "Tu lugar sigue confirmado. 🙌"
                if ok
                else "Detectamos un pago duplicado para tu entrada. Un asesor gestionará el "
                "reembolso; tu lugar sigue confirmado."
            )
            await _send(db, order.conversation_id, text)

    return run


async def _mark_payment_refunded(db: AsyncSession, order: SpecialEventOrder) -> None:
    if order.payment_id:
        payment = await db.get(Payment, order.payment_id)
        if payment is not None:
            payment.status = "REFUNDED"


# ---------------------------------------------------------------------------------------------
# Staff actions
# ---------------------------------------------------------------------------------------------
async def refund_order(
    db: AsyncSession, order_id: int, *, actor_account_id: Optional[int], reason: str
) -> SpecialEventOrder:
    """Refund a paid ticket on purpose (event canceled, client cannot attend...).

    Mercado Pago payments are refunded through the API; front-desk payments are marked refunded
    once staff confirm they returned the money. No message is sent to the client.
    """
    reason = (reason or "").strip() or "Reembolso solicitado por el personal"
    order = await orders_crud.get_order(db, order_id)
    if order is None:
        raise SpecialEventError("No encontré esa compra.")
    await lock_special_event(db, order.event_id)
    order = await orders_crud.get_order(db, order_id, for_update=True)
    if order.status not in (ORDER_PAID, ORDER_REFUND_PENDING, ORDER_NEEDS_REVIEW):
        raise SpecialEventError("Solo se pueden reembolsar entradas pagadas.")
    now = utcnow()
    order.updated_by = actor_account_id
    order.updated_at = now
    if order.mp_payment_id and order.payment_method in (None, "mercadopago"):
        payment_id = order.mp_payment_id
        order.status = ORDER_REFUND_PENDING
        order.review_reason = reason
        await db.commit()
        await execute_refund(db, order_id, payment_id, reason, notify_customer=False)
        return await orders_crud.get_order(db, order_id)

    order.status = ORDER_REFUNDED
    order.refunded_at = now
    order.review_reason = reason
    await _mark_payment_refunded(db, order)
    await db.commit()
    return order


async def expire_payment_links(preference_ids: List[str]) -> None:
    """Best effort: close links of holds voided before payment (natural expiry is set anyway)."""
    for preference_id in preference_ids:
        try:
            await mercadopago_service.expire_preference(preference_id)
        except Exception:  # noqa: BLE001
            logger.warning("Could not expire Mercado Pago preference %s", preference_id)


# ---------------------------------------------------------------------------------------------
# Periodic sweep
# ---------------------------------------------------------------------------------------------
async def sweep_holds(limit: int = 50) -> Dict[str, int]:
    """Expire lapsed holds, but first ask Mercado Pago whether they were actually paid.

    A webhook can be lost (deploy, outage). Checking the payment search before freeing a seat
    turns that into a normal confirmation instead of a late payment. Also closes past events.
    """
    stats = {"reconciled": 0, "expired": 0, "completed_events": 0, "errors": 0}
    now = utcnow()
    async with async_session_factory() as db:
        due = (
            await db.execute(
                select(
                    SpecialEventOrder.id,
                    SpecialEventOrder.event_id,
                    SpecialEventOrder.external_reference,
                    SpecialEventOrder.mp_preference_id,
                    SpecialEventOrder.hold_expires_at,
                )
                .where(
                    SpecialEventOrder.status == ORDER_AWAITING_PAYMENT,
                    SpecialEventOrder.hold_expires_at <= now,
                )
                .order_by(SpecialEventOrder.hold_expires_at)
                .limit(limit)
            )
        ).all()

    for order_id, event_id, reference, preference_id, expires in due:
        if mercadopago_config.is_configured() and reference and preference_id:
            try:
                found = await mercadopago_service.search_payments(reference)
            except Exception:  # noqa: BLE001
                stats["errors"] += 1
                logger.warning("Payment search failed for order %s", order_id)
                # Keep the hold a little longer rather than free a seat that may be paid.
                if expires and expires > now - timedelta(minutes=15):
                    continue
                found = []
            approved = next(
                (p for p in found if (p.get("status") or "").lower() == "approved"), None
            )
            if approved is not None:
                try:
                    await process_payment_notification(str(approved.get("id")), approved)
                    stats["reconciled"] += 1
                except Exception:  # noqa: BLE001
                    stats["errors"] += 1
                    logger.exception("Reconciling order %s failed", order_id)
                continue
        async with async_session_factory() as db:
            await lock_special_event(db, event_id)
            result = await db.execute(
                update(SpecialEventOrder)
                .where(
                    SpecialEventOrder.id == order_id,
                    SpecialEventOrder.status == ORDER_AWAITING_PAYMENT,
                    SpecialEventOrder.hold_expires_at <= utcnow(),
                )
                .values(status=ORDER_EXPIRED, updated_at=utcnow())
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            stats["expired"] += int(result.rowcount or 0)

    async with async_session_factory() as db:
        stats["completed_events"] = await events_crud.complete_past_events(db)
        await db.commit()
    return stats


# ---------------------------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------------------------
async def confirmation_text(db: AsyncSession, order: SpecialEventOrder, event: SpecialEvent, *, late: bool) -> str:
    venue_address = event.address
    if not venue_address:
        from app.models import Venue

        venue = await db.get(Venue, event.venue_id)
        venue_address = venue.address if venue else None
    seat_label = None
    if order.seat_id:
        seat = await db.get(Seat, order.seat_id)
        seat_label = seat.label if seat else None
    lines = []
    if late:
        lines.append("Tu pago llegó después de que venció el apartado, pero aún había lugar. 🙌")
    lines.append(f"✅ ¡Pago acreditado! Tu lugar para «{event.name}» está confirmado.")
    lines.append(f"📅 {local_schedule_label(event.start_at, event.end_at, event.timezone)}")
    lines.append(f"👤 Participante: {order.participant_name}")
    if seat_label:
        lines.append(f"🚲 {seat_label}")
    if venue_address:
        lines.append(f"📍 {venue_address}")
    lines.append(f"💳 {money_label(order.amount, order.currency)} pagados con Mercado Pago")
    lines.append("Guarda este mensaje como comprobante. ¡Te esperamos!")
    return "\n".join(lines)


def _notify_confirmed(order_id: int, *, late: bool) -> FollowUp:
    async def run(db: AsyncSession) -> None:
        order = await orders_crud.get_order(db, order_id)
        if order is None or order.status != ORDER_PAID or not order.conversation_id:
            return
        event = await events_crud.get_event(db, order.event_id)
        text = await confirmation_text(db, order, event, late=late)
        await _send(db, order.conversation_id, text)

    return run


def _notify_review(order_id: int) -> FollowUp:
    async def run(db: AsyncSession) -> None:
        order = await orders_crud.get_order(db, order_id)
        if order is None or not order.conversation_id:
            return
        await _send(
            db,
            order.conversation_id,
            "Recibimos tu pago, pero necesitamos revisarlo antes de confirmar tu lugar. Un "
            "asesor te contactará en breve. 🙏",
        )

    return run


async def _send(db: AsyncSession, conversation_id: int, text: str) -> None:
    from app.services.chatbot.reply_service import send_and_persist_reply
    from app.services.whatsapp_outbound import KIND_PAYMENT_CONFIRM

    conversation = await whatsappCrud.get_conversation(db, conversation_id)
    contact = getattr(conversation, "contact", None) if conversation else None
    if contact is None:
        logger.warning("Special event: no contact for conversation %s; message not sent", conversation_id)
        return
    await send_and_persist_reply(
        db,
        conversation_id=conversation_id,
        contact_id=contact.id,
        to_wa_id=contact.wa_id,
        text=text,
        kind=KIND_PAYMENT_CONFIRM,
    )


def _reconcile(
    db: AsyncSession,
    event_type: str,
    payment_id: Optional[str],
    reference: Optional[str],
    *,
    detail=None,
    processed: bool = False,
) -> None:
    """Durable reconciliation record (``webhook_logs``, unprocessed) for staff follow-up."""
    from app.models.whatsappModel import WebhookLog

    db.add(
        WebhookLog(
            event_type=event_type[:50],
            x_request_id=str(payment_id) if payment_id else None,
            payload={
                "payment_id": str(payment_id) if payment_id else None,
                "external_reference": reference,
                "detail": detail,
            },
            processed=1 if processed else 0,
        )
    )
