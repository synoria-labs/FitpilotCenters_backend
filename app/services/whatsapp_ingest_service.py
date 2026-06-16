"""Parse and persist inbound WhatsApp Cloud API webhook payloads.

Responsibilities:
- Upsert contacts and (re)open the conversation (refreshing the 24h window).
- Insert inbound messages idempotently (dedupe by wa_message_id).
- Record delivery statuses for outbound messages.
- Schedule media downloads for non-text messages.
- Invoke the AI extension hook after each inbound message.

The INSERT into app.messages triggers pg_notify, which drives the realtime
subscription (see Phase E), so this service does not publish events itself.
"""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.db.postgresql import async_session_factory
from app.crud import whatsappCrud as crud
from app.crud import campaignsCrud as campaigns_crud
from app.services import whatsapp_media_service as media_service
from app.services.whatsapp_hooks import on_inbound_message

logger = logging.getLogger(__name__)

MEDIA_TYPES = {"image", "audio", "video", "document", "sticker"}


def _to_dt(ts: Any) -> datetime:
    """Cloud API timestamps are unix epoch seconds (as strings)."""
    try:
        return datetime.utcfromtimestamp(int(ts))
    except (TypeError, ValueError):
        return datetime.utcnow()


async def process(payload: Dict[str, Any]) -> List[int]:
    """Process a full webhook payload. Returns the ids of newly inserted messages."""
    new_message_ids: List[int] = []
    pending_media: List[tuple] = []  # (media_row_id, cloud_media_id)

    async with async_session_factory() as db:
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value", {}) or {}

                # Build wa_id -> profile name map from the contacts block.
                profile_names: Dict[str, str] = {}
                for c in value.get("contacts", []) or []:
                    wa_id = c.get("wa_id")
                    name = (c.get("profile") or {}).get("name")
                    if wa_id and name:
                        profile_names[wa_id] = name

                for msg in value.get("messages", []) or []:
                    created = await _process_message(db, msg, profile_names, pending_media)
                    if created is not None:
                        new_message_ids.append(created)

                for st in value.get("statuses", []) or []:
                    await _process_status(db, st)

        await db.commit()

    # Schedule downloads only after the rows are committed.
    for media_row_id, cloud_media_id in pending_media:
        media_service.schedule_download(media_row_id, cloud_media_id)

    return new_message_ids


async def _process_message(
    db,
    msg: Dict[str, Any],
    profile_names: Dict[str, str],
    pending_media: List[tuple],
) -> Optional[int]:
    wa_id = msg.get("from")
    wa_message_id = msg.get("id")
    msg_type = msg.get("type", "text")
    if not wa_id:
        return None

    contact = await crud.upsert_contact(
        db, wa_id=wa_id, phone_number=wa_id, profile_name=profile_names.get(wa_id)
    )
    ts = _to_dt(msg.get("timestamp"))
    conversation = await crud.get_or_open_conversation(db, contact.id, window_anchor=ts)

    text_content: Optional[str] = None
    media_obj: Dict[str, Any] = {}
    context_message_id = (msg.get("context") or {}).get("id")
    if msg_type == "text":
        text_content = (msg.get("text") or {}).get("body")
    elif msg_type in MEDIA_TYPES:
        media_obj = msg.get(msg_type) or {}
        text_content = media_obj.get("caption")
    elif msg_type == "reaction":
        # Reactions carry a ``reaction`` object (not a ``context``): store the emoji
        # in text_content and the reacted-to message id in context_message_id. An
        # empty emoji means the reaction was removed (kept verbatim).
        reaction = msg.get("reaction") or {}
        text_content = reaction.get("emoji")
        context_message_id = reaction.get("message_id")

    message = await crud.insert_inbound_message(
        db,
        conversation_id=conversation.id,
        contact_id=contact.id,
        message_type=msg_type,
        timestamp=ts,
        wa_message_id=wa_message_id,
        text_content=text_content,
        context_message_id=context_message_id,
    )
    if message is None:
        return None  # duplicate delivery, already stored

    # Media: create the row now, download in the background after commit.
    if msg_type in MEDIA_TYPES and media_obj.get("id"):
        media_row = await crud.insert_media(
            db,
            message_id=message.id,
            media_type=msg_type,
            mime_type=media_obj.get("mime_type"),
            caption=media_obj.get("caption"),
            cloud_media_id=media_obj["id"],
        )
        pending_media.append((media_row.id, media_obj["id"]))

    try:
        await on_inbound_message(db, message, contact, conversation)
    except Exception as e:  # noqa: BLE001 - never let the hook break ingestion
        logger.warning("on_inbound_message hook failed: %s", e)

    return message.id


async def _process_status(db, st: Dict[str, Any]) -> None:
    wa_message_id = st.get("id")
    status = st.get("status")
    if not wa_message_id or not status:
        return
    ts = _to_dt(st.get("timestamp"))
    await crud.insert_message_status(
        db,
        wa_message_id=wa_message_id,
        status=status,
        timestamp=ts,
    )
    # Mirror the delivery status onto the campaign recipient (if this outbound message
    # belongs to a campaign). Never let campaign tracking break webhook ingestion.
    try:
        await campaigns_crud.apply_delivery_status(
            db, wa_message_id=wa_message_id, meta_status=status, timestamp=ts
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("campaign delivery-status update failed: %s", e)
