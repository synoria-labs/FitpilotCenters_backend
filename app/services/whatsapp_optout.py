"""Inbound STOP/BAJA/ALTA keyword handling for WhatsApp.

Called from ``on_inbound_message`` BEFORE the bot is scheduled, in the ingest session, so the
keyword consumes the turn and the bot never double-replies. STOP/BAJA revoke marketing consent +
pause the bot (effectively disabled) + confirm; ALTA/START re-grant + resume + confirm.

Note: only MARKETING is governed by consent — a customer-initiated service reply still gets a bot
answer; STOP additionally pauses the bot because people who say STOP usually want to be left alone.
"""
import logging
import unicodedata
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.outbound_config import outbound_config
from app.crud import membersCrud
from app.crud import optInCrud

logger = logging.getLogger(__name__)

# ~100 years => effectively disabled until an explicit ALTA clears it.
_DISABLE_DELTA = timedelta(days=36500)

_OPTOUT_REPLY = (
    "Listo ✅ ya no recibirás mensajes promocionales. Si quieres reactivarlos, escribe *ALTA*."
)
_OPTIN_REPLY = (
    "¡Listo! 🎉 Reactivamos tus mensajes y el asistente. ¿En qué te puedo ayudar?"
)


# Opt-out keywords that also mean "cancel what I'm buying". While a purchase is in progress the
# customer almost certainly means the purchase, not "never message me again".
_PURCHASE_AMBIGUOUS = {"CANCELAR"}


def _normalize(text: str) -> str:
    t = unicodedata.normalize("NFKD", (text or "").strip())
    t = "".join(c for c in t if not unicodedata.combining(c))  # strip accents
    return t.upper()


async def _purchase_in_progress(db: AsyncSession, conversation) -> bool:
    """A chatbot purchase or a special-event order is waiting on this conversation."""
    from sqlalchemy import select

    from app.crud import chatbotPendingCrud
    from app.models.specialEventModel import ORDER_AWAITING_PAYMENT, SpecialEventOrder

    if await chatbotPendingCrud.get_active_pending(db, conversation.id) is not None:
        return True
    row = await db.execute(
        select(SpecialEventOrder.id)
        .where(
            SpecialEventOrder.conversation_id == conversation.id,
            SpecialEventOrder.status == ORDER_AWAITING_PAYMENT,
        )
        .limit(1)
    )
    return row.first() is not None


async def handle_keyword(db: AsyncSession, message, contact, conversation) -> bool:
    """Return True if the inbound message was an opt-out/opt-in keyword (turn consumed)."""
    word = _normalize(message.text_content)
    is_optout = word in outbound_config.OPTOUT_KEYWORDS
    is_optin = word in outbound_config.OPTIN_KEYWORDS
    if not (is_optout or is_optin):
        return False
    if is_optout and word in _PURCHASE_AMBIGUOUS and await _purchase_in_progress(db, conversation):
        # Let the assistant cancel the purchase instead of unsubscribing the customer.
        return False

    person_id = await membersCrud.get_member_id_by_wa_id(db, contact.wa_id)
    evidence = {"wa_message_id": message.wa_message_id, "text": message.text_content}

    if is_optout:
        if person_id is not None:
            await optInCrud.revoke_whatsapp_consent(db, person_id, evidence=evidence)
        else:
            logger.info("opt-out from unknown person (wa_id=%s); pausing bot only", contact.wa_id)
        conversation.bot_paused_until = datetime.utcnow() + _DISABLE_DELTA
        reply = _OPTOUT_REPLY
    else:
        if person_id is not None:
            await optInCrud.grant_whatsapp_consent(db, person_id, evidence=evidence)
        conversation.bot_paused_until = None
        reply = _OPTIN_REPLY

    # Confirmation: send in the BACKGROUND (its own session) so the ingest session commits the
    # consent + bot pause immediately and the per-contact advisory lock is NOT held across the Meta
    # HTTP call on the webhook path. kind=chatbot_reply is transactional and does NOT pause the bot.
    from app.services.chatbot import reply_service

    reply_service.schedule_text_send(conversation.id, contact.id, contact.wa_id, reply)
    # The ingest pipeline commits the session (consent + bot_paused_until) right after this returns.
    return True
