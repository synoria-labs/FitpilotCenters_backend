"""Download inbound WhatsApp media and persist it.

Media arrives by reference (a media id) in the webhook. We resolve the id to a
temporary URL, download the bytes (authenticated), and store them. Storage
prefers the configured object store (MinIO/R2 — survives container redeploys)
and falls back to the local ``backend/uploads/whatsapp/`` mount for local dev.
Downloads run as background tasks so the webhook can return 200 immediately.
"""
import asyncio
import hashlib
import logging
import mimetypes
from pathlib import Path
from typing import Optional, Set

from app.core.media_storage_config import media_storage_config
from app.db.postgresql import async_session_factory
from app.services import whatsapp_cloud_service as cloud
from app.services import whatsapp_media_assets_service as media_assets
from app.crud.whatsappCrud import mark_media_downloaded, mark_media_failed

logger = logging.getLogger(__name__)

UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "uploads" / "whatsapp"


async def store_media_bytes(
    data: bytes, *, sha256: str, ext: str, filename: str, mime_type: Optional[str]
) -> str:
    """Persist chat media bytes and return the value for ``media.media_url``.

    Uses object storage (MinIO/R2, served via ``MEDIA_PUBLIC_BASE_URL``) when
    configured so files survive redeploys; otherwise writes to the local
    ``/uploads`` mount (dev). Used by both inbound downloads and outbound sends.
    """
    if media_storage_config.is_configured():
        key = f"whatsapp/chat/{sha256}{ext}"
        return await media_assets.store_object(
            key, data, mime_type or "application/octet-stream"
        )
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR / filename).write_bytes(data)
    return f"/uploads/whatsapp/{filename}"

# Keep strong references so background tasks are not garbage-collected.
_background_tasks: Set["asyncio.Task"] = set()


def schedule_download(media_row_id: int, cloud_media_id: str) -> None:
    """Fire-and-forget a media download on the running event loop."""
    try:
        task = asyncio.create_task(_download_and_store(media_row_id, cloud_media_id))
    except RuntimeError:
        # No running loop (e.g. called from a sync context) — run inline as fallback.
        asyncio.run(_download_and_store(media_row_id, cloud_media_id))
        return
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


MAX_ATTEMPTS = 3


async def _download_and_store(media_row_id: int, cloud_media_id: str) -> None:
    last_error: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            # 2s then 8s. Re-resolving the metadata below is mandatory: the
            # temporary download URL Meta returns expires within minutes.
            await asyncio.sleep(2 * 4 ** (attempt - 1))
        try:
            meta = await cloud.get_media_metadata(cloud_media_id)
            media_url = meta.get("url")
            mime_type = meta.get("mime_type")
            if not media_url:
                raise cloud.WhatsAppError(f"No download URL for media {cloud_media_id}")

            data = await cloud.download_media_bytes(media_url)
            sha = hashlib.sha256(data).hexdigest()
            ext = mimetypes.guess_extension(mime_type or "") or ""
            filename = f"{cloud_media_id}{ext}"

            stored_url = await store_media_bytes(
                data, sha256=sha, ext=ext, filename=filename, mime_type=mime_type
            )

            async with async_session_factory() as db:
                await mark_media_downloaded(
                    db,
                    media_row_id,
                    sha256=sha,
                    filename=filename,
                    file_size=len(data),
                    media_url=stored_url,
                    mime_type=mime_type,
                )
                await db.commit()
            logger.info("Downloaded WhatsApp media %s -> %s", cloud_media_id, filename)
            return
        except Exception as e:  # noqa: BLE001
            last_error = e
            logger.warning(
                "Failed to download media %s (attempt %d/%d): %s",
                cloud_media_id, attempt + 1, MAX_ATTEMPTS, e,
            )

    logger.error("Giving up on media %s: %s", cloud_media_id, last_error)
    async with async_session_factory() as db:
        try:
            await mark_media_failed(db, media_row_id)
            await db.commit()
        except Exception:  # noqa: BLE001
            await db.rollback()
