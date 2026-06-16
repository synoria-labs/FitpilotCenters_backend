"""Inbound MercadoPago payment webhook.

When a Checkout Pro payment is approved, MercadoPago notifies this endpoint. We verify the
signature, fetch the payment, match it to the pending chatbot purchase by ``external_reference``,
and (idempotently) execute the purchase + notify the customer on WhatsApp. Always returns 200 so
MercadoPago does not retry indefinitely.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.crud import chatbotPendingCrud
from app.crud import whatsappCrud
from app.db.postgresql import async_session_factory
from app.models.chatbotModel import (
    ChatbotPendingAction,
    PENDING_STATUS_AWAITING_PAYMENT,
    PENDING_STATUS_CANCELED,
    PENDING_STATUS_CONFIRMED,
    PENDING_STATUS_PROCESSING,
)
from app.services import mercadopago_service

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

router = APIRouter(tags=["mercadopago-webhook"])


def _extract_payment_id(request: Request, body: dict) -> Optional[str]:
    qp = request.query_params
    pid = qp.get("data.id") or qp.get("id")
    if pid:
        return str(pid)
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict) and data.get("id"):
        return str(data["id"])
    res = body.get("resource") if isinstance(body, dict) else None
    if isinstance(res, str) and res.isdigit():
        return res
    return None


def _topic(request: Request, body: dict) -> str:
    return (
        request.query_params.get("type")
        or request.query_params.get("topic")
        or (body.get("type") if isinstance(body, dict) else "")
        or ""
    )


@router.post("/webhook/mercadopago")
async def receive_mercadopago(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}

    payment_id = _extract_payment_id(request, body)
    topic = _topic(request, body)

    if not mercadopago_service.verify_webhook_signature(
        x_signature=request.headers.get("x-signature"),
        x_request_id=request.headers.get("x-request-id"),
        data_id=payment_id,
    ):
        logger.warning("MercadoPago webhook signature invalid; ignoring (id=%s)", payment_id)
        return JSONResponse({"status": "ignored"})

    if topic and "payment" not in topic.lower():
        return JSONResponse({"status": "ok"})
    if not payment_id:
        return JSONResponse({"status": "ok"})

    try:
        await _process_approved_payment(payment_id)
    except Exception:  # noqa: BLE001
        logger.exception("MercadoPago webhook processing failed (id=%s)", payment_id)

    return JSONResponse({"status": "ok"})


async def _process_approved_payment(payment_id: str) -> None:
    payment = await mercadopago_service.get_payment(payment_id)
    status = (payment.get("status") or "").lower()
    external_reference = payment.get("external_reference")
    if status != "approved" or not external_reference:
        logger.info(
            "MercadoPago payment %s status=%s ref=%s (no action)",
            payment_id, status, external_reference,
        )
        return

    # Lazy import keeps app startup free of the chatbot/langchain deps.
    from app.services.chatbot.tools import _execute_pending

    async with async_session_factory() as db:
        # --- Claim the pending row atomically. MercadoPago commonly delivers the same payment
        #     notification more than once (and concurrently). We lock the row (FOR UPDATE) and flip
        #     it to 'processing' inside one transaction, so a duplicate webhook blocks, then sees
        #     'processing'/'confirmed' and skips instead of double-executing the purchase. ---
        async with db.begin():
            pending = await chatbotPendingCrud.get_by_external_reference(
                db, external_reference, for_update=True
            )
            if pending is None:
                await _record_unmatched_payment(db, payment, payment_id, external_reference)
                return
            if pending.status != PENDING_STATUS_AWAITING_PAYMENT:
                # Idempotent: already processing/confirmed/canceled/expired.
                logger.info("MercadoPago: pending %s already %s; skipping", pending.id, pending.status)
                return
            # Capture before any rollback (rolled-back ORM attributes lazy-load -> MissingGreenlet).
            pending_id = pending.id
            conversation_id = pending.conversation_id
            pending.status = PENDING_STATUS_PROCESSING
            pending.updated_at = _utcnow()
        # Claim committed -> lock released. We now own this purchase exclusively.

        pending = await db.get(ChatbotPendingAction, pending_id)
        try:
            result = await _execute_pending(
                db,
                pending,
                payment_method="mercadopago",
                payment_provider="mercadopago",
                provider_payment_id=str(payment_id),
                external_reference=external_reference,
            )
        except ValueError as exc:
            # Business/availability failure (e.g. the class filled up between the link being sent
            # and the payment acreditando). The money WAS captured at MercadoPago -> refund
            # automatically (unless a payment already committed -> _refund_and_notify guards that).
            await db.rollback()
            await _refund_and_notify(
                db, pending_id, conversation_id, str(payment_id), external_reference, str(exc)
            )
            return
        except Exception:  # noqa: BLE001 - transient/unexpected -> release the claim so a retry runs
            await db.rollback()
            logger.exception("MercadoPago: executing pending %s failed (transient)", pending_id)
            await chatbotPendingCrud.mark_status(
                db, pending_id, PENDING_STATUS_AWAITING_PAYMENT, commit=True
            )
            await _notify(
                db, conversation_id,
                "Recibimos tu pago, pero hubo un problema al confirmar tu lugar. "
                "Un asesor te contactará en breve.",
            )
            return
        await chatbotPendingCrud.mark_status(db, pending_id, PENDING_STATUS_CONFIRMED, commit=True)
        await _notify(db, conversation_id, f"¡Pago acreditado! {result}")


async def _refund_and_notify(
    db, pending_id: int, conversation_id: int, payment_id: str,
    external_reference: str, reason: str,
) -> None:
    """A captured payment whose booking failed: refund the money and tell the customer.

    SAFETY GUARD: some flows (the fixed-slot *package* path) commit the enrollment mid-transaction
    — ``generate_sessions_from_template`` commits — *before* the availability assertion raises, so
    the prior ``db.rollback()`` cannot undo the person/subscription/payment. If a Payment row for
    this purchase actually committed, we must NOT refund (that would hand out a paid membership for
    free); we mark it confirmed and flag it for staff reconciliation instead. The clean day-pass
    path commits nothing on failure, so no payment exists and the refund proceeds.
    """
    if await _committed_payment_exists(db, external_reference):
        logger.error(
            "MercadoPago: '%s' after a COMMITTED payment (pending %s, ref %s). NOT refunding; "
            "confirming + flagging for reconciliation.", reason, pending_id, external_reference,
        )
        _reconcile(db, "mercadopago_partial_booking", payment_id, external_reference, detail=reason)
        await chatbotPendingCrud.mark_status(db, pending_id, PENDING_STATUS_CONFIRMED, commit=True)
        await _notify(
            db, conversation_id,
            "¡Pago acreditado! Tu inscripción quedó registrada. Un asesor confirmará los detalles "
            "de tu horario en breve. 🙏",
        )
        return

    logger.warning(
        "MercadoPago: booking failed after payment (pending %s): %s -> refunding payment %s",
        pending_id, reason, payment_id,
    )
    refunded = False
    try:
        await mercadopago_service.refund_payment(
            payment_id, idempotency_key=f"refund-{payment_id}"
        )
        refunded = True
    except Exception:  # noqa: BLE001 - refund API failure: leave a durable reconciliation record
        logger.exception("MercadoPago: refund failed for payment %s", payment_id)
        _reconcile(db, "mercadopago_refund_failed", payment_id, external_reference, detail=reason)

    await chatbotPendingCrud.mark_status(db, pending_id, PENDING_STATUS_CANCELED, commit=True)

    if refunded:
        msg = (
            "Lo sentimos 🙏 no pudimos confirmar tu lugar para esa clase (es posible que se haya "
            "llenado). Te reembolsamos el importe automáticamente; puede tardar unos minutos en "
            "reflejarse. ¿Te ayudo a reservar otro horario?"
        )
    else:
        msg = (
            "Lo sentimos 🙏 no pudimos confirmar tu lugar para esa clase. Un asesor gestionará tu "
            "reembolso a la brevedad."
        )
    await _notify(db, conversation_id, msg)


async def _committed_payment_exists(db, external_reference: str) -> bool:
    """True if a Payment row for this purchase already committed (so refunding would be wrong)."""
    if not external_reference:
        return False
    from app.models import Payment

    stmt = select(Payment.id).where(
        Payment.external_reference == external_reference
    ).limit(1)
    return (await db.execute(stmt)).first() is not None


def _reconcile(
    db, event_type: str, payment_id: str, external_reference: str, *, detail=None
) -> None:
    """Queue a ``webhook_logs`` reconciliation row (``processed=0``). Caller commits afterwards."""
    from app.models.whatsappModel import WebhookLog

    db.add(
        WebhookLog(
            event_type=event_type,
            x_request_id=str(payment_id),
            payload={
                "payment_id": str(payment_id),
                "external_reference": external_reference,
                "detail": detail,
            },
            processed=0,
        )
    )


async def _record_unmatched_payment(
    db, payment: dict, payment_id: str, external_reference: str
) -> None:
    """Persist an approved payment that has no matching pending action, for manual reconciliation.

    This happens if the pending row expired or was superseded before the payment acreditó. Without
    this, the customer has paid but there is no booking, no payment record and no alert. We log at
    ERROR and write a ``webhook_logs`` row (``processed=0``) so staff can reconcile/refund.
    """
    from app.models.whatsappModel import WebhookLog

    logger.error(
        "MercadoPago APPROVED payment %s has NO matching pending (ref=%s, amount=%s). "
        "Recorded for reconciliation.",
        payment_id, external_reference, payment.get("transaction_amount"),
    )
    db.add(
        WebhookLog(
            event_type="mercadopago_unmatched_payment",
            x_request_id=str(payment_id),
            payload={
                "payment_id": str(payment_id),
                "external_reference": external_reference,
                "status": payment.get("status"),
                "amount": payment.get("transaction_amount"),
                "payer": payment.get("payer"),
            },
            processed=0,
        )
    )


async def _notify(db, conversation_id: int, text: str) -> None:
    from app.services.chatbot.reply_service import send_and_persist_reply
    from app.services.whatsapp_outbound import KIND_PAYMENT_CONFIRM

    conversation = await whatsappCrud.get_conversation(db, conversation_id)
    contact = getattr(conversation, "contact", None) if conversation else None
    if contact is None:
        logger.warning("MercadoPago: cannot notify, no contact for conversation %s", conversation_id)
        return
    try:
        await send_and_persist_reply(
            db,
            conversation_id=conversation_id,
            contact_id=contact.id,
            to_wa_id=contact.wa_id,
            text=text,
            kind=KIND_PAYMENT_CONFIRM,
        )
    except Exception:  # noqa: BLE001
        logger.exception("MercadoPago: failed to notify customer (conversation %s)", conversation_id)
