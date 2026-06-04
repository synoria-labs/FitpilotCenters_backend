"""Download inbound WhatsApp media to local storage.

Media arrives by reference (a media id) in the webhook. We resolve the id to a
temporary URL, download the bytes (authenticated), store them under
``backend/uploads/whatsapp/`` (served by the existing ``/uploads`` static mount) and
update the ``media`` row. Downloads run as background tasks so the webhook can return
200 immediately.
"""
import asyncio
import hashlib
import logging
import mimetypes
from pathlib import Path
from typing import Set

from app.db.postgresql import async_session_factory
from app.services import whatsapp_cloud_service as cloud
from app.crud.whatsappCrud import mark_media_downloaded, mark_media_failed

logger = logging.getLogger(__name__)

UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "uploads" / "whatsapp"

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


async def _download_and_store(media_row_id: int, cloud_media_id: str) -> None:
    async with async_session_factory() as db:
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

            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            (UPLOAD_DIR / filename).write_bytes(data)

            await mark_media_downloaded(
                db,
                media_row_id,
                sha256=sha,
                filename=filename,
                file_size=len(data),
                media_url=f"/uploads/whatsapp/{filename}",
                mime_type=mime_type,
            )
            await db.commit()
            logger.info("Downloaded WhatsApp media %s -> %s", cloud_media_id, filename)
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to download media %s: %s", cloud_media_id, e)
            try:
                await mark_media_failed(db, media_row_id)
                await db.commit()
            except Exception:  # noqa: BLE001
                await db.rollback()
