"""Extension hooks for the WhatsApp pipeline.

This is the enganche point for a future AI auto-responder (the behaviour the external
bot used to provide). A later phase can implement ``on_inbound_message`` to read/write
``app.chat_memory`` / ``app.chat_kv`` and call
``app.services.whatsapp_cloud_service.send_text`` to reply automatically.

Keep this signature stable so the ingest pipeline does not need to change when the
auto-responder is added.
"""
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Message, Contact, Conversation

logger = logging.getLogger(__name__)


async def on_inbound_message(
    db: AsyncSession,
    message: Message,
    contact: Contact,
    conversation: Conversation,
) -> None:
    """Called after each inbound message is persisted. No-op for now."""
    logger.debug(
        "on_inbound_message hook (no-op): msg=%s contact=%s", message.id, contact.wa_id
    )
    return None
