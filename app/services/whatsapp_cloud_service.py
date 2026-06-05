"""Client for the Meta WhatsApp Cloud API (Graph API).

Handles outbound text sends and media retrieval/download. Inbound messages arrive
via the webhook (see app/webhooks/whatsapp_webhook.py), not here.
"""
import logging
from typing import Optional, Dict, Any

import httpx

from app.core.whatsapp_config import whatsapp_config

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(30.0)


class WhatsAppError(Exception):
    """Raised when a Cloud API call fails."""

    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.code = code


def _auth_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {whatsapp_config.ACCESS_TOKEN}"}


def _parse_api_error(resp: httpx.Response) -> WhatsAppError:
    try:
        err = resp.json().get("error", {})
        msg = err.get("message") or resp.text
        code = err.get("code")
    except Exception:  # noqa: BLE001
        msg = resp.text or f"HTTP {resp.status_code}"
        code = None
    return WhatsAppError(msg, code)


async def send_text(to: str, text: str) -> Dict[str, Any]:
    """Send a free-form text message. Returns {"wa_message_id": ...}.

    Raises WhatsAppError on failure (e.g. code 131047 outside the 24h window).
    """
    if not whatsapp_config.is_send_configured():
        raise WhatsAppError("WhatsApp Cloud API is not configured (missing phone id/token).")

    url = whatsapp_config.graph_url(f"{whatsapp_config.PHONE_NUMBER_ID}/messages")
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    headers = {**_auth_headers(), "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=payload, headers=headers)

    if resp.status_code >= 400:
        error = _parse_api_error(resp)
        logger.warning("WhatsApp send failed (%s): %s", error.code, error.message)
        raise error

    data = resp.json()
    try:
        wa_message_id = data["messages"][0]["id"]
    except (KeyError, IndexError):
        raise WhatsAppError(f"Unexpected send response: {data}")
    return {"wa_message_id": wa_message_id}


async def get_media_metadata(media_id: str) -> Dict[str, Any]:
    """Resolve a media id to its temporary download URL and metadata."""
    url = whatsapp_config.graph_url(media_id)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, headers=_auth_headers())
    if resp.status_code >= 400:
        raise _parse_api_error(resp)
    return resp.json()  # {url, mime_type, sha256, file_size, id, messaging_product}


async def download_media_bytes(media_url: str) -> bytes:
    """Download the binary content of a media URL (requires the bearer token)."""
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(media_url, headers=_auth_headers())
    if resp.status_code >= 400:
        raise _parse_api_error(resp)
    return resp.content
