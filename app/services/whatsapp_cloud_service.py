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


def _media_header_param(
    media_type: str,
    media_url: Optional[str],
    media_id: Optional[str],
    *,
    error: str,
) -> Dict[str, Any]:
    media_value = (media_id or "").strip()
    media_link = (media_url or "").strip()
    if media_value:
        media_payload: Dict[str, Any] = {"id": media_value}
    elif media_link:
        media_payload = {"link": media_link}
    else:
        raise WhatsAppError(error)
    return {"type": "header", "parameters": [{"type": media_type, media_type: media_payload}]}


def _body_params_component(
    body_component: Dict[str, Any],
    body_params: Optional[List[str]],
    *,
    label: str,
) -> Optional[Dict[str, Any]]:
    count = _placeholder_count(str(body_component.get("text") or ""))
    if not count:
        return None
    provided = [str(value).strip() for value in body_params or []]
    examples = _body_example_values(body_component)
    values: List[str] = []
    for index in range(count):
        value = provided[index] if index < len(provided) else ""
        if not value and index < len(examples):
            value = str(examples[index]).strip()
        if not value:
            raise WhatsAppError(
                f"{label} requiere {count} parámetro(s) en el cuerpo. "
                "Completa los valores antes de enviar."
            )
        values.append(value)
    return {"type": "body", "parameters": [{"type": "text", "text": value} for value in values]}


