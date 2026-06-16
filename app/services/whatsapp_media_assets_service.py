"""Reusable WhatsApp media asset storage and validation."""
from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.media_storage_config import media_storage_config
from app.crud import whatsappMediaAssetsCrud as crud
from app.models import WhatsAppMediaAsset
from app.services.whatsapp_cloud_service import WhatsAppError

MEDIA_KIND_TO_HEADER_FORMAT = {
    "image": "IMAGE",
    "video": "VIDEO",
    "document": "DOCUMENT",
}

HEADER_FORMAT_TO_MEDIA_KIND = {
    "IMAGE": "image",
    "VIDEO": "video",
    "DOCUMENT": "document",
}

MAX_BYTES = {
    "image": 5 * 1024 * 1024,
    "video": 16 * 1024 * 1024,
    "audio": 16 * 1024 * 1024,
    "document": 100 * 1024 * 1024,
}

ALLOWED_MIME_PREFIXES = {
    "image": ("image/jpeg", "image/png"),
    "video": ("video/mp4", "video/3gpp"),
    "audio": (
        "audio/aac",
        "audio/amr",
        "audio/mpeg",
        "audio/mp4",
        "audio/ogg",
        "audio/ogg; codecs=opus",
    ),
    "document": (
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "text/plain",
        "text/csv",
    ),
}


class MediaAssetError(ValueError):
    """Raised for user-correctable media asset validation/storage errors."""


def normalize_kind(kind: str) -> str:
    media_kind = (kind or "").strip().lower()
    if media_kind not in {"image", "video", "audio", "document"}:
        raise MediaAssetError("Tipo de media no soportado.")
    return media_kind


def assert_asset_matches_header(
    asset: Optional[WhatsAppMediaAsset], header_format: Optional[str]
) -> None:
    if not header_format:
        return
    if asset is None:
        raise MediaAssetError(
            f"La plantilla requiere media de encabezado ({header_format})."
        )
    expected = HEADER_FORMAT_TO_MEDIA_KIND.get((header_format or "").upper())
    if expected is None:
        raise MediaAssetError(f"Header de plantilla no soportado: {header_format}.")
    if (asset.media_kind or "").lower() != expected:
        raise MediaAssetError(
            f"El asset seleccionado es {asset.media_kind}; la plantilla requiere {expected}."
        )
    if (asset.status or "").lower() != "active":
        raise MediaAssetError("El asset seleccionado no esta activo.")


def public_url_from_key(storage_key: str) -> str:
    base = media_storage_config.PUBLIC_BASE_URL.rstrip("/")
    if not base:
        raise MediaAssetError("Falta MEDIA_PUBLIC_BASE_URL para construir la URL publica.")
    return f"{base}/{storage_key.lstrip('/')}"


async def upload_asset(
    db: AsyncSession,
    *,
    file,
    kind: str,
    display_name: Optional[str] = None,
    created_by_id: Optional[int] = None,
) -> WhatsAppMediaAsset:
    media_kind = normalize_kind(kind)
    original_filename = Path(getattr(file, "filename", "") or "media").name
    raw = await file.read()
    mime_type = _detect_mime_type(file, original_filename)
    validate_media(media_kind, raw, mime_type)

    digest = hashlib.sha256(raw).hexdigest()
    ext = _extension(original_filename, mime_type)
    storage_key = f"whatsapp/templates/{digest}{ext}"
    public_url = public_url_from_key(storage_key)

    existing = await crud.get_asset_by_storage_key(db, storage_key)
    if existing:
        return await _reuse_existing_asset(
            db,
            existing,
            media_kind=media_kind,
            public_url=public_url,
        )

    await _put_object(storage_key, raw, mime_type)
    try:
        return await crud.create_asset(
            db,
            media_kind=media_kind,
            display_name=(display_name or original_filename or digest).strip(),
            original_filename=original_filename,
            mime_type=mime_type,
            file_ext=ext.lstrip("."),
            file_size=len(raw),
            sha256=digest,
            storage_key=storage_key,
            public_url=public_url,
            created_by_id=created_by_id,
        )
    except IntegrityError:
        await db.rollback()
        existing = await crud.get_asset_by_storage_key(db, storage_key)
        if existing:
            return await _reuse_existing_asset(
                db,
                existing,
                media_kind=media_kind,
                public_url=public_url,
            )
        raise


