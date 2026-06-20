from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from app.crud import ownerAgentCrud
from app.graphql.schema import schema
from app.services import whatsapp_hooks


def test_owner_phone_normalization_supports_mexican_whatsapp_formats():
    assert ownerAgentCrud.normalize_owner_phone("8719708890") == "5218719708890"
    assert ownerAgentCrud.normalize_owner_phone("528719708890") == "5218719708890"
    assert ownerAgentCrud.normalize_owner_phone("5218719708890") == "5218719708890"

    keys = ownerAgentCrud.phone_match_keys("8719708890")
    assert {"8719708890", "528719708890", "5218719708890"}.issubset(keys)


@pytest.mark.asyncio
async def test_whatsapp_hook_routes_authorized_owner_to_owner_agent(monkeypatch):
    scheduled = {}

    async def fake_resolve(_db, wa_id):
        assert wa_id == "5218719708890"
        return SimpleNamespace(id=99)

    def fake_schedule_agent_reply(**kwargs):
        scheduled.update(kwargs)

    monkeypatch.setattr(ownerAgentCrud, "resolve_authorized_phone", fake_resolve)

    from app.services.owner_agent import reply_service as owner_reply_service

    monkeypatch.setattr(owner_reply_service, "schedule_agent_reply", fake_schedule_agent_reply)

    message = SimpleNamespace(
        id=10, direction="inbound", message_type="text", text_content="reporte de hoy"
    )
    contact = SimpleNamespace(id=20, wa_id="5218719708890")
    conversation = SimpleNamespace(id=30, bot_enabled=True, bot_paused_until=None)

    await whatsapp_hooks.on_inbound_message(object(), message, contact, conversation)

    assert scheduled["conversation_id"] == 30
    assert scheduled["contact_id"] == 20
    assert scheduled["authorized_phone_id"] == 99
    assert scheduled["text"] == "reporte de hoy"


@pytest.mark.asyncio
async def test_whatsapp_hook_keeps_customer_chatbot_for_non_owner(monkeypatch):
    customer_scheduled = {}

    async def fake_resolve(_db, _wa_id):
        return None

    async def fake_keyword(*_args, **_kwargs):
        return False

    fake_reply_service = SimpleNamespace(
        schedule_agent_reply=lambda **kwargs: customer_scheduled.update(kwargs)
    )

    monkeypatch.setattr(ownerAgentCrud, "resolve_authorized_phone", fake_resolve)
    monkeypatch.setitem(sys.modules, "app.services.chatbot.reply_service", fake_reply_service)
    import app.services.chatbot as chatbot_pkg

    monkeypatch.setattr(chatbot_pkg, "reply_service", fake_reply_service, raising=False)
    monkeypatch.setattr("app.services.whatsapp_optout.handle_keyword", fake_keyword)

    message = SimpleNamespace(id=11, direction="inbound", message_type="text", text_content="hola")
    contact = SimpleNamespace(id=21, wa_id="5215555555555")
    conversation = SimpleNamespace(id=31, bot_enabled=True, bot_paused_until=None)

    await whatsapp_hooks.on_inbound_message(object(), message, contact, conversation)

    assert customer_scheduled["conversation_id"] == 31
    assert customer_scheduled["contact_wa_id"] == "5215555555555"
    assert customer_scheduled["text"] == "hola"


@pytest.mark.asyncio
async def test_owner_agent_config_mutation_requires_capability():
    mutation = """
        mutation {
            saveOwnerAgentConfig(input: {enabled: true}) {
                success
                error
            }
        }
    """
    result = await schema.execute(
        mutation,
        context_value=SimpleNamespace(db=object(), user=SimpleNamespace(roles=[]), account_id=1),
    )

    assert not result.errors
    payload = result.data["saveOwnerAgentConfig"]
    assert payload["success"] is False
    assert "permiso" in payload["error"].lower()