def _button_send_component(
    buttons: List[Any], override: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Emit the runtime parameter for the single dynamic URL button (Meta allows only one)."""
    for index, button in enumerate(buttons or []):
        if not isinstance(button, dict):
            continue
        if str(button.get("type") or "").upper() != "URL":
            continue
        if not _placeholder_count(str(button.get("url") or "")):
            continue
        value = (override or "").strip()
        if not value:
            example = button.get("example")
            if isinstance(example, list) and example:
                value = str(example[0]).strip()
        if not value:
            raise WhatsAppError(
                "La plantilla tiene un botón de URL con variable; falta su valor para enviar."
            )
        return {
            "type": "button",
            "sub_type": "url",
            "index": str(index),
            "parameters": [{"type": "text", "text": value}],
        }
    return None


def _location_header_param(location: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    loc = location or {}
    latitude = str(loc.get("latitude") or "").strip()
    longitude = str(loc.get("longitude") or "").strip()
    if not latitude or not longitude:
        raise WhatsAppError(
            "La plantilla requiere una ubicación (latitud y longitud) para enviarse."
        )
    location_payload: Dict[str, Any] = {"latitude": latitude, "longitude": longitude}
    name = str(loc.get("name") or "").strip()
    address = str(loc.get("address") or "").strip()
    if name:
        location_payload["name"] = name
    if address:
        location_payload["address"] = address
    return {"type": "header", "parameters": [{"type": "location", "location": location_payload}]}


def _carousel_send_component(
    carousel_component: Dict[str, Any],
    runtime_cards: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    cards_def = [c for c in carousel_component.get("cards") or [] if isinstance(c, dict)]
    runtime = runtime_cards or []
    send_cards: List[Dict[str, Any]] = []
    for card_index, card_def in enumerate(cards_def):
        rt = runtime[card_index] if card_index < len(runtime) and isinstance(runtime[card_index], dict) else {}
        card_components: List[Dict[str, Any]] = []
        sub_components = [c for c in card_def.get("components") or [] if isinstance(c, dict)]

        for sub in sub_components:
            if _component_type(sub) != "HEADER":
                continue
            card_format = str(sub.get("format") or "").upper()
            if card_format in {"IMAGE", "VIDEO", "DOCUMENT"}:
                card_components.append(
                    _media_header_param(
                        card_format.lower(),
                        rt.get("media_url"),
                        rt.get("media_id"),
                        error=f"La tarjeta {card_index + 1} del carrusel requiere media para enviarse.",
                    )
                )
            break

        for sub in sub_components:
            if _component_type(sub) != "BODY":
                continue
            body_param = _body_params_component(
                sub, rt.get("body_params"), label=f"La tarjeta {card_index + 1}"
            )
            if body_param is not None:
                card_components.append(body_param)
            break

        for sub in sub_components:
            if _component_type(sub) != "BUTTONS":
                continue
            button_param = _button_send_component(sub.get("buttons") or [], rt.get("button_url_param"))
            if button_param is not None:
                card_components.append(button_param)
            break

        send_cards.append({"card_index": card_index, "components": card_components})

    return {"type": "carousel", "cards": send_cards}


def _template_send_components(
    template_components: Optional[List[Dict[str, Any]]],
    body_params: Optional[List[str]],
    header_media_url: Optional[str] = None,
    header_media_id: Optional[str] = None,
    *,
    header_text_param: Optional[str] = None,
    location: Optional[Dict[str, Any]] = None,
    button_url_param: Optional[str] = None,
    carousel_cards: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Build Cloud API send components from a stored Meta template definition.

    Meta validates the send payload against the approved template shape. For example,
    a template with an IMAGE header must receive a header image parameter, and a BODY
    with ``{{1}}``/``{{2}}`` must receive exactly two body text parameters.

    Static components (static text headers, static buttons) need no runtime input: they
    emit nothing here or fall back to the stored ``example`` values. Runtime values are
    supplied via ``header_text_param`` / ``location`` / ``button_url_param`` / ``carousel_cards``
    only when the approved template carries a variable Meta cannot fill from the example.
    """
    components: List[Dict[str, Any]] = []
    stored_components = [
        component for component in template_components or [] if isinstance(component, dict)
    ]

    for component in stored_components:
        if _component_type(component) != "HEADER":
            continue

        header_format = str(component.get("format") or "").upper()
        if header_format in {"IMAGE", "VIDEO", "DOCUMENT"}:
            components.append(
                _media_header_param(
                    header_format.lower(),
                    header_media_url,
                    header_media_id,
                    error="La plantilla requiere un encabezado de media. "
                    "Agrega una URL o ID de media para enviarla.",
                )
            )
        elif header_format == "TEXT":
            count = _placeholder_count(str(component.get("text") or ""))
            if count:
                value = (header_text_param or "").strip()
                if not value:
                    example = component.get("example")
                    if isinstance(example, dict):
                        values = example.get("header_text")
                        if isinstance(values, list) and values:
                            value = str(values[0]).strip()
                if not value:
                    raise WhatsAppError(
                        "La plantilla tiene un encabezado de texto con variable; "
                        "falta su valor para enviar."
                    )
                components.append(
                    {"type": "header", "parameters": [{"type": "text", "text": value}]}
                )
        elif header_format == "LOCATION":
            components.append(_location_header_param(location))
        break

    for component in stored_components:
        if _component_type(component) != "BODY":
            continue
        body_param = _body_params_component(component, body_params, label="La plantilla")
        if body_param is not None:
            components.append(body_param)
        break

    for component in stored_components:
        if _component_type(component) != "BUTTONS":
            continue
        button_param = _button_send_component(component.get("buttons") or [], button_url_param)
        if button_param is not None:
            components.append(button_param)
        break

    for component in stored_components:
        if _component_type(component) != "CAROUSEL":
            continue
        components.append(_carousel_send_component(component, carousel_cards))
        break

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


async def send_read_receipt(wa_message_id: Optional[str]) -> bool:
    """Mark an inbound message as read in WhatsApp (blue ticks). Best-effort.

    Reuses the same ``/messages`` endpoint + bearer credentials as ``send_text``. Returns
    True on success, False on any failure (missing config/id, expired 24h window, etc.) and
    never raises — a read receipt must never break the UI flow.
    """
    if not wa_message_id or not whatsapp_config.is_send_configured():
        return False

    url = whatsapp_config.graph_url(f"{whatsapp_config.PHONE_NUMBER_ID}/messages")
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": wa_message_id,
    }
    headers = {**_auth_headers(), "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            error = _parse_api_error(resp)
            logger.info("WhatsApp read receipt skipped (%s): %s", error.code, error.message)
            return False
        return True
    except Exception:  # noqa: BLE001
        logger.debug("WhatsApp read receipt failed for %s", wa_message_id, exc_info=True)
        return False


async def send_reaction(to: str, message_id: str, emoji: str) -> Dict[str, Any]:
    """React to a message with an emoji. Returns {"wa_message_id": ...}.

    ``emoji=""`` removes a previously sent reaction. ``message_id`` is the
    wa_message_id of the target message. Raises WhatsAppError on failure.
    """
    if not whatsapp_config.is_send_configured():
        raise WhatsAppError("WhatsApp Cloud API is not configured (missing phone id/token).")

    url = whatsapp_config.graph_url(f"{whatsapp_config.PHONE_NUMBER_ID}/messages")
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "reaction",
        "reaction": {"message_id": message_id, "emoji": emoji},
    }
    headers = {**_auth_headers(), "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=payload, headers=headers)

    if resp.status_code >= 400:
        error = _parse_api_error(resp)
        logger.warning("WhatsApp reaction send failed (%s): %s", error.code, error.message)
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
    header_text_param: Optional[str] = None,
    location: Optional[Dict[str, Any]] = None,
    button_url_param: Optional[str] = None,
    carousel_cards: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Send an approved Meta template message. Returns {"wa_message_id": ...}.

    Works for any recipient (no 24h-window restriction), provided the template is
    APPROVED in Meta. ``body_params`` are the positional values for the BODY
    placeholders ({{1}}, {{2}}, ...) in order. ``header_text_param`` / ``location`` /
    ``button_url_param`` / ``carousel_cards`` supply runtime values for a TEXT-header
    variable, a LOCATION header, a dynamic URL button and carousel cards respectively
    (each falls back to the stored template example when omitted).

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
        header_text_param=header_text_param,
        location=location,
        button_url_param=button_url_param,
        carousel_cards=carousel_cards,
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


async def upload_media(content: bytes, mime_type: str, filename: str) -> str:
    """Upload binary media to Meta and return its media id.

    The id is then referenced in a media message send; Meta keeps the binary
    ~30 days. Raises WhatsAppError on failure.
    """
    if not whatsapp_config.is_send_configured():
        raise WhatsAppError("WhatsApp Cloud API is not configured (missing phone id/token).")

    url = whatsapp_config.graph_url(f"{whatsapp_config.PHONE_NUMBER_ID}/media")
    files = {"file": (filename, content, mime_type)}
    data = {"messaging_product": "whatsapp"}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, data=data, files=files, headers=_auth_headers())

    if resp.status_code >= 400:
        error = _parse_api_error(resp)
        logger.warning("WhatsApp media upload failed (%s): %s", error.code, error.message)
        raise error

    media_id = resp.json().get("id")
    if not media_id:
        raise WhatsAppError(f"Unexpected media upload response: {resp.text}")
    return str(media_id)


