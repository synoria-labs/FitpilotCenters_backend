"""Exercise persisted attachments through ingestion, both reply pipelines and follow-ups."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.db.postgresql import async_session_factory
from app.models import Contact, Conversation, Media, Message
from app.services import whatsapp_ingest_service as ingest
from app.services.chatbot import image_context as images
from app.services.chatbot import reply_service as customer
from app.services.owner_agent import agent as owner_agent
from app.services.owner_agent import reply_service as owner
from tests.test_chatbot_images import picture


async def attachment(db, tmp_path):
    token = uuid4().hex
    contact = Contact(wa_id=token[:20], phone_number=token[:20])
    db.add(contact)
    await db.flush()
    conversation = Conversation(contact_id=contact.id)
    db.add(conversation)
    await db.flush()
    message = Message(contact_id=contact.id, conversation_id=conversation.id, direction="inbound",
                      message_type="image", text_content=None, timestamp=datetime.utcnow(), wa_message_id=token)
    db.add(message)
    await db.flush()
    path = tmp_path / f"{token}.png"
    path.write_bytes(picture())
    db.add(Media(message_id=message.id, media_type="image", mime_type="image/png", downloaded=1,
                 media_url=f"/uploads/whatsapp/{path.name}", file_size=path.stat().st_size))
    await db.flush()
    return contact.id, conversation.id, message.id, token


@pytest.mark.parametrize("scope", ["customer", "owner"])
@pytest.mark.parametrize("model_fails", [False, True])
@pytest.mark.parametrize("followup", [False, True])
async def test_persisted_image_reaches_both_agents_and_model_failure_gets_a_reply(
    db, tmp_path, monkeypatch, scope, model_fails, followup,
):
    monkeypatch.setattr(images, "UPLOAD_DIR", tmp_path)
    contact_id, conversation_id, image_id, wa_id = await attachment(db, tmp_path)
    message_id = image_id
    if followup:
        msg = Message(contact_id=contact_id, conversation_id=conversation_id, direction="inbound",
                      message_type="text", text_content="Me aparece un error",
                      timestamp=datetime.utcnow() + timedelta(seconds=1), wa_message_id=uuid4().hex)
        db.add(msg)
        await db.flush()
        message_id = msg.id
    received = []

    @asynccontextmanager
    async def session():
        yield db

    target = customer if scope == "customer" else owner
    monkeypatch.setattr(target, "async_session_factory", session)
    sent = AsyncMock()
    monkeypatch.setattr(target, "send_and_persist_reply", sent)
    expected = "La captura muestra un error al abrir el pago. Esto no confirma si hubo un cobro."

    async def run(*args, **kwargs):
        content = kwargs["image_content"]
        tools = args[1] if scope == "customer" else kwargs["tools"]
        if not followup:
            # Even model instructions hidden in the pixels cannot invoke an action tool.
            assert all(t.name == "respond_to_customer" or t.name.startswith(("get_", "list_", "check_"))
                       for t in tools)
            assert not any(t.name == "confirm_action" for t in tools)
        if followup:
            assert content is None
            history = args[4] if scope == "customer" else kwargs["history"]
            content = history[-1].content if scope == "customer" else history[-1][1]
        assert content[1]["type"] == "image" and content[1]["source"]["data"]
        assert images.IMAGE_PLACEHOLDER in content[0]["text"]
        received.append(content)
        if model_fails:
            raise RuntimeError("vision provider unavailable")
        if scope == "customer":
            respond = next(t for t in args[1] if t.name == "respond_to_customer")
            await respond.ainvoke({"kind": "information", "text": expected})
        return expected

    if scope == "customer":
        from app.services.chatbot.prompt_optimizer import _fallback_config
        config = _fallback_config()
        config.enabled = True
        config.require_mp_payment = False
        monkeypatch.setattr(customer.chatbot_env, "is_configured", lambda: True)
        monkeypatch.setattr(customer.chatbot_env, "ENABLED", True)
        monkeypatch.setattr(customer.chatbotConfigCrud, "get_config", AsyncMock(return_value=config))
        monkeypatch.setattr(customer.membersCrud, "get_member_id_by_wa_id", AsyncMock(return_value=None))
        monkeypatch.setattr(customer, "build_business_info", AsyncMock(return_value="Estudio"))
        monkeypatch.setattr(customer, "run_agent", run)
        await target._run_agent_reply(conversation_id, contact_id, wa_id, message_id,
                                      "Me aparece un error" if followup else "",
                                      message_type="text" if followup else "image")
    else:
        config = await owner.ownerAgentCrud.get_config(db)
        config.enabled = True
        monkeypatch.setattr(owner.owner_agent_env, "SERVER_ENABLED", True)
        monkeypatch.setattr(owner.owner_agent_env, "ANTHROPIC_API_KEY", "test-only")
        monkeypatch.setattr(owner.ownerAgentCrud, "get_config", AsyncMock(return_value=config))
        monkeypatch.setattr(owner_agent, "run_agent", run)
        await target._run_agent_reply(conversation_id=conversation_id, contact_id=contact_id,
                                      contact_wa_id=wa_id, authorized_phone_id=1, message_id=message_id,
                                      text="Me aparece un error" if followup else "",
                                      message_type="text" if followup else "image")
    assert len(received) == 1
    sent.assert_awaited_once()
    assert sent.call_args.kwargs["text"] == (images.IMAGE_FAILURE_REPLY if model_fails else expected)


async def test_quote_outside_history_window_is_loaded_only_from_its_conversation(db, tmp_path):
    contact_id, conversation_id, image_id, wa_id = await attachment(db, tmp_path)
    _, _, _, foreign_wa_id = await attachment(db, tmp_path)
    msg = Message(contact_id=contact_id, conversation_id=conversation_id, direction="inbound",
                  message_type="text", text_content="¿Y esta captura?", context_message_id=wa_id,
                  timestamp=datetime.utcnow() + timedelta(seconds=10), wa_message_id=uuid4().hex)
    db.add(msg)
    await db.flush()
    rows, chosen = await images.history_rows(db, conversation_id=conversation_id, limit=1,
                                             exclude_message_id=msg.id, context_message_id=wa_id)
    assert [r.id for r in rows] == [image_id] and chosen == {image_id}
    rows, chosen = await images.history_rows(db, conversation_id=conversation_id, limit=1,
                                             exclude_message_id=msg.id, context_message_id=foreign_wa_id)
    assert rows == [] and chosen == set()


async def test_ingest_commits_message_and_media_before_hook_and_deduplicates_delivery(monkeypatch):
    token = uuid4().hex
    wa_id = token[:20]
    hooks, downloads = [], []

    async def hook(db, message, contact, conversation):
        # A separate connection, like the background agent, must already see the attachment.
        async with async_session_factory() as other:
            saved = await other.get(Message, message.id)
            media = await other.scalar(select(Media).where(Media.message_id == message.id))
            assert saved is not None and media.cloud_media_id == "test-cloud-image"
            assert saved.contact_id == contact.id and saved.conversation_id == conversation.id
        hooks.append(message.id)

    monkeypatch.setattr(ingest, "on_inbound_message", hook)
    monkeypatch.setattr(ingest.media_service, "schedule_download", lambda *args: downloads.append(args))
    payload = {"entry": [{"changes": [{"value": {"messages": [{
        "id": token, "from": wa_id, "type": "image", "timestamp": str(int(datetime.now().timestamp())),
        "image": {"id": "test-cloud-image", "mime_type": "image/png", "caption": "Me aparece un error"},
    }]}}]}]}
    try:
        identifiers = await ingest.process(payload)
        assert len(identifiers) == 1 and hooks == identifiers and len(downloads) == 1
        assert await ingest.process(payload) == []
        assert hooks == identifiers and len(downloads) == 1
    finally:
        async with async_session_factory() as cleanup:
            contact_ids = select(Contact.id).where(Contact.wa_id == wa_id)
            message_ids = select(Message.id).where(Message.contact_id.in_(contact_ids))
            await cleanup.execute(delete(Media).where(Media.message_id.in_(message_ids)))
            await cleanup.execute(delete(Message).where(Message.contact_id.in_(contact_ids)))
            await cleanup.execute(delete(Conversation).where(Conversation.contact_id.in_(contact_ids)))
            await cleanup.execute(delete(Contact).where(Contact.wa_id == wa_id))
            await cleanup.commit()