async def validate_asset_url(asset: WhatsAppMediaAsset) -> WhatsAppMediaAsset:
    parsed = urlparse(asset.public_url or "")
    if parsed.scheme != "https" or not parsed.netloc:
        raise MediaAssetError("El asset no tiene una URL publica HTTPS valida.")
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), follow_redirects=True) as client:
        response = await client.head(asset.public_url)
        if response.status_code == 405:
            response = await client.get(asset.public_url, headers={"Range": "bytes=0-0"})
    if response.status_code >= 400:
        raise MediaAssetError(
            f"No se pudo validar la URL publica del asset (HTTP {response.status_code})."
        )
    return asset


async def fetch_public_asset_bytes(asset: WhatsAppMediaAsset) -> bytes:
    parsed = urlparse(asset.public_url or "")
    if parsed.scheme != "https" or not parsed.netloc:
        raise MediaAssetError("El asset no tiene una URL publica HTTPS valida.")
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=True) as client:
        response = await client.get(asset.public_url)
    if response.status_code >= 400:
        raise MediaAssetError(
            f"No se pudo descargar el asset publico para Meta (HTTP {response.status_code})."
        )
    return response.content


def validate_media(media_kind: str, raw: bytes, mime_type: str) -> None:
    max_bytes = MAX_BYTES[media_kind]
    if not raw:
        raise MediaAssetError("El archivo esta vacio.")
    if len(raw) > max_bytes:
        mb = max_bytes // (1024 * 1024)
        raise MediaAssetError(f"El archivo excede el limite para {media_kind}: {mb} MB.")
    allowed = ALLOWED_MIME_PREFIXES[media_kind]
    if mime_type not in allowed:
        allowed_list = ", ".join(allowed)
        raise MediaAssetError(
            f"MIME no soportado para {media_kind}: {mime_type}. Permitidos: {allowed_list}."
        )


async def _reuse_existing_asset(
    db: AsyncSession,
    asset: WhatsAppMediaAsset,
    *,
    media_kind: str,
    public_url: str,
) -> WhatsAppMediaAsset:
    existing_kind = (asset.media_kind or "").lower()
    if existing_kind != media_kind:
        raise MediaAssetError(
            f"El archivo ya existe como {existing_kind}; no puede reutilizarse como {media_kind}."
        )
    return await crud.make_asset_active(db, asset, public_url=public_url)


async def _put_object(storage_key: str, raw: bytes, mime_type: str) -> None:
    if not media_storage_config.is_configured():
        raise MediaAssetError(
            "Storage R2 no configurado. Define MEDIA_S3_ENDPOINT_URL, MEDIA_S3_BUCKET, "
            "MEDIA_S3_ACCESS_KEY_ID, MEDIA_S3_SECRET_ACCESS_KEY y MEDIA_PUBLIC_BASE_URL."
        )
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ImportError as exc:  # pragma: no cover - dependency issue
        raise MediaAssetError("Falta instalar boto3 para subir media a R2/S3.") from exc

    client = boto3.client(
        "s3",
        endpoint_url=media_storage_config.S3_ENDPOINT_URL,
        region_name=media_storage_config.S3_REGION,
        aws_access_key_id=media_storage_config.S3_ACCESS_KEY_ID,
        aws_secret_access_key=media_storage_config.S3_SECRET_ACCESS_KEY,
    )
    try:
        client.put_object(
            Bucket=media_storage_config.S3_BUCKET,
            Key=storage_key,
            Body=raw,
            ContentType=mime_type,
            CacheControl=media_storage_config.CACHE_CONTROL,
        )
    except (BotoCoreError, ClientError) as exc:
        raise MediaAssetError(f"No se pudo subir el archivo a R2/S3: {exc}") from exc


async def store_object(storage_key: str, raw: bytes, mime_type: str) -> str:
    """Upload arbitrary bytes to the configured object storage and return the
    public URL. Reuses the same S3/R2/MinIO backend as template assets, so chat
    media persists across container redeploys (unlike the local /uploads mount)."""
    await _put_object(storage_key, raw, mime_type)
    return public_url_from_key(storage_key)


def _detect_mime_type(file, filename: str) -> str:
    content_type = str(getattr(file, "content_type", "") or "").split(";")[0].strip()
    if content_type:
        return content_type.lower()
    guessed, _ = mimetypes.guess_type(filename)
    return (guessed or "application/octet-stream").lower()


def _extension(filename: str, mime_type: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix:
        return suffix
    guessed = mimetypes.guess_extension(mime_type) or ""
    return ".jpg" if guessed == ".jpe" else guessed


def header_format_for_kind(kind: str) -> Optional[str]:
    return MEDIA_KIND_TO_HEADER_FORMAT.get((kind or "").lower())
