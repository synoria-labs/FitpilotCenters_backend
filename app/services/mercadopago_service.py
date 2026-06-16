"""Client for the MercadoPago Checkout Pro API (create payment links, fetch payments, verify webhooks).

Uses httpx (no SDK). Server-side calls authenticate with the access token; the webhook is verified
with the webhook secret (HMAC-SHA256 over MercadoPago's documented manifest).
"""
import hashlib
import hmac
import logging
from typing import Any, Dict, Optional

import httpx

from app.core.mercadopago_config import mercadopago_config as cfg

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(20.0)


class MercadoPagoError(Exception):
    """Raised when a MercadoPago API call fails."""


def _auth_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {cfg.ACCESS_TOKEN}"}


async def create_preference(
    *,
    amount,
    description: str,
    external_reference: str,
    payer_name: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """Create a Checkout Pro preference and return its id + payment link (init_point).

    With a TEST access token the sandbox link is returned. ``external_reference`` is echoed back
    on the payment so the webhook can match it to the pending action.
    """
    if not cfg.is_configured():
        raise MercadoPagoError("MercadoPago no está configurado (falta MP_ACCESS_TOKEN).")

    url = f"{cfg.API_BASE}/checkout/preferences"
    body: Dict[str, Any] = {
        "items": [
            {
                "title": (description or "Pago FitPilot")[:250],
                "quantity": 1,
                "unit_price": float(amount),
                "currency_id": cfg.CURRENCY,
            }
        ],
        "external_reference": external_reference,
        "metadata": {"external_reference": external_reference},
    }
    if cfg.NOTIFICATION_URL:
        body["notification_url"] = cfg.NOTIFICATION_URL
    if payer_name:
        body["payer"] = {"name": payer_name}

    headers = {**_auth_headers(), "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=body, headers=headers)

    if resp.status_code >= 400:
        logger.warning("MercadoPago preference failed (%s): %s", resp.status_code, resp.text[:300])
        raise MercadoPagoError(f"No se pudo crear el link de pago (HTTP {resp.status_code}).")

    data = resp.json()
    init_point = (data.get("sandbox_init_point") if cfg.is_test() else data.get("init_point"))
    init_point = init_point or data.get("init_point") or data.get("sandbox_init_point")
    if not init_point:
        raise MercadoPagoError(f"Respuesta inesperada de MercadoPago: {data}")
    return {"preference_id": data.get("id"), "init_point": init_point}


async def get_payment(payment_id) -> Dict[str, Any]:
    """Fetch a payment by id (the webhook gives only the id; the status comes from here)."""
    if not cfg.is_configured():
        raise MercadoPagoError("MercadoPago no está configurado (falta MP_ACCESS_TOKEN).")
    url = f"{cfg.API_BASE}/v1/payments/{payment_id}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, headers=_auth_headers())
    if resp.status_code >= 400:
        logger.warning("MercadoPago get_payment failed (%s): %s", resp.status_code, resp.text[:300])
        raise MercadoPagoError(f"No se pudo consultar el pago (HTTP {resp.status_code}).")
    return resp.json()


async def refund_payment(
    payment_id, amount=None, *, idempotency_key: Optional[str] = None
) -> Dict[str, Any]:
    """Refund a payment (full if ``amount`` is None, otherwise partial).

    Used when a purchase is paid but the booking can no longer be honored (e.g. the class filled
    up between the payment link being sent and the webhook arriving). ``idempotency_key`` is sent
    as ``X-Idempotency-Key`` so retried webhooks never double-refund.
    """
    if not cfg.is_configured():
        raise MercadoPagoError("MercadoPago no está configurado (falta MP_ACCESS_TOKEN).")
    url = f"{cfg.API_BASE}/v1/payments/{payment_id}/refunds"
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    if idempotency_key:
        headers["X-Idempotency-Key"] = idempotency_key
    body: Dict[str, Any] = {} if amount is None else {"amount": float(amount)}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=body, headers=headers)
    if resp.status_code >= 400:
        logger.warning("MercadoPago refund failed (%s): %s", resp.status_code, resp.text[:300])
        raise MercadoPagoError(f"No se pudo reembolsar el pago (HTTP {resp.status_code}).")
    return resp.json()


def verify_webhook_signature(
    *, x_signature: Optional[str], x_request_id: Optional[str], data_id: Optional[str]
) -> bool:
    """Validate MercadoPago's ``x-signature`` header (HMAC-SHA256).

    Header format: ``ts=<ts>,v1=<hash>``. Manifest signed with the webhook secret:
    ``id:<data.id>;request-id:<x-request-id>;ts:<ts>;`` (data.id lowercased).
    """
    if not cfg.WEBHOOK_SECRET:
        logger.error("MP_WEBHOOK_SECRET not set; rejecting MercadoPago webhook")
        return False
    if not x_signature:
        return False

    ts = None
    v1 = None
    for part in x_signature.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key, value = key.strip(), value.strip()
        if key == "ts":
            ts = value
        elif key == "v1":
            v1 = value
    if not ts or not v1:
        return False

    manifest = f"id:{str(data_id or '').lower()};request-id:{x_request_id or ''};ts:{ts};"
    expected = hmac.new(
        cfg.WEBHOOK_SECRET.encode("utf-8"), manifest.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, v1)
