"""Resolving a button tap (or a quoted reply) back to the campaign/template that sent it.

Real bug this covers: a customer tapping a template QUICK_REPLY button arrives as
``type: "button"`` (or ``"interactive"`` for a generic reply-button/list message). Before this,
``whatsapp_ingest_service`` had no branch for either type, so ``text_content`` stayed ``None``,
and ``whatsapp_hooks`` gated the chatbot on ``message_type == "text"``, so the tap never even
reached the agent. The desktop chat showed the literal placeholder ``"[button]"`` instead.
"""
from __future__ import annotations

from typing import Tuple

import pytest
from sqlalchemy import select

from app.crud import campaignsCrud as crud
from app.crud.chatbotConfigCrud import ChatbotConfigData
from app.models import Campaign, Message, WhatsAppTemplate
from app.services.chatbot.agent import build_system_prompt
from app.services.chatbot.reply_service import _build_campaign_context_note
from app.services.whatsapp_ingest_service import _process_message

WA_ID = "5218710000002"


def _button_msg(wa_message_id: str, context_id: str, button_text: str) -> dict:
    return {
        "from": WA_ID,
        "id": wa_message_id,
        "timestamp": "1700000000",
        "type": "button",
        "context": {"id": context_id},
        "button": {"text": button_text, "payload": button_text},
    }


def _interactive_msg(wa_message_id: str, context_id: str, title: str) -> dict:
    return {
        "from": WA_ID,
        "id": wa_message_id,
        "timestamp": "1700000000",
        "type": "interactive",
        "context": {"id": context_id},
        "interactive": {
            "type": "button_reply",
            "button_reply": {"id": "btn-1", "title": title},
        },
    }


# ---------------------------------------------------------------------------
# Ingestion: button/interactive text + context capture
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ingest_button_tap_stores_text_and_context(db):
    msg_id = await _process_message(
        db, _button_msg("wamid.btn1", "wamid.template_out_1", "Quiero reservar"), {}, []
    )
    await db.flush()

    row = (await db.execute(select(Message).where(Message.id == msg_id))).scalars().first()
    assert row is not None
    assert row.message_type == "button"
    assert row.direction == "inbound"
    assert row.text_content == "Quiero reservar"
    assert row.context_message_id == "wamid.template_out_1"


@pytest.mark.asyncio
async def test_ingest_interactive_reply_stores_title_and_context(db):
    msg_id = await _process_message(
        db, _interactive_msg("wamid.int1", "wamid.template_out_2", "Ver horarios"), {}, []
    )
    await db.flush()

    row = (await db.execute(select(Message).where(Message.id == msg_id))).scalars().first()
    assert row is not None
    assert row.message_type == "interactive"
    assert row.text_content == "Ver horarios"
    assert row.context_message_id == "wamid.template_out_2"


@pytest.mark.asyncio
async def test_ingest_button_without_context_leaves_it_none(db):
    # Defensive: some client versions omit ``context`` entirely.
    msg = _button_msg("wamid.btn2", "unused", "Quiero reservar")
    msg["context"] = {}
    msg_id = await _process_message(db, msg, {}, [])
    await db.flush()

    row = (await db.execute(select(Message).where(Message.id == msg_id))).scalars().first()
    assert row is not None
    assert row.context_message_id is None


# ---------------------------------------------------------------------------
# Campaign/template context resolution for the AI agent
# ---------------------------------------------------------------------------
async def _make_campaign_with_template(
    db, *, campaign_name: str, template_name: str
) -> Tuple[Campaign, WhatsAppTemplate]:
    template = WhatsAppTemplate(
        template_name=template_name,
        template_namespace="ns",
        template_language="es_MX",
        template_status="APPROVED",
        category="MARKETING",
        components=[{"type": "BODY", "text": "Hola {{1}}"}],
    )
    db.add(template)
    await db.flush()

    campaign = await crud.create_campaign(
        db, name=campaign_name, objective="win_back", commit=False, template_id=template.id
    )
    return campaign, template


async def _sent_recipient(db, campaign: Campaign, *, dedup_suffix: str, wa_message_id: str):
    recipient_id = await crud.insert_recipient(
        db,
        campaign_id=campaign.id,
        dedup_key=f"campaign:{campaign.id}:{dedup_suffix}",
        phone_e164=WA_ID,
        wa_id=WA_ID,
    )
    recipient = await crud.get_recipient_model(db, recipient_id)
    await crud.mark_recipient_sent(
        db, recipient, wa_message_id=wa_message_id, message_id=None, commit=False
    )
    return recipient


@pytest.mark.asyncio
async def test_campaign_note_for_button_tap(db):
    campaign, _template = await _make_campaign_with_template(
        db, campaign_name="recaptura-9am-test", template_name="recaptura_9_00_am"
    )
    await _sent_recipient(db, campaign, dedup_suffix="r1", wa_message_id="wamid.template_out_1")

    note = await _build_campaign_context_note(
        db, "wamid.template_out_1", "button", "Quiero reservar"
    )

    assert note is not None
    assert "Quiero reservar" in note
    assert "recaptura-9am-test" in note
    assert "recaptura_9_00_am" in note


@pytest.mark.asyncio
async def test_campaign_note_for_quoted_text_reply(db):
    """WhatsApp also fills ``context.id`` when the customer swipe-replies to plain text —
    not only for button taps — so the same resolution must work for message_type='text'."""
    campaign, _template = await _make_campaign_with_template(
        db, campaign_name="regresa-test", template_name="regresa"
    )
    await _sent_recipient(db, campaign, dedup_suffix="r2", wa_message_id="wamid.template_out_2")

    note = await _build_campaign_context_note(
        db, "wamid.template_out_2", "text", "sí, cuéntame más"
    )

    assert note is not None
    assert "citando" in note
    assert "regresa-test" in note
    assert "regresa" in note.split("plantilla")[-1]  # names the template, not just the campaign


@pytest.mark.asyncio
async def test_campaign_note_is_none_without_context(db):
    assert await _build_campaign_context_note(db, None, "text", "hola") is None


@pytest.mark.asyncio
async def test_campaign_note_is_none_when_context_matches_nothing(db):
    assert await _build_campaign_context_note(db, "wamid.unknown", "button", "Sí") is None


# ---------------------------------------------------------------------------
# System prompt assembly: the note must actually reach the agent
# ---------------------------------------------------------------------------
def _minimal_config(**overrides) -> ChatbotConfigData:
    base = dict(
        id=1, enabled=True, require_confirmation=True, require_mp_payment=False,
        model="claude-sonnet-4-6", system_prompt="Eres el asistente de FitPilot.",
        business_name="FitPilot", address=None, operating_hours=None, phone=None,
        policies=None, tone=None, extra_info=None, created_at=None, updated_at=None,
    )
    base.update(overrides)
    return ChatbotConfigData(**base)


def test_build_system_prompt_includes_campaign_note_when_present():
    prompt = build_system_prompt(
        _minimal_config(), business_info="", member_id=None,
        campaign_note='📣 El cliente tocó el botón "Quiero reservar" de la campaña "X".',
    )
    assert 'El cliente tocó el botón "Quiero reservar"' in prompt


def test_build_system_prompt_omits_campaign_note_when_absent():
    prompt = build_system_prompt(_minimal_config(), business_info="", member_id=None)
    assert "📣" not in prompt
