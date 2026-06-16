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


def _normalize(text: str) -> str:
    t = unicodedata.normalize("NFKD", (text or "").strip())
    t = "".join(c for c in t if not unicodedata.combining(c))  # strip accents
    return t.upper()


async def handle_keyword(db: AsyncSession, message, contact, conversation) -> bool:
    """Return True if the inbound message was an opt-out/opt-in keyword (turn consumed)."""
    word = _normalize(message.text_content)
    is_optout = word in outbound_config.OPTOUT_KEYWORDS
    is_optin = word in outbound_config.OPTIN_KEYWORDS
    if not (is_optout or is_optin):
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

    # Confirmation: transactional reply (kind=chatbot_reply does NOT pause the bot, unlike a manual
    # human send). The inbound just refreshed the 24h window, so the free-form text is allowed.
    from app.services import whatsapp_outbound as outbound

    try:
        await outbound.send_text(
            db,
            kind=outbound.KIND_CHATBOT_REPLY,
            conversation_id=conversation.id,
            contact_id=contact.id,
            wa_id=contact.wa_id,
            text=reply,
            persist=True,
        )
    except Exception:  # noqa: BLE001
        logger.exception("opt-out: failed to send confirmation to %s", contact.wa_id)
    # The ingest pipeline commits the session right after on_inbound_message returns.
    return True
