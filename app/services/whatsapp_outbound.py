"""Unified WhatsApp outbound gateway.

Every outbound WhatsApp send routes through here so the subsystems (manual chat, chatbot,
payment confirmation, auto-notifications, marketing campaigns, template test) coexist without
conflicts. The gateway is a thin coordinator over the existing ``whatsapp_cloud_service`` send
functions + ``whatsappCrud.insert_outbound_message``; it centralizes:

* **Per-contact serialization** — a transaction-scoped Postgres advisory lock keyed on the
  contact's number, so two sends to the SAME contact never race/interleave (e.g. a chatbot reply
  and a payment confirmation firing at the same instant).
* **Message class ledger** — every persisted send records its ``message_class`` on ``app.messages``,
  which is the source of truth for the marketing frequency cap (Phase 2).
* **Transactional safety** — a payment confirmation is never silently dropped outside the 24h
  window; it is recorded for reconciliation.

Gates for consent / frequency / quiet-hours / bot-pause are layered in by later phases at the
clearly-marked seam below; Phase 1 activates only the lock + class recording + payment safety, so
it is behavior-preserving except for serialization.
"""
import hashlib
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Optional

from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import whatsappCrud as crud
from app.models.whatsappModel import WebhookLog
from app.services import whatsapp_cloud_service as cloud

logger = logging.getLogger(__name__)

# --- message classes ---------------------------------------------------------------
CLASS_TRANSACTIONAL = "transactional"
CLASS_MARKETING = "marketing"

# --- send kinds (which subsystem) --------------------------------------------------
KIND_MANUAL_HUMAN = "manual_human"
KIND_CHATBOT_REPLY = "chatbot_reply"
KIND_PAYMENT_CONFIRM = "payment_confirm"
KIND_NOTIFICATION = "notification"
KIND_CAMPAIGN = "campaign"
KIND_TEMPLATE_TEST = "template_test"

# Meta error codes.
_OUTSIDE_WINDOW_CODE = 131047
_RATE_LIMIT_CODES = {130429, 131048, 131056, 80007}


class SendStatus(str, Enum):
    SENT = "sent"
    SUPPRESSED = "suppressed"   # policy said don't send (consent / cap)
    DEFERRED = "deferred"       # try again later (quiet hours / rate limit / window)
    FAILED = "failed"           # the send genuinely errored


@dataclass
class OutboundResult:
    status: SendStatus
    reason: Optional[str] = None
    wa_message_id: Optional[str] = None
    message_id: Optional[int] = None
    error_code: Optional[int] = None
    message: Optional[Any] = None  # the persisted ORM Message (when persist=True and SENT)

    @property
    def ok(self) -> bool:
        return self.status is SendStatus.SENT


