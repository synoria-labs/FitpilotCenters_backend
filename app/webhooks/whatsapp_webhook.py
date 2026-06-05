"""Inbound WhatsApp Cloud API webhook.

GET  /webhook/whatsapp  -> verification handshake (hub.challenge).
POST /webhook/whatsapp  -> events: signature check, raw log, ingest.

FitPilot owns this webhook (it replaced the external bot). Authentication is by the
Meta app-secret signature, not by user JWT.
"""
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, JSONResponse

from app.core.whatsapp_config import whatsapp_config
from app.db.postgresql import async_session_factory
from app.models import WebhookLog
from app.services import whatsapp_ingest_service as ingest

logger = logging.getLogger("whatsapp.webhook")

router = APIRouter(tags=["whatsapp-webhook"])


def _verify_signature(raw_body: bytes, signature_header: str) -> bool:
    """Validate X-Hub-Signature-256 against the app secret (constant time)."""
    if not whatsapp_config.APP_SECRET:
        # No secret configured: refuse to accept unverifiable requests.
        logger.error("WHATSAPP_APP_SECRET not set; rejecting webhook POST")
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        whatsapp_config.APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    received = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, received)


async def _log_webhook(event_type: str, x_request_id: str, payload: dict, processed: int) -> None:
    try:
        async with async_session_factory() as db:
            db.add(
                WebhookLog(
                    event_type=event_type[:50],
                    x_request_id=(x_request_id or None),
                    payload=payload,
                    processed=processed,
                )
            )
            await db.commit()
    except Exception as e:  # noqa: BLE001 - logging must never break the response
        logger.warning("Failed to write webhook_logs: %s", e)


@router.get("/webhook/whatsapp")
async def verify_webhook(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token and token == whatsapp_config.WEBHOOK_VERIFY_TOKEN:
        logger.info("WhatsApp webhook verified")
        return PlainTextResponse(challenge or "")
    logger.warning("WhatsApp webhook verification failed")
    return PlainTextResponse("Forbidden", status_code=403)


@router.post("/webhook/whatsapp")
async def receive_webhook(request: Request):
    raw = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")

    if not _verify_signature(raw, signature):
        logger.warning("Rejected webhook with invalid signature")
        return JSONResponse({"status": "invalid signature"}, status_code=403)

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return JSONResponse({"status": "bad payload"}, status_code=400)

    x_request_id = request.headers.get("x-request-id", "")
    processed = 1
    try:
        await ingest.process(payload)
    except Exception as e:  # noqa: BLE001 - always ack to avoid retry storms
        processed = 0
        logger.exception("WhatsApp ingest failed: %s", e)

    await _log_webhook(event_type=payload.get("object", "whatsapp"),
                       x_request_id=x_request_id, payload=payload, processed=processed)

    # Always acknowledge so Meta does not retry indefinitely (ingest is idempotent).
    return JSONResponse({"status": "ok"})
