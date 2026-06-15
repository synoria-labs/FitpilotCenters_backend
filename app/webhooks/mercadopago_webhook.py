"""Inbound MercadoPago payment webhook.

When a Checkout Pro payment is approved, MercadoPago notifies this endpoint. We verify the
signature, fetch the payment, match it to the pending chatbot purchase by ``external_reference``,
and (idempotently) execute the purchase + notify the customer on WhatsApp. Always returns 200 so
MercadoPago does not retry indefinitely.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.crud import chatbotPendingCrud
from app.crud import whatsappCrud
from app.db.postgresql import async_session_factory
from app.models.chatbotModel import PENDING_STATUS_AWAITING_PAYMENT, PENDING_STATUS_CONFIRMED
from app.services import mercadopago_service

logger = logging.getLogger(__name__)

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
        pending = await chatbotPendingCrud.get_by_external_reference(db, external_reference)
        if pending is None:
            logger.warning("MercadoPago: no pending action for ref %s", external_reference)
            return
        if pending.status != PENDING_STATUS_AWAITING_PAYMENT:
            # Idempotent: already confirmed/canceled/expired.
            logger.info("MercadoPago: pending %s already %s; skipping", pending.id, pending.status)
            return
        try:
            result = await _execute_pending(
                db,
                pending,
                payment_method="mercadopago",
                payment_provider="mercadopago",
                provider_payment_id=str(payment_id),
                external_reference=external_reference,
            )
        except Exception:  # noqa: BLE001
            await db.rollback()
            logger.exception("MercadoPago: executing pending %s failed", pending.id)
            await _notify(
                db, pending.conversation_id,
                "Recibimos tu pago, pero hubo un problema al confirmar tu lugar. "
                "Un asesor te contactará en breve.",
            )
            return
        await chatbotPendingCrud.mark_status(db, pending.id, PENDING_STATUS_CONFIRMED, commit=True)
        await _notify(db, pending.conversation_id, f"¡Pago acreditado! {result}")


async def _notify(db, conversation_id: int, text: str) -> None:
    from app.services.chatbot.reply_service import send_and_persist_reply

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
        )
    except Exception:  # noqa: BLE001
        logger.exception("MercadoPago: failed to notify customer (conversation %s)", conversation_id)
