"""Background orchestration for the owner/admin WhatsApp agent."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Set

from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import ownerAgentCrud
from app.crud import whatsappCrud as whatsapp_crud
from app.db.postgresql import async_session_factory
from app.models.ownerAgentModel import OWNER_PENDING_STATUS_PENDING
from app.services import whatsapp_cloud_service as cloud
from app.services import whatsapp_outbound as outbound
from app.services.owner_agent.env import owner_agent_env

logger = logging.getLogger(__name__)

_background_tasks: Set["asyncio.Task"] = set()


async def send_and_persist_reply(
    db: AsyncSession,
    *,
    conversation_id: int,
    contact_id: int,
    to_wa_id: str,
    text: str,
) -> outbound.OutboundResult:
    result = await outbound.send_text(
        db,
        kind=outbound.KIND_OWNER_AGENT_REPLY,
        conversation_id=conversation_id,
        contact_id=contact_id,
        wa_id=to_wa_id,
        text=text,
        persist=True,
        message_class=outbound.CLASS_TRANSACTIONAL,
    )
    await db.commit()
    if result.ok:
        try:
            _conv, latest_wa_id = await whatsapp_crud.mark_conversation_read(db, conversation_id)
            if latest_wa_id:
                await cloud.send_read_receipt(latest_wa_id)
        except Exception:  # noqa: BLE001
            logger.debug("owner-agent post-reply mark-read failed", exc_info=True)
    return result


async def _load_history(
    db: AsyncSession, conversation_id: int, exclude_message_id: Optional[int], limit: int
) -> list[tuple[str, str]]:
    rows = await whatsapp_crud.get_conversation_messages(
        db, conversation_id, limit=max(1, min(int(limit or 30), 100))
    )
    history: list[tuple[str, str]] = []
    for row in rows:
        if exclude_message_id is not None and row.id == exclude_message_id:
            continue
        if row.message_type == "reaction":
            continue
        text = (row.text_content or "").strip()
        if not text:
            continue
        history.append((row.direction, text))
    return history


async def _pending_note(db: AsyncSession, conversation_id: int) -> Optional[str]:
    pending = await ownerAgentCrud.get_pending_action(db, conversation_id)
    if pending is None:
        return None
    summary = pending.summary or "una accion administrativa"
    if pending.status == OWNER_PENDING_STATUS_PENDING:
        return (
            f"Hay una accion pendiente: {summary}. Si el usuario confirma con si/ok/dale, "
            "llama a confirm_action. Si responde no/cancelar, llama a cancel_action. "
            "No vuelvas a proponer la misma accion."
        )
    return None


async def _run_agent_reply(
    *,
    conversation_id: int,
    contact_id: int,
    contact_wa_id: str,
    authorized_phone_id: int,
    message_id: int,
    text: str,
) -> None:
    try:
        text = (text or "").strip()
        if not text:
            return

        async with async_session_factory() as db:
            config = await ownerAgentCrud.get_config(db)
            if not owner_agent_env.SERVER_ENABLED or not config.enabled:
                logger.info(
                    "Owner agent suppressed: server=%s db=%s",
                    owner_agent_env.SERVER_ENABLED,
                    config.enabled,
                )
                return
            if not owner_agent_env.ANTHROPIC_API_KEY:
                logger.warning("Owner agent enabled without ANTHROPIC_API_KEY")
                return

            from app.services.owner_agent.agent import run_agent
            from app.services.owner_agent.tools import OwnerAgentContext, build_tools

            history = await _load_history(db, conversation_id, message_id, config.history_limit)
            pending_note = await _pending_note(db, conversation_id)
            ctx = OwnerAgentContext(
                db=db,
                conversation_id=conversation_id,
                authorized_phone_id=authorized_phone_id,
                message_id=message_id,
                require_confirmation=bool(config.require_confirmation),
            )
            tools = build_tools(ctx)
            reply = await run_agent(
                config=config,
                tools=tools,
                history=history,
                user_text=text,
                pending_note=pending_note,
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
    except cloud.WhatsAppError as exc:
        logger.warning("Owner agent WhatsApp send failed (%s): %s", exc.code, exc.message)
    except Exception:  # noqa: BLE001
        logger.exception("Owner agent reply failed for conversation %s", conversation_id)


def schedule_agent_reply(
    *,
    conversation_id: int,
    contact_id: int,
    contact_wa_id: str,
    authorized_phone_id: int,
    message_id: int,
    text: str,
) -> None:
    try:
        task = asyncio.create_task(
            _run_agent_reply(
                conversation_id=conversation_id,
                contact_id=contact_id,
                contact_wa_id=contact_wa_id,
                authorized_phone_id=authorized_phone_id,
                message_id=message_id,
                text=text,
            )
        )
    except RuntimeError:
        logger.warning("owner schedule_agent_reply called without running loop; skipped")
        return
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
