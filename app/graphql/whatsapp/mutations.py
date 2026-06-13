"""GraphQL mutations for the WhatsApp chat feature."""
import hashlib
import logging
import mimetypes
from pathlib import Path
from typing import Optional, Tuple

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.file_uploads import Upload
from strawberry.types import Info

from app.crud import whatsappCrud as crud
from app.graphql.whatsapp.types import (
    SendMediaMessageInput,
    SendTextMessageInput,
    SendMessageResult,
    ChatMessage,
)
from app.graphql.auth.permissions import IsAuthenticated
from app.models import Contact, Conversation
from app.services import whatsapp_cloud_service as cloud
from app.services import whatsapp_media_service as media_service
from app.services.whatsapp_media_assets_service import (
    MediaAssetError,
    _detect_mime_type,
    validate_media,
)

logger = logging.getLogger(__name__)

# WhatsApp message types that map directly from a MIME prefix. Anything else
# (pdf, office docs, plain text, ...) is sent as a document.
_MIME_PREFIX_TO_KIND = {"image/": "image", "audio/": "audio", "video/": "video"}


async def _resolve_target(
    db: AsyncSession, conversation_id: Optional[int], wa_id: Optional[str]
) -> Tuple[Optional[Contact], Optional[Conversation], Optional[str]]:
    """Resolve the destination contact + conversation for a send.

    Returns (contact, conversation, error). Exactly one of the pair
    (contact+conversation) or error is set.
    """
    if conversation_id:
        conversation = await crud.get_conversation(db, conversation_id)
        if conversation is None:
            return None, None, "Conversación no encontrada."
        return conversation.contact, conversation, None
    if wa_id:
        # Resolve by normalized number (52/521 aware) so a send never spawns a
        # duplicate contact/conversation for a number that already exists.
        contact = await crud.upsert_contact(
            db, wa_id=wa_id, phone_number=wa_id, authoritative=False
        )
        conversation = await crud.get_or_open_conversation(db, contact.id)
        return contact, conversation, None
    return None, None, "Falta conversationId o waId."


def _media_kind_for_mime(mime_type: str) -> str:
    for prefix, kind in _MIME_PREFIX_TO_KIND.items():
        if mime_type.startswith(prefix):
            return kind
    return "document"


@strawberry.type
class WhatsAppChatMutation:
    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def send_text_message(
        self, info: Info, input: SendTextMessageInput
    ) -> SendMessageResult:
        """Send a free-form text message via the Cloud API and persist it."""
        db: AsyncSession = info.context.db

        text = (input.text or "").strip()
        if not text:
            return SendMessageResult(success=False, error="El mensaje está vacío.")

        contact, conversation, error = await _resolve_target(
            db, input.conversation_id, input.wa_id
        )
        if error:
            return SendMessageResult(success=False, error=error)

        # Send via the Cloud API.
        try:
            result = await cloud.send_text(to=contact.wa_id, text=text)
        except cloud.WhatsAppError as e:
            await db.rollback()
            return SendMessageResult(success=False, error=e.message)
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            logger.exception("Unexpected error sending WhatsApp message")
            return SendMessageResult(success=False, error=str(e))

        # Persist the outbound message (the DB trigger fans it out to subscribers).
        message = await crud.insert_outbound_message(
            db,
            conversation_id=conversation.id,
            contact_id=contact.id,
            text=text,
            wa_message_id=result.get("wa_message_id"),
        )
        await db.commit()

        return SendMessageResult(
            success=True,
            message=ChatMessage.from_data(crud.ChatMessageData.from_model(message)),
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def send_media_message(
        self, info: Info, input: SendMediaMessageInput, file: Upload
    ) -> SendMessageResult:
        """Send a media message (image/audio/video/document) via the Cloud API.

        Uploads the binary to Meta, sends it, stores a local copy under
        ``/uploads/whatsapp/`` (so the bubble renders immediately) and persists
        the Message + Media rows.
        """
        db: AsyncSession = info.context.db

        contact, conversation, error = await _resolve_target(
            db, input.conversation_id, input.wa_id
        )
        if error:
            return SendMessageResult(success=False, error=error)

        original_filename = Path(getattr(file, "filename", "") or "archivo").name
        raw = await file.read()
        mime_type = _detect_mime_type(file, original_filename)
        media_kind = _media_kind_for_mime(mime_type)
        caption = (input.caption or "").strip() or None
        if media_kind == "audio":
            caption = None  # the Cloud API rejects captions on audio

        try:
            validate_media(media_kind, raw, mime_type)
        except MediaAssetError as e:
            return SendMessageResult(success=False, error=str(e))

        try:
            media_id = await cloud.upload_media(raw, mime_type, original_filename)
            result = await cloud.send_media(
                to=contact.wa_id,
                media_type=media_kind,
                media_id=media_id,
                caption=caption,
                filename=original_filename if media_kind == "document" else None,
            )
        except cloud.WhatsAppError as e:
            await db.rollback()
            return SendMessageResult(success=False, error=e.message)
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            logger.exception("Unexpected error sending WhatsApp media")
            return SendMessageResult(success=False, error=str(e))

        # Local copy, same naming convention as inbound downloads.
        ext = Path(original_filename).suffix.lower() or (
            mimetypes.guess_extension(mime_type) or ""
        )
        stored_filename = f"{media_id}{ext}"
        try:
            media_service.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            (media_service.UPLOAD_DIR / stored_filename).write_bytes(raw)
            local_url = f"/uploads/whatsapp/{stored_filename}"
        except OSError:
            logger.exception("Could not store local copy of sent media %s", media_id)
            local_url = None

        message = await crud.insert_outbound_message(
            db,
            conversation_id=conversation.id,
            contact_id=contact.id,
            text=caption,
            wa_message_id=result.get("wa_message_id"),
            message_type=media_kind,
        )
        await crud.insert_outbound_media(
            db,
            message_id=message.id,
            media_type=media_kind,
            mime_type=mime_type,
            filename=original_filename,
            file_size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            media_url=local_url,
            caption=caption,
            cloud_media_id=media_id,
        )
        await db.commit()

        # Re-fetch with the media relation eager-loaded so the result (and the
        # realtime fan-out) carries the attachment metadata.
        data = await crud.get_message_by_id(db, message.id)
        return SendMessageResult(
            success=True,
            message=ChatMessage.from_data(data) if data else None,
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def retry_media_download(self, info: Info, message_id: int) -> SendMessageResult:
        """Re-download the attachment of a message from Meta (after a failure
        or a lost local file). Requires the stored cloud media id."""
        db: AsyncSession = info.context.db

        media = await crud.get_media_for_retry(db, message_id)
        if media is None:
            return SendMessageResult(success=False, error="El mensaje no tiene archivo adjunto.")
        if not media.cloud_media_id:
            return SendMessageResult(
                success=False,
                error="No se puede reintentar: el id de media de Meta no está disponible.",
            )

        media.download_failed = 0
        await db.commit()
        media_service.schedule_download(media.id, media.cloud_media_id)

        data = await crud.get_message_by_id(db, message_id)
        return SendMessageResult(
            success=True,
            message=ChatMessage.from_data(data) if data else None,
        )
