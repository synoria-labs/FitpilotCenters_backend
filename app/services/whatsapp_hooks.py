"""Extension hooks for the WhatsApp pipeline.

This is the enganche point for the AI auto-responder. ``on_inbound_message`` schedules the
LangChain chatbot agent to reply (in the background, off the webhook request path). The agent
itself opens its own DB session, loads the conversation history, and sends + persists a reply
via ``app.services.chatbot.reply_service``.

Keep this signature stable so the ingest pipeline does not need to change.
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
    """Called after each inbound message is persisted.

    Schedules the chatbot agent to reply in the background. Only inbound text messages are
    handled; media/reactions/statuses are ignored. We pass plain values (not ORM objects) into
    the background task because the ingest session is committed and closed right after this
    hook returns.
    """
    if message.direction != "inbound" or message.message_type != "text":
        return None

    text = (message.text_content or "").strip()
    if not text:
        return None

    # STOP/BAJA/ALTA keywords consume the turn (consent + bot pause/resume + confirmation); the bot
    # must NOT also reply.
    from app.services import whatsapp_optout

    if await whatsapp_optout.handle_keyword(db, message, contact, conversation):
        return None

    # Bot coexistence: don't auto-reply if the bot is disabled (robot button off) or temporarily
    # paused for this conversation (a human is handling it / STOP). bot_paused_until is naive-UTC.
    from datetime import datetime

    if getattr(conversation, "bot_enabled", True) is False:
        return None
    paused_until = getattr(conversation, "bot_paused_until", None)
    if paused_until is not None and paused_until > datetime.utcnow():
        return None

    logger.debug(
        "on_inbound_message: scheduling chatbot reply msg=%s contact=%s",
        message.id, contact.wa_id,
    )
    # Lazy import: keeps the core ingest pipeline free of the LangChain deps at import time.
    from app.services.chatbot import reply_service

    reply_service.schedule_agent_reply(
        conversation_id=conversation.id,
        contact_id=contact.id,
        contact_wa_id=contact.wa_id,
        message_id=message.id,
        text=text,
    )
    return None
