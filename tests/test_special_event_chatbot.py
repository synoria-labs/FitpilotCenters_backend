"""WhatsApp assistant and special events: campaign buttons, tools and verified statuses.

No LLM is called: the tests exercise the context notes and the tools the agent would use.
Mercado Pago and WhatsApp sends are faked.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.core.mercadopago_config import MercadoPagoConfig
from app.crud.special_events import events as events_crud
from app.crud.special_events import takeover as takeover_crud
from app.models import Campaign, CampaignRecipient, ChatbotPendingAction, MembershipPlan
from app.models.specialEventModel import SpecialEventOrder
from app.services import mercadopago_service, special_event_payments as sep
from app.services.chatbot import reply_service
from app.services.chatbot.agent import build_system_prompt
from app.services.chatbot.tools import ChatbotContext, build_tools
from tests.special_event_factories import (
    make_event,
    make_person,
    regular_class_scenario,
    same_session_factory,
)

WA_ID = "5218711234567"
CONVERSATION = 4242


@pytest.fixture
def fake_mp(monkeypatch, db):
    calls = []

    async def create_preference(**kwargs):
        calls.append(kwargs)
        return {"preference_id": "pref-x", "init_point": "https://mp.test/checkout/x"}

    monkeypatch.setattr(MercadoPagoConfig, "ACCESS_TOKEN", "APP_USR-test-token")
    monkeypatch.setattr(mercadopago_service, "create_preference", create_preference)
    monkeypatch.setattr(sep, "async_session_factory", same_session_factory(db))

    async def no_send(*_args, **_kwargs):
        return None

    monkeypatch.setattr(sep, "_send", no_send)
    return calls


async def _published(db):
    sc = await regular_class_scenario(db, booked=1)
    event = await make_event(db, sc)
    preview = await takeover_crud.preview_takeover(db, event)
    await takeover_crud.apply_takeover(db, event, actor_account_id=None, session_ids=preview.pending_session_ids)
    await events_crud.publish_event(db, event, actor_account_id=None)
    return sc, event


async def _campaign_sent(db, event, *, wa_message_id="wamid.HALLOWEEN", sent_days_ago=1):
    campaign = Campaign(name="Halloween 2026", objective="broadcast", status="completed",
                        special_event_id=event.id)
    db.add(campaign)
    recipient = await make_person(db, "Destinataria", "8711234567")
    await db.flush()
    db.add(
        CampaignRecipient(
            campaign_id=campaign.id,
            person_id=recipient.id,
            dedup_key=f"{campaign.id}:{wa_message_id}",
            status="sent",
            wa_id=WA_ID,
            wa_message_id=wa_message_id,
            sent_at=datetime.now(timezone.utc) - timedelta(days=sent_days_ago),
        )
    )
    await db.flush()
    return campaign


def _tool(tools, name):
    return next(t for t in tools if t.name == name).coroutine


async def test_quiero_reservar_button_resolves_the_linked_event(db):
    _sc, event = await _published(db)
    await _campaign_sent(db, event)
    note = await reply_service._build_special_event_note(
        db, CONVERSATION, WA_ID, "wamid.HALLOWEEN", "button", "Quiero reservar"
    )
    assert f"#{event.id}" in note and "$500.00 MXN" in note
    assert f"start_event_purchase(event_id={event.id})" in note
    assert "No ofrezcas planes ni pase diario" in note


async def test_mas_informacion_button_asks_for_event_details(db):
    _sc, event = await _published(db)
    await _campaign_sent(db, event)
    note = await reply_service._build_special_event_note(
        db, CONVERSATION, WA_ID, "wamid.HALLOWEEN", "button", "Más información"
    )
    assert f"get_special_event({event.id})" in note and "ofrécele reservar" in note


async def test_event_stays_in_context_after_the_button_turn(db):
    _sc, event = await _published(db)
    await _campaign_sent(db, event)
    # The customer now just types the participant's name: no context id, plain text.
    note = await reply_service._build_special_event_note(db, CONVERSATION, WA_ID, None, "text", "Ana López")
    assert f"#{event.id}" in note and "una campaña reciente" in note


async def test_unpublished_or_unlinked_events_are_not_suggested(db):
    sc = await regular_class_scenario(db, booked=0)
    draft = await make_event(db, sc)  # draft: invisible to the assistant
    await _campaign_sent(db, draft)
    note = await reply_service._build_special_event_note(
        db, CONVERSATION, WA_ID, "wamid.HALLOWEEN", "button", "Quiero reservar"
    )
    assert note is None
    tools = build_tools(ChatbotContext(db=db, conversation_id=CONVERSATION, member_id=None, wa_id=WA_ID))
    assert "No hay eventos especiales" in await _tool(tools, "list_special_events")()
    assert "No encontré ese evento" in await _tool(tools, "get_special_event")(event_id=draft.id)


async def test_purchase_tool_gives_link_and_status_is_verified(db, fake_mp):
    _sc, event = await _published(db)
    tools = build_tools(
        ChatbotContext(db=db, conversation_id=CONVERSATION, member_id=None, wa_id=WA_ID,
                       require_mp_payment=True, mp_required=True)
    )
    details = await _tool(tools, "get_special_event")(event_id=event.id)
    assert "Mesa de snacks" in details and "no usa ni modifica la membresía" in details

    missing = await _tool(tools, "start_event_purchase")(event_id=event.id)
    assert "nombre completo" in missing

    started = await _tool(tools, "start_event_purchase")(event_id=event.id, participant_name="Ana López")
    assert "https://mp.test/checkout/x" in started and "NO está confirmada" in started
    status = await _tool(tools, "check_event_order_status")()
    assert "pendiente de pago" in status and "PAGADA" not in status

    order = (await db.execute(select(SpecialEventOrder))).scalars().one()
    payment = {
        "id": 31337, "status": "approved", "external_reference": order.external_reference,
        "transaction_amount": 500.0, "currency_id": "MXN",
    }
    await sep.process_payment_notification(31337, payment)
    status = await _tool(tools, "check_event_order_status")()
    assert "PAGADA y confirmada" in status

    note = await reply_service._build_special_event_note(db, CONVERSATION, WA_ID, None, "text", "hola")
    assert "PAGADA" in note


async def test_cancel_tool_only_cancels_unpaid_orders(db, fake_mp):
    _sc, event = await _published(db)
    tools = build_tools(
        ChatbotContext(db=db, conversation_id=CONVERSATION, member_id=None, wa_id=WA_ID,
                       require_mp_payment=True, mp_required=True)
    )
    await _tool(tools, "start_event_purchase")(event_id=event.id, participant_name="Ana López")
    result = await _tool(tools, "cancel_event_order")()
    assert "cancelé 1" in result
    assert "No hay compras" in await _tool(tools, "cancel_event_order")()


async def test_purchases_are_refused_when_mercadopago_is_required_but_missing(db, monkeypatch):
    monkeypatch.setattr(MercadoPagoConfig, "ACCESS_TOKEN", "")
    sc = await regular_class_scenario(db, booked=0)
    plan = MembershipPlan(name="Plan Diario (test)", price=Decimal("50"), duration_value=1,
                          duration_unit="day", fixed_time_slot=True)
    db.add(plan)
    await db.flush()
    tools = build_tools(
        ChatbotContext(db=db, conversation_id=CONVERSATION, member_id=None, wa_id=WA_ID,
                       require_mp_payment=False, mp_required=True)
    )
    reply = await _tool(tools, "propose_day_pass")(
        plan_id=plan.id, session_id=sc["session"].id, full_name="Ana López"
    )
    assert "NO la confirmes" in reply
    pending = (await db.execute(select(func.count(ChatbotPendingAction.id)))).scalar()
    assert pending == 0  # nothing waiting for a "Sí" that would record an uncollected payment


async def test_cancelar_during_a_purchase_is_not_an_opt_out(db, monkeypatch):
    from app.services import whatsapp_optout

    monkeypatch.setattr(reply_service, "schedule_text_send", lambda *a, **k: None)
    _sc, event = await _published(db)
    conversation = SimpleNamespace(id=CONVERSATION, bot_paused_until=None)
    contact = SimpleNamespace(id=1, wa_id=WA_ID)
    message = SimpleNamespace(text_content="Cancelar", wa_message_id="wamid.c")

    db.add(SpecialEventOrder(
        event_id=event.id, participant_name="Ana", participant_key="8711234567",
        conversation_id=CONVERSATION, status="awaiting_payment", amount=Decimal("500"),
        external_reference="event-cancelar", hold_expires_at=datetime.now(timezone.utc) + timedelta(minutes=20),
    ))
    await db.flush()
    assert await whatsapp_optout.handle_keyword(db, message, contact, conversation) is False
    assert conversation.bot_paused_until is None

    other = SimpleNamespace(id=CONVERSATION + 1, bot_paused_until=None)
    assert await whatsapp_optout.handle_keyword(db, message, contact, other) is True
    assert other.bot_paused_until is not None  # outside a purchase it still opts out


def test_system_prompt_separates_events_from_regular_purchases():
    config = SimpleNamespace(system_prompt="", tone="", require_confirmation=True)
    prompt = build_system_prompt(config, "", None, event_note="🎟️ Evento especial en contexto: #1")
    assert "EVENTOS ESPECIALES" in prompt and "NUNCA digas que una entrada está pagada" in prompt
    assert "🎟️ Evento especial en contexto" in prompt
    assert "CLASE REGULAR" in prompt
