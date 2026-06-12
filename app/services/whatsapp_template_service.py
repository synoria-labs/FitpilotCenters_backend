"""Client for the WhatsApp Business Management API (message templates).

Separate from ``whatsapp_cloud_service`` (which sends messages from the phone number):
template management lives on the WhatsApp Business Account (WABA) and requires the
``whatsapp_business_management`` permission on the access token. Used by the template
CRUD mutations to keep the local ``app.whatsapp_templates`` mirror in sync with Meta.
"""
import logging
from typing import Any, Dict, List, Optional

import httpx

from app.core.whatsapp_config import whatsapp_config
# Reuse the shared error type and helpers from the cloud service.
from app.services.whatsapp_cloud_service import (
    WhatsAppError,
    _auth_headers,
    _parse_api_error,
    _TIMEOUT,
)

logger = logging.getLogger(__name__)

# Fields requested when listing templates from Meta.
_TEMPLATE_FIELDS = "id,name,status,category,language,components"


def _require_management() -> None:
    if not whatsapp_config.is_management_configured():
        raise WhatsAppError(
            "Gestión de plantillas no configurada (falta WHATSAPP_BUSINESS_ACCOUNT_ID o token)."
        )


async def fetch_namespace() -> Optional[str]:
    """Return the WABA's message_template_namespace (needed to persist new templates)."""
    _require_management()
    url = whatsapp_config.graph_url(whatsapp_config.BUSINESS_ACCOUNT_ID)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            url,
            params={"fields": "message_template_namespace"},
            headers=_auth_headers(),
        )
    if resp.status_code >= 400:
        raise _parse_api_error(resp)
    return resp.json().get("message_template_namespace")


async def list_templates() -> List[Dict[str, Any]]:
    """List all message templates on the WABA (follows pagination)."""
    _require_management()
    url = whatsapp_config.graph_url(
        f"{whatsapp_config.BUSINESS_ACCOUNT_ID}/message_templates"
    )
    params: Optional[Dict[str, Any]] = {"fields": _TEMPLATE_FIELDS, "limit": 200}
    results: List[Dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        next_url: Optional[str] = url
        while next_url:
            resp = await client.get(next_url, params=params, headers=_auth_headers())
            if resp.status_code >= 400:
                raise _parse_api_error(resp)
            body = resp.json()
            results.extend(body.get("data", []))
            # Cursor pagination: the "next" URL already carries fields/cursor.
            next_url = (body.get("paging") or {}).get("next")
            params = None

    return results


async def create_template(
    name: str,
    language: str,
    category: str,
    components: List[dict],
) -> Dict[str, Any]:
    """Submit a new template to Meta for approval. Returns {id, status, category}."""
    _require_management()
    url = whatsapp_config.graph_url(
        f"{whatsapp_config.BUSINESS_ACCOUNT_ID}/message_templates"
    )
    payload = {
        "name": name,
        "language": language,
        "category": category,
        "components": components,
    }
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=payload, headers=headers)
    if resp.status_code >= 400:
        raise _parse_api_error(resp)
    return resp.json()


async def edit_template(
    meta_template_id: str,
    components: List[dict],
    category: Optional[str] = None,
) -> Dict[str, Any]:
    """Edit the components (and optionally category) of an existing Meta template.

    Only the components/category of an APPROVED or REJECTED template can be edited; name,
    language and the placeholder structure cannot change after creation.
    """
    _require_management()
    url = whatsapp_config.graph_url(meta_template_id)
    payload: Dict[str, Any] = {"components": components}
    if category:
        payload["category"] = category
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=payload, headers=headers)
    if resp.status_code >= 400:
        raise _parse_api_error(resp)
    return resp.json()


async def delete_template(
    name: str,
    meta_template_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Delete a template in Meta.

    With ``name`` only, Meta deletes every language variant of that name. When
    ``meta_template_id`` is given it is passed as ``hsm_id`` to delete just that variant.
    """
    _require_management()
    url = whatsapp_config.graph_url(
        f"{whatsapp_config.BUSINESS_ACCOUNT_ID}/message_templates"
    )
    params: Dict[str, Any] = {"name": name}
    if meta_template_id:
        params["hsm_id"] = meta_template_id
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.delete(url, params=params, headers=_auth_headers())
    if resp.status_code >= 400:
        raise _parse_api_error(resp)
    return resp.json()


async def upload_template_header_sample(
    *,
    filename: str,
    mime_type: str,
    content: bytes,
) -> str:
    """Upload example media to Meta and return the handle used in ``header_handle``.

    Media headers in template creation do not use the public send URL. Meta requires an
    asset handle generated through the Graph Resumable Upload API and then stores that
    handle under ``components[].example.header_handle`` for template review.
    """
    _require_management()
    owner_id = whatsapp_config.upload_owner_id()
    if not owner_id:
        raise WhatsAppError(
            "Falta WHATSAPP_APP_ID o WHATSAPP_UPLOAD_OWNER_ID para subir la muestra de media a Meta."
        )
    if not content:
        raise WhatsAppError("El archivo de muestra esta vacio.")

    start_url = whatsapp_config.graph_url(f"{owner_id}/uploads")
    params = {
        "file_name": filename,
        "file_length": str(len(content)),
        "file_type": mime_type,
        "access_token": whatsapp_config.ACCESS_TOKEN,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        start = await client.post(start_url, params=params)
        if start.status_code >= 400:
            raise _parse_api_error(start)
        session_id = start.json().get("id")
        if not session_id:
            raise WhatsAppError(f"Meta no devolvio upload session id: {start.text}")

        upload_url = whatsapp_config.graph_url(str(session_id))
        upload = await client.post(
            upload_url,
            content=content,
            headers={
                **_auth_headers(),
                "Content-Type": "application/octet-stream",
                "file_offset": "0",
            },
        )
        if upload.status_code >= 400:
            raise _parse_api_error(upload)

    payload = upload.json()
    handle = str(payload.get("h") or payload.get("id") or "").strip()
    if not handle:
        raise WhatsAppError(f"Meta no devolvio header handle: {payload}")
    return handle
