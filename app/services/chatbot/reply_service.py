"""Background orchestration for the WhatsApp chatbot reply.

The webhook must not block on the LLM, so ``schedule_agent_reply`` fires a background task
(mirroring ``whatsapp_media_service.schedule_download``). The task opens its OWN fresh DB
session — never the ingest pipeline's mid-transaction session — loads the conversation history,
resolves the member, runs the agent, and sends + persists the reply.

Outbound replies are persisted via ``insert_outbound_message`` exactly like the
``send_text_message`` mutation, so the bot's messages show up in the desktop Chats tab and the
realtime stream (the DB trigger fans the INSERT out to subscribers).
"""
import asyncio
import logging
from typing import List, Optional, Set

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.chatbot_env import chatbot_env
from app.core.mercadopago_config import mercadopago_config
from app.crud import chatbotConfigCrud
from app.crud import chatbotPendingCrud
from app.crud import membersCrud
from app.crud import whatsappCrud as crud
from app.db.postgresql import async_session_factory
from app.models.chatbotModel import (
    PENDING_STATUS_AWAITING_PAYMENT,
    PENDING_STATUS_PROCESSING,
)
from app.services import whatsapp_cloud_service as cloud
from app.services import whatsapp_outbound as outbound
from app.services.chatbot.agent import run_agent
from app.services.chatbot.business_context import build_business_info
from app.services.chatbot.tools import ChatbotContext, build_tools

logger = logging.getLogger(__name__)

# Keep strong references so background tasks are not garbage-collected.
_background_tasks: Set["asyncio.Task"] = set()


async def send_and_persist_reply(
    db: AsyncSession,
    *,
    conversation_id: int,
    contact_id: int,
    to_wa_id: str,
    text: str,
    kind: str = outbound.KIND_CHATBOT_REPLY,
) -> "outbound.OutboundResult":
    """Send a text reply through the unified outbound gateway and persist it.

    ``kind`` distinguishes a normal chatbot reply from a payment confirmation: the gateway never
    silently drops a payment confirm outside the 24h window (it records it for reconciliation).
    Returns the gateway result; existing callers may ignore it.
    """
    result = await outbound.send_text(
        db,
        kind=kind,
        conversation_id=conversation_id,
        contact_id=contact_id,
        wa_id=to_wa_id,
        text=text,
        persist=True,
        message_class=outbound.CLASS_TRANSACTIONAL,
    )
    await db.commit()

    # The bot just answered this conversation, so the customer's inbound messages are
    # handled — mark them read (and send a read receipt) so they don't linger as unread
    # for staff. Best-effort; never let it break the reply flow.
    if result.ok:
        try:
            _conv, latest_wa_id = await crud.mark_conversation_read(db, conversation_id)
            if latest_wa_id:
                await cloud.send_read_receipt(latest_wa_id)
        except Exception:  # noqa: BLE001
            logger.debug("post-reply mark-read failed for %s", conversation_id, exc_info=True)

    return result


async def _load_history(
    db: AsyncSession, conversation_id: int, exclude_message_id: Optional[int]
) -> List[BaseMessage]:
    """Build LangChain message history from the conversation's stored messages.

    Excludes the current inbound message (it is appended separately by the agent), reactions,
    and empty messages. Inbound -> HumanMessage, outbound -> AIMessage.
    """
    rows = await crud.get_conversation_messages(
        db, conversation_id, limit=chatbot_env.HISTORY_LIMIT
    )
    messages: List[BaseMessage] = []
    for r in rows:
        if exclude_message_id is not None and r.id == exclude_message_id:
            continue
        if r.message_type == "reaction":
            continue
        text = (r.text_content or "").strip()
        if not text:
            continue
        if r.direction == "inbound":
            messages.append(HumanMessage(content=text))
        else:
            # Label automated/campaign context so the bot can close a sale the customer is
            # responding to (and not mistake a campaign/notification for its own prior turn).
            if getattr(r, "message_class", None) == "marketing" or r.message_type == "template":
                text = f"[mensaje automático/campaña] {text}"
            messages.append(AIMessage(content=text))
    return messages