def _lock_key(wa_id: str) -> int:
    """Stable signed 64-bit advisory-lock key from a contact's digits (process-independent)."""
    digits = re.sub(r"\D", "", wa_id or "")
    h = hashlib.blake2b(digits.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(h, "big", signed=True)


async def _acquire_contact_lock(db: AsyncSession, wa_id: str) -> None:
    # Transaction-scoped: auto-released at commit/rollback (no leak on pooled asyncpg conns).
    await db.execute(sa_text("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_key(wa_id)})


def _in_quiet_hours() -> bool:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.core.outbound_config import outbound_config

    s, e = outbound_config.QUIET_HOURS_START, outbound_config.QUIET_HOURS_END
    if s == e:
        return False
    hour = datetime.now(ZoneInfo(outbound_config.QUIET_HOURS_TZ)).hour
    return (s <= hour < e) if s < e else (hour >= s or hour < e)


async def _marketing_gates(
    db: AsyncSession, *, person_id: Optional[int], contact_id: int, conversation_id: int
) -> Optional[OutboundResult]:
    """Policy gates that apply ONLY to marketing-class sends. Returns None when allowed."""
    from app.core.outbound_config import outbound_config
    from app.crud import chatbotPendingCrud
    from app.services.notification_service import _is_opted_out

    if person_id is not None and await _is_opted_out(db, person_id):
        return OutboundResult(SendStatus.SUPPRESSED, reason="no_consent")
    if conversation_id and await chatbotPendingCrud.get_active_pending(db, conversation_id):
        # Customer is mid-purchase with the bot — don't interrupt with marketing.
        return OutboundResult(SendStatus.DEFERRED, reason="pending_action")
    if _in_quiet_hours():
        return OutboundResult(SendStatus.DEFERRED, reason="quiet_hours")
    cap = outbound_config.MARKETING_DAILY_CAP
    if cap > 0 and await crud.count_marketing_sends_today(db, contact_id) >= cap:
        return OutboundResult(SendStatus.SUPPRESSED, reason="daily_cap")
    return None


async def _pause_bot_for_takeover(db: AsyncSession, conversation_id: int) -> None:
    """A human replied in Chats -> pause the bot for this conversation (auto-resumes on expiry)."""
    from datetime import datetime, timedelta

    from sqlalchemy import update

    from app.core.outbound_config import outbound_config
    from app.models.whatsappModel import Conversation

    until = datetime.utcnow() + timedelta(hours=outbound_config.HUMAN_TAKEOVER_HOURS)
    await db.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(bot_paused_until=until)
    )


async def _handle_outside_window(
    db: AsyncSession, *, kind: str, wa_id: str, ref: Optional[str], error_message: str
) -> OutboundResult:
    """A free-form send was rejected because the 24h window is closed (code 131047)."""
    if kind == KIND_PAYMENT_CONFIRM:
        # NEVER silently drop a payment confirmation — record for staff reconciliation.
        logger.error("WhatsApp payment confirm outside 24h window (wa_id=%s ref=%s)", wa_id, ref)
        db.add(
            WebhookLog(
                event_type="whatsapp_outside_window_payment",
                x_request_id=str(ref or wa_id),
                payload={"wa_id": wa_id, "ref": ref, "code": _OUTSIDE_WINDOW_CODE},
                processed=0,
            )
        )
        await db.flush()
        return OutboundResult(SendStatus.FAILED, reason="outside_window", error_code=_OUTSIDE_WINDOW_CODE)
    if kind == KIND_CHATBOT_REPLY:
        # The bot stays quiet outside the window (today's behavior, now structured).
        return OutboundResult(SendStatus.DEFERRED, reason="outside_window", error_code=_OUTSIDE_WINDOW_CODE)
    # manual / other: surface the Cloud API error to the caller (UI) unchanged.
    return OutboundResult(SendStatus.FAILED, reason=error_message, error_code=_OUTSIDE_WINDOW_CODE)


async def _deliver(
    db: AsyncSession,
    *,
    kind: str,
    message_class: str,
    conversation_id: int,
    contact_id: int,
    wa_id: str,
    cloud_send: Callable[[], Any],
    persist: bool,
    persist_text: Optional[str] = None,
    persist_message_type: str = "text",
    persist_template_id: Optional[int] = None,
    persist_context_message_id: Optional[str] = None,
    person_id: Optional[int] = None,
    ref: Optional[str] = None,
) -> OutboundResult:
    """Coordinate + send one outbound message. Caller owns the transaction (no commit here)."""
    await _acquire_contact_lock(db, wa_id)

    # Marketing-only policy gates: consent / mid-purchase / quiet hours / daily cap.
    if message_class == CLASS_MARKETING:
        gate = await _marketing_gates(
            db, person_id=person_id, contact_id=contact_id, conversation_id=conversation_id
        )
        if gate is not None:
            return gate

    try:
        res = await cloud_send()
    except cloud.WhatsAppError as e:
        if e.code == _OUTSIDE_WINDOW_CODE:
            return await _handle_outside_window(
                db, kind=kind, wa_id=wa_id, ref=ref, error_message=e.message
            )
        if e.code in _RATE_LIMIT_CODES:
            return OutboundResult(SendStatus.DEFERRED, reason="rate_limited", error_code=e.code)
        return OutboundResult(SendStatus.FAILED, reason=e.message, error_code=e.code)

    wa_message_id = res.get("wa_message_id") if isinstance(res, dict) else None
    message = None
    if persist:
        message = await crud.insert_outbound_message(
            db,
            conversation_id=conversation_id,
            contact_id=contact_id,
            text=persist_text,
            wa_message_id=wa_message_id,
            message_type=persist_message_type,
            template_id=persist_template_id,
            context_message_id=persist_context_message_id,
            message_class=message_class,
        )
    if kind == KIND_MANUAL_HUMAN and persist_message_type != "reaction":
        # Human takeover (a real reply, not a reaction): pause the bot so it doesn't talk over staff.
        await _pause_bot_for_takeover(db, conversation_id)
    return OutboundResult(
        SendStatus.SENT, wa_message_id=wa_message_id,
        message_id=(message.id if message else None), message=message,
    )


# --- typed wrappers used by callers ------------------------------------------------
async def send_text(
    db: AsyncSession,
    *,
    kind: str,
    conversation_id: int,
    contact_id: int,
    wa_id: str,
    text: str,
    persist: bool = True,
    message_class: str = CLASS_TRANSACTIONAL,
    person_id: Optional[int] = None,
    ref: Optional[str] = None,
) -> OutboundResult:
    return await _deliver(
        db, kind=kind, message_class=message_class,
        conversation_id=conversation_id, contact_id=contact_id, wa_id=wa_id,
        cloud_send=lambda: cloud.send_text(to=wa_id, text=text),
        persist=persist, persist_text=text, persist_message_type="text",
        person_id=person_id, ref=ref,
    )


async def send_template(
    db: AsyncSession,
    *,
    kind: str,
    message_class: str,
    conversation_id: int,
    contact_id: int,
    wa_id: str,
    template_name: str,
    language_code: str,
    body_params: Optional[list] = None,
    components: Optional[list] = None,
    header_media_url: Optional[str] = None,
    header_media_id: Optional[str] = None,
    persist: bool = True,
    persist_text: Optional[str] = None,
    template_id: Optional[int] = None,
    person_id: Optional[int] = None,
) -> OutboundResult:
    return await _deliver(
        db, kind=kind, message_class=message_class,
        conversation_id=conversation_id, contact_id=contact_id, wa_id=wa_id,
        cloud_send=lambda: cloud.send_template(
            to=wa_id, template_name=template_name, language_code=language_code,
            body_params=body_params, components=components,
            header_media_url=header_media_url, header_media_id=header_media_id,
        ),
        persist=persist, persist_text=persist_text, persist_message_type="template",
        persist_template_id=template_id, person_id=person_id,
    )


async def send_reaction(
    db: AsyncSession,
    *,
    conversation_id: int,
    contact_id: int,
    wa_id: str,
    target_wa_id: str,
    emoji: str,
    persist: bool = True,
) -> OutboundResult:
    return await _deliver(
        db, kind=KIND_MANUAL_HUMAN, message_class=CLASS_TRANSACTIONAL,
        conversation_id=conversation_id, contact_id=contact_id, wa_id=wa_id,
        cloud_send=lambda: cloud.send_reaction(to=wa_id, message_id=target_wa_id, emoji=emoji),
        persist=persist, persist_text=emoji, persist_message_type="reaction",
        persist_context_message_id=target_wa_id,
    )


async def send_media(
    db: AsyncSession,
    *,
    kind: str,
    conversation_id: int,
    contact_id: int,
    wa_id: str,
    media_type: str,
    media_id: str,
    caption: Optional[str] = None,
    filename: Optional[str] = None,
    voice: bool = False,
) -> OutboundResult:
    """Send media. ``persist=False``: the caller persists the message + media row (it owns the
    richer media bookkeeping); the gateway only serializes + sends."""
    return await _deliver(
        db, kind=kind, message_class=CLASS_TRANSACTIONAL,
        conversation_id=conversation_id, contact_id=contact_id, wa_id=wa_id,
        cloud_send=lambda: cloud.send_media(
            to=wa_id,
            media_type=media_type,
            media_id=media_id,
            caption=caption,
            filename=filename,
            voice=voice,
        ),
        persist=False,
    )