async def send_media(
    to: str,
    media_type: str,
    media_id: str,
    caption: Optional[str] = None,
    filename: Optional[str] = None,
    voice: bool = False,
) -> Dict[str, Any]:
    """Send a previously uploaded media object. Returns {"wa_message_id": ...}.

    ``media_type`` must be one of image/audio/video/document. The Cloud API
    accepts ``caption`` only for image/video/document (audio rejects it) and
    ``filename`` only for document. ``voice=True`` marks an audio payload as a
    WhatsApp voice message; Meta expects OGG/Opus media for that mode.
    """
    if not whatsapp_config.is_send_configured():
        raise WhatsAppError("WhatsApp Cloud API is not configured (missing phone id/token).")
    if media_type not in {"image", "audio", "video", "document"}:
        raise WhatsAppError(f"Tipo de media no soportado para envío: {media_type}")
    if voice and media_type != "audio":
        raise WhatsAppError("Las notas de voz solo pueden enviarse como audio.")

    media_obj: Dict[str, Any] = {"id": media_id}
    if voice:
        media_obj["voice"] = True
    if caption and media_type != "audio":
        media_obj["caption"] = caption
    if filename and media_type == "document":
        media_obj["filename"] = filename

    url = whatsapp_config.graph_url(f"{whatsapp_config.PHONE_NUMBER_ID}/messages")
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": media_type,
        media_type: media_obj,
    }
    headers = {**_auth_headers(), "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=payload, headers=headers)

    if resp.status_code >= 400:
        error = _parse_api_error(resp)
        logger.warning("WhatsApp media send failed (%s): %s", error.code, error.message)
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
