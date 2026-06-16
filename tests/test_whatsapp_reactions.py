"""Tests for WhatsApp message reactions (inbound capture + outbound send).

Inbound: the webhook ingest must store the reaction emoji in ``text_content`` and the
reacted-to message id in ``context_message_id`` (no dedicated columns / migration).
Outbound: the ``sendReaction`` mutation must call the Cloud API and persist an outbound
reaction row carrying the same emoji + target.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.graphql.whatsapp.mutations import WhatsAppChatMutation
from app.models import Message
from app.services.whatsapp_ingest_service import _process_message

WA_ID = "5218710000001"
TARGET_WAMID = "wamid.target123"


def _reaction_msg(wa_message_id: str, target: str, emoji: str) -> dict:
    return {
        "from": WA_ID,
        "id": wa_message_id,
        "timestamp": "1700000000",
        "type": "reaction",
        "reaction": {"message_id": target, "emoji": emoji},
    }


# ---------------------------------------------------------------------------
# Inbound ingest
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ingest_reaction_stores_emoji_and_target(db):
    msg_id = await _process_message(db, _reaction_msg("wamid.r1", TARGET_WAMID, "👍"), {}, [])
    await db.flush()

    row = (await db.execute(select(Message).where(Message.id == msg_id))).scalars().first()
    assert row is not None
    assert row.message_type == "reaction"
    assert row.direction == "inbound"
    assert row.text_content == "👍"
    assert row.context_message_id == TARGET_WAMID


@pytest.mark.asyncio
async def test_ingest_reaction_removal_stores_empty_emoji(db):
    # WhatsApp sends an empty emoji when the reaction is removed.
    msg_id = await _process_message(db, _reaction_msg("wamid.r2", TARGET_WAMID, ""), {}, [])
    await db.flush()

    row = (await db.execute(select(Message).where(Message.id == msg_id))).scalars().first()
    assert row is not None
    assert row.message_type == "reaction"
    assert row.text_content == ""  # not coerced to NULL — the frontend reads it as "remove"
    assert row.context_message_id == TARGET_WAMID


# ---------------------------------------------------------------------------
# Outbound send mutation
# ---------------------------------------------------------------------------
class _FakeDb:
    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def execute(self, *args, **kwargs):
        # The outbound gateway issues a pg_advisory_xact_lock SELECT; the result is ignored.
        return None


def _reaction_message(emoji: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        conversation_id=2,
        contact_id=3,
        direction="outbound",
        message_type="reaction",
        text_content=emoji,
        timestamp=datetime.now(timezone.utc),
        wa_message_id="wamid.react.out",
        context_message_id=TARGET_WAMID,
    )


async def _run_send_reaction(monkeypatch, emoji: str) -> tuple:
    cloud_kwargs: dict = {}
    insert_kwargs: dict = {}

    async def fake_get_conversation(_db, _cid):
        return SimpleNamespace(id=2, contact=SimpleNamespace(id=3, wa_id="5215555555555"))

    async def fake_send_reaction(**kwargs):
        cloud_kwargs.update(kwargs)
        return {"wa_message_id": "wamid.react.out"}

    async def fake_insert_outbound_message(*_args, **kwargs):
        insert_kwargs.update(kwargs)
        return _reaction_message(emoji)

    monkeypatch.setattr(
        "app.graphql.whatsapp.mutations.crud.get_conversation", fake_get_conversation
    )
    monkeypatch.setattr(
        "app.graphql.whatsapp.mutations.cloud.send_reaction", fake_send_reaction
    )
    monkeypatch.setattr(
        "app.graphql.whatsapp.mutations.crud.insert_outbound_message",
        fake_insert_outbound_message,
    )

    info = SimpleNamespace(context=SimpleNamespace(db=_FakeDb()))
    input_data = SimpleNamespace(
        message_id=TARGET_WAMID, emoji=emoji, conversation_id=2, wa_id=None
    )
    result = await WhatsAppChatMutation().send_reaction(info, input_data)
    return result, cloud_kwargs, insert_kwargs


@pytest.mark.asyncio
async def test_send_reaction_persists_outbound_reaction(monkeypatch):
    result, cloud_kwargs, insert_kwargs = await _run_send_reaction(monkeypatch, "👍")

    assert result.success is True
    assert result.message.message_type == "reaction"
    assert result.message.text_content == "👍"
    assert result.message.context_message_id == TARGET_WAMID

    # Cloud API received the right reaction payload.
    assert cloud_kwargs["message_id"] == TARGET_WAMID
    assert cloud_kwargs["emoji"] == "👍"
    assert cloud_kwargs["to"] == "5215555555555"

    # Persisted as an outbound reaction with the target reference.
    assert insert_kwargs["message_type"] == "reaction"
    assert insert_kwargs["context_message_id"] == TARGET_WAMID
    assert insert_kwargs["text"] == "👍"


@pytest.mark.asyncio
async def test_send_reaction_removal_sends_empty_emoji(monkeypatch):
    result, cloud_kwargs, insert_kwargs = await _run_send_reaction(monkeypatch, "")

    assert result.success is True
    assert cloud_kwargs["emoji"] == ""
    assert insert_kwargs["text"] == ""
    assert insert_kwargs["message_type"] == "reaction"


@pytest.mark.asyncio
async def test_send_reaction_requires_target(monkeypatch):
    info = SimpleNamespace(context=SimpleNamespace(db=_FakeDb()))
    input_data = SimpleNamespace(message_id="", emoji="👍", conversation_id=2, wa_id=None)
    result = await WhatsAppChatMutation().send_reaction(info, input_data)
    assert result.success is False
