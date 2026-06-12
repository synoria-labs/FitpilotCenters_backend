"""Resolve runtime media for WhatsApp template sends.

Template media samples are used only for Meta review. Sends must provide a runtime
header media source when the approved template has an IMAGE/VIDEO/DOCUMENT header.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import whatsappMediaAssetsCrud as media_crud
from app.models import WhatsAppTemplate
from app.services import whatsapp_media_assets_service as media_service
from app.services.whatsapp_template_components import required_header_media_format


@dataclass(frozen=True)
class ResolvedHeaderMedia:
    media_format: Optional[str]
    media_url: Optional[str]
    media_id: Optional[str]
    source: str


def _clean_https_url(value: Optional[str]) -> Optional[str]:
    url = (value or "").strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise media_service.MediaAssetError("La URL de media debe ser una URL publica HTTPS.")
    return url


async def resolve_template_send_header_media(
    db: AsyncSession,
    *,
    template: WhatsAppTemplate,
    override_media_asset_id: Optional[int] = None,
    legacy_header_media_url: Optional[str] = None,
    header_media_id: Optional[str] = None,
) -> ResolvedHeaderMedia:
    """Resolve the media source to pass to ``cloud.send_template``.

    Priority:
    1. Direct WhatsApp ``header_media_id``.
    2. Per-send/per-event override asset.
    3. Template default asset.
    4. Legacy HTTPS URL.
    """
    media_format = required_header_media_format(template.components)
    media_id = (header_media_id or "").strip() or None
    legacy_url = _clean_https_url(legacy_header_media_url)

    if not media_format:
        if media_id or override_media_asset_id or legacy_url:
            raise media_service.MediaAssetError(
                "La plantilla seleccionada no requiere media de encabezado."
            )
        return ResolvedHeaderMedia(
            media_format=None,
            media_url=None,
            media_id=None,
            source="none",
        )

    if media_id:
        return ResolvedHeaderMedia(
            media_format=media_format,
            media_url=None,
            media_id=media_id,
            source="id",
        )

    if override_media_asset_id:
        asset = await media_crud.get_asset_model(db, override_media_asset_id)
        media_service.assert_asset_matches_header(asset, media_format)
        return ResolvedHeaderMedia(
            media_format=media_format,
            media_url=asset.public_url,
            media_id=None,
            source="override_asset",
        )

    if template.default_header_media_asset_id:
        asset = await media_crud.get_asset_model(db, template.default_header_media_asset_id)
        media_service.assert_asset_matches_header(asset, media_format)
        return ResolvedHeaderMedia(
            media_format=media_format,
            media_url=asset.public_url,
            media_id=None,
            source="template_default_asset",
        )

    if legacy_url:
        return ResolvedHeaderMedia(
            media_format=media_format,
            media_url=legacy_url,
            media_id=None,
            source="legacy_url",
        )

    raise media_service.MediaAssetError(
        f"La plantilla requiere media de encabezado ({media_format}); "
        "selecciona un asset, usa el default de la plantilla o agrega una URL HTTPS."
    )
