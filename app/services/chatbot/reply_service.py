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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.chatbot_env import chatbot_env
from app.core.mercadopago_config import mercadopago_config
from app.crud import chatbotConfigCrud
from app.crud import chatbotPendingCrud
from app.crud import membersCrud
from app.crud import whatsappCrud as crud
from app.crud.chatbotConfigCrud import ChatbotConfigData
from app.db.postgresql import async_session_factory
from app.models import Venue
from app.models.chatbotModel import PENDING_STATUS_AWAITING_PAYMENT
from app.services import whatsapp_cloud_service as cloud
from app.services.chatbot.agent import run_agent
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
) -> None:
    """Send a text reply via the Cloud API and persist the outbound Message row."""
    result = await cloud.send_text(to=to_wa_id, text=text)
    await crud.insert_outbound_message(
        db,
        conversation_id=conversation_id,
        contact_id=contact_id,
        text=text,
        wa_message_id=result.get("wa_message_id"),
    )
    await db.commit()


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
            messages.append(AIMessage(content=text))
    return messages


async def _build_business_info(db: AsyncSession, config: ChatbotConfigData) -> str:
    """Render the configured business info (address falls back to the first Venue)."""
    lines: List[str] = []
    if config.business_name:
        lines.append(f"Nombre: {config.business_name}")
    address = config.address
    if not address:
        venue = (await db.execute(select(Venue).order_by(Venue.id).limit(1))).scalars().first()
        address = venue.address if venue else None
    if address:
        lines.append(f"Dirección: {address}")
    if config.operating_hours:
        lines.append(f"Horarios: {config.operating_hours}")
    if config.phone:
        lines.append(f"Teléfono: {config.phone}")
    if config.policies:
        lines.append(f"Políticas: {config.policies}")
    if config.extra_info:
        lines.append(config.extra_info)
    return "\n".join(lines)


async def _build_pending_note(db: AsyncSession, conversation_id: int) -> Optional[str]:
    """Tell the agent there's a pending action so it confirms/reminds-to-pay instead of re-proposing."""
    pending = await chatbotPendingCrud.get_active_pending(db, conversation_id)
    if pending is None:
        return None
    summary = pending.summary or "una compra"
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
            business_info = await _build_business_info(db, config)
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