async def _build_pending_note(db: AsyncSession, conversation_id: int) -> Optional[str]:
    """Tell the agent there's a pending action so it confirms/reminds-to-pay instead of re-proposing."""
    pending = await chatbotPendingCrud.get_active_pending(db, conversation_id)
    if pending is None:
        return None
    summary = pending.summary or "una compra"
    if pending.status == PENDING_STATUS_PROCESSING:
        return (
            f"⏳ La compra «{summary}» tiene un pago que se está acreditando justo ahora. Pídele al "
            "cliente que espere unos segundos; le confirmas en cuanto se procese. NO propongas una "
            "compra nueva ni generes otro link (interrumpirías la compra en curso)."
        )
    if pending.status == PENDING_STATUS_AWAITING_PAYMENT:
        link = pending.mp_init_point or "(link no disponible)"
        return (
            f"⚠️ Hay una compra pendiente de PAGO: «{summary}». El cliente debe pagar en el link ya "
            f"enviado: {link}. Si pregunta o no lo encuentra, reenvíaselo. NO la confirmes por texto; "
            "se confirma sola al acreditarse el pago. Si quiere cancelar, usa cancel_action."
        )
    return (
        f"⚠️ Hay una acción PENDIENTE de confirmación: «{summary}». Si el cliente confirma "
        "(sí/ok/dale), llama a confirm_action AHORA; NO vuelvas a proponer ni a pedir más datos. "
        "Si rechaza o cambia de idea, usa cancel_action."
    )


async def _run_agent_reply(
    conversation_id: int,
    contact_id: int,
    contact_wa_id: str,
    message_id: int,
    text: str,
) -> None:
    """Run the agent for one inbound message and reply. Uses its own DB session."""
    if not chatbot_env.is_configured() or not chatbot_env.ENABLED:
        return
    text = (text or "").strip()
    if not text:
        return
    try:
        async with async_session_factory() as db:
            config = await chatbotConfigCrud.get_config(db)
            if config is None or not config.enabled:
                return

            member_id = await membersCrud.get_member_id_by_wa_id(db, contact_wa_id)
            business_info = await build_business_info(db, config)
            history = await _load_history(db, conversation_id, exclude_message_id=message_id)
            require_mp = bool(config.require_mp_payment) and mercadopago_config.is_configured()
            pending_note = await _build_pending_note(db, conversation_id)

            ctx = ChatbotContext(
                db=db,
                conversation_id=conversation_id,
                member_id=member_id,
                wa_id=contact_wa_id,
                require_mp_payment=require_mp,
            )
            tools = build_tools(ctx)
            reply = await run_agent(
                config, tools, business_info, member_id, history, text, pending_note=pending_note
            )
            if not reply:
                return

            # The agent took time to run; re-check the bot wasn't disabled/paused meanwhile (STOP,
            # human takeover, or robot button off) so it doesn't talk over a human or reply right
            # after the customer asked to stop.
            from datetime import datetime

            conv = await crud.get_conversation(db, conversation_id)
            if conv is not None and (
                conv.bot_enabled is False
                or (conv.bot_paused_until is not None and conv.bot_paused_until > datetime.utcnow())
            ):
                logger.info(
                    "Chatbot reply suppressed: bot disabled/paused for conversation %s",
                    conversation_id,
                )
                return

            await send_and_persist_reply(
                db,
                conversation_id=conversation_id,
                contact_id=contact_id,
                to_wa_id=contact_wa_id,
                text=reply,
            )
    except cloud.WhatsAppError as e:
        # e.g. 131047 outside the 24h window — log, don't crash the loop.
        logger.warning("Chatbot reply send failed (%s): %s", e.code, e.message)
    except Exception:  # noqa: BLE001
        logger.exception("Chatbot agent reply failed for conversation %s", conversation_id)


def schedule_agent_reply(
    conversation_id: int,
    contact_id: int,
    contact_wa_id: str,
    message_id: int,
    text: str,
) -> None:
    """Fire-and-forget the agent reply on the running event loop (non-blocking webhook)."""
    try:
        task = asyncio.create_task(
            _run_agent_reply(conversation_id, contact_id, contact_wa_id, message_id, text)
        )
    except RuntimeError:
        # No running loop (e.g. called from a sync context outside the app) — skip.
        logger.warning("schedule_agent_reply called without a running event loop; skipped")
        return
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _run_text_send(
    conversation_id: int, contact_id: int, to_wa_id: str, text: str, kind: str
) -> None:
    """Background a single transactional text send in its own session (e.g. opt-out confirmation)."""
    try:
        async with async_session_factory() as db:
            await send_and_persist_reply(
                db,
                conversation_id=conversation_id,
                contact_id=contact_id,
                to_wa_id=to_wa_id,
                text=text,
                kind=kind,
            )
    except Exception:  # noqa: BLE001
        logger.exception("background text send failed for conversation %s", conversation_id)


def schedule_text_send(
    conversation_id: int,
    contact_id: int,
    to_wa_id: str,
    text: str,
    kind: str = outbound.KIND_CHATBOT_REPLY,
) -> None:
    """Fire-and-forget a transactional text send so the webhook/ingest path stays non-blocking."""
    try:
        task = asyncio.create_task(
            _run_text_send(conversation_id, contact_id, to_wa_id, text, kind)
        )
    except RuntimeError:
        logger.warning("schedule_text_send called without a running event loop; skipped")
        return
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
