"""Client for the Meta WhatsApp Cloud API (Graph API).

Handles outbound text sends and media retrieval/download. Inbound messages arrive
via the webhook (see app/webhooks/whatsapp_webhook.py), not here.
"""
import logging
import re
from typing import Optional, Dict, Any, List

import httpx

from app.core.whatsapp_config import whatsapp_config

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(30.0)
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\d+)\s*\}\}")


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


def _placeholder_count(text: Optional[str]) -> int:
    indices = [int(match) for match in _PLACEHOLDER_RE.findall(text or "")]
    return max(indices) if indices else 0


def _component_type(component: Dict[str, Any]) -> str:
    return str(component.get("type") or "").upper()


def _body_example_values(component: Dict[str, Any]) -> List[str]:
    example = component.get("example")
    if not isinstance(example, dict):
        return []
    rows = example.get("body_text")
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], list):
        return []
    return [str(value) for value in rows[0]]


def _template_send_components(
    template_components: Optional[List[Dict[str, Any]]],
    body_params: Optional[List[str]],
    header_media_url: Optional[str] = None,
    header_media_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Build Cloud API send components from a stored Meta template definition.

    Meta validates the send payload against the approved template shape. For example,
    a template with an IMAGE header must receive a header image parameter, and a BODY
    with ``{{1}}``/``{{2}}`` must receive exactly two body text parameters.
    """
    components: List[Dict[str, Any]] = []
    stored_components = [
        component for component in template_components or [] if isinstance(component, dict)
    ]

    for component in stored_components:
        ctype = _component_type(component)
        if ctype != "HEADER":
            continue

        header_format = str(component.get("format") or "").upper()
        if header_format in {"IMAGE", "VIDEO", "DOCUMENT"}:
            media_type = header_format.lower()
            media_value = (header_media_id or "").strip()
            media_link = (header_media_url or "").strip()
            if media_value:
                media_payload = {"id": media_value}
            elif media_link:
                media_payload = {"link": media_link}
            else:
                raise WhatsAppError(
                    "La plantilla requiere un encabezado de media. "
                    "Agrega una URL o ID de media para enviarla."
                )
            components.append(
                {
                    "type": "header",
                    "parameters": [{"type": media_type, media_type: media_payload}],
                }
            )
        elif header_format == "TEXT":
            count = _placeholder_count(str(component.get("text") or ""))
            if count:
                raise WhatsAppError(
                    "La plantilla tiene variables en el encabezado. "
                    "Este flujo solo soporta variables en el cuerpo por ahora."
                )

    for component in stored_components:
        if _component_type(component) != "BODY":
            continue

        count = _placeholder_count(str(component.get("text") or ""))
        if not count:
            break

        provided = [str(value).strip() for value in body_params or []]
        examples = _body_example_values(component)
        values: List[str] = []
        for index in range(count):
            value = provided[index] if index < len(provided) else ""
            if not value and index < len(examples):
                value = str(examples[index]).strip()
            if not value:
                raise WhatsAppError(
                    f"La plantilla requiere {count} parámetro(s) en el cuerpo. "
                    "Completa los valores antes de enviar."
                )
            values.append(value)

        components.append(
            {
                "type": "body",
                "parameters": [{"type": "text", "text": value} for value in values],
            }
        )
        break

    for component in stored_components:
        if _component_type(component) != "BUTTONS":
            continue
        buttons = component.get("buttons") or []
        if any(_placeholder_count(str(button.get("url") or "")) for button in buttons if isinstance(button, dict)):
            raise WhatsAppError(
                "La plantilla tiene botones con variables. "
                "Este flujo todavía no soporta parámetros de botones."
            )

    return components


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


async def send_template(
    to: str,
    template_name: str,
    language_code: str,
    body_params: Optional[List[str]] = None,
    components: Optional[List[Dict[str, Any]]] = None,
    header_media_url: Optional[str] = None,
    header_media_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Send an approved Meta template message. Returns {"wa_message_id": ...}.

    Works for any recipient (no 24h-window restriction), provided the template is
    APPROVED in Meta. ``body_params`` are the positional values for the BODY
    placeholders ({{1}}, {{2}}, ...) in order.

    Raises WhatsAppError on failure (e.g. 132000 if param count mismatches the template).
    """
    if not whatsapp_config.is_send_configured():
        raise WhatsAppError("WhatsApp Cloud API is not configured (missing phone id/token).")

    template: Dict[str, Any] = {
        "name": template_name,
        "language": {"code": language_code},
    }
    send_components = _template_send_components(
        components,
        body_params,
        header_media_url=header_media_url,
        header_media_id=header_media_id,
    )
    if send_components:
        template["components"] = send_components

    url = whatsapp_config.graph_url(f"{whatsapp_config.PHONE_NUMBER_ID}/messages")
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "template",
        "template": template,
    }
    headers = {**_auth_headers(), "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=payload, headers=headers)

    if resp.status_code >= 400:
        error = _parse_api_error(resp)
        logger.warning("WhatsApp template send failed (%s): %s", error.code, error.message)
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
