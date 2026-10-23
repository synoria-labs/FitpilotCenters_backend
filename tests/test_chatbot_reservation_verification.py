from datetime import datetime, timedelta, timezone, time
from types import SimpleNamespace

import pytest

from app.models import Reservation
from app.services.chatbot.tools import ChatbotContext
from app.services.chatbot.verification import finalize_reply, reservation_context, requested_day, requested_hours
from app.services.chatbot import reply_service
from app.core.timezones import to_local
from tests.test_day_passes import scenario
from tests.special_event_factories import make_session, make_subscription


def test_relative_days_and_unspecified_ampm():
    now = datetime(2026, 10, 23, 1, tzinfo=timezone.utc)
    assert str(requested_day("hoy a las 8", now)) == "2026-10-22"
    assert str(requested_day("mañana a las 8", now)) == "2026-10-23"
    assert requested_hours("hoy a las 8") == {(8, 0), (20, 0)}
    assert requested_hours("hoy a las 8 pm") == {(20, 0)}


async def test_history_preserves_local_dates_and_labels_payment(monkeypatch):
    async def rows(*args, **kwargs):
        return [SimpleNamespace(id=1, direction="inbound", message_type="text", text_content="Hoy a las 8",
                                timestamp=datetime(2026, 10, 22, 1, 11)),
                SimpleNamespace(id=2, direction="outbound", message_type="text", text_content="¡Pago acreditado!",
                                timestamp=datetime(2026, 10, 22, 1, 13))]
    monkeypatch.setattr(reply_service.crud, "get_conversation_messages", rows)
    history = await reply_service._load_history(None, 1, None)
    assert all("21/10/2026" in item.content for item in history)
    assert "notificación automática de pago" in history[1].content


async def test_karla_old_reservation_and_active_membership_cannot_confirm_today(db):
    sc = await scenario(db)
    await make_subscription(db, sc.person)
    old = await make_session(db, sc.template, to_local(datetime.now(timezone.utc)).date() - timedelta(days=1))
    reservation = Reservation(person_id=sc.person.id, session_id=old.id, status="reserved", source="manual")
    db.add(reservation)
    await db.flush()
    ctx = ChatbotContext(db, 474, sc.person.id, "test", user_text="Quiero pagar para el día de hoy a las 8")
    result = await finalize_reply(ctx, kind="reservation_confirmation", session_id=old.id,
                                  reservation_id=reservation.id, requested_date=str(to_local(old.start_at).date()))
    assert ctx.validated_reply is None and "no confirma" in result
    note = await reservation_context(db, sc.person.id)
    assert "NINGUNA" in note
    assert reservation.status == "reserved"  # historical records are unchanged


async def test_confirmation_uses_verified_facts_and_rechecks_cancellations(db):
    sc = await scenario(db)
    reservation = Reservation(person_id=sc.person.id, session_id=sc.session.id, status="reserved", source="manual")
    db.add(reservation)
    await db.flush()
    day = to_local(sc.session.start_at).date().isoformat()
    ctx = ChatbotContext(db, 1, sc.person.id, "test", user_text=f"Mi reserva para {day} a las 8 pm")
    args = dict(kind="reservation_confirmation", reservation_id=reservation.id, session_id=sc.session.id, requested_date=day,
                text="This invented text must never appear")
    await finalize_reply(ctx, **args)
    assert f"#{reservation.id}" in ctx.validated_reply and "20:00" in ctx.validated_reply
    assert "invented" not in ctx.validated_reply
    reservation.status = "canceled"
    await db.flush()
    await finalize_reply(ctx, **args)
    assert ctx.validated_reply is None


async def test_wrong_day_wrong_owner_and_displaced_class_are_rejected(db):
    sc = await scenario(db)
    reservation = Reservation(person_id=sc.person.id, session_id=sc.session.id, status="reserved", source="manual")
    db.add(reservation)
    await db.flush()
    ctx = ChatbotContext(db, 1, sc.person.id, "test", user_text="Quiero pagar hoy a las 8")
    args = dict(kind="reservation_confirmation", reservation_id=reservation.id, session_id=sc.session.id,
                requested_date=to_local(sc.session.start_at).date().isoformat())
    assert "no coincide" in await finalize_reply(ctx, **args)
    ctx.member_id = sc.person.id + 99999
    assert "no confirma" in await finalize_reply(ctx, **args)
    ctx.member_id = sc.person.id
    sc.session.status = "displaced"
    await db.flush()
    assert "no confirma" in await finalize_reply(ctx, **args)


async def test_ambiguous_eight_requires_clarification(db):
    sc = await scenario(db)
    day = to_local(sc.session.start_at).date()
    sc.template.start_time_local = time(8)
    await make_session(db, sc.template, day)
    reservation = Reservation(person_id=sc.person.id, session_id=sc.session.id, status="reserved", source="manual")
    db.add(reservation)
    await db.flush()
    ctx = ChatbotContext(db, 1, sc.person.id, "test", user_text=f"El {day} a las 8")
    result = await finalize_reply(ctx, kind="reservation_confirmation", reservation_id=reservation.id,
                                  session_id=sc.session.id, requested_date=day.isoformat())
    assert "más de una" in result and ctx.validated_reply is None


@pytest.mark.parametrize("text", ["Ya tienes tu lugar confirmado", "Tu reserva #7442 está activa", "Tu bici está apartada"])
async def test_free_text_cannot_bypass_confirmation_gate(db, text):
    ctx = ChatbotContext(db, 1, None, "test")
    await finalize_reply(ctx, kind="information", text=text)
    assert ctx.validated_reply is None


async def test_purchase_link_is_rendered_from_pending_facts_not_model_text(db):
    from app.crud.chatbotPendingCrud import upsert_pending
    sc = await scenario(db)
    await upsert_pending(db, conversation_id=474, action_type="buy_day_pass", member_id=sc.person.id,
        payload={"plan_id": sc.plan.id, "session_id": sc.session.id}, status="awaiting_payment",
        mp_init_point="https://www.mercadopago.com.mx/verified-checkout", summary="Pase diario $50")
    day = to_local(sc.session.start_at).date()
    ctx = ChatbotContext(db, 474, sc.person.id, "test", user_text=f"Quiero pagar el {day} a las 8 pm")
    await finalize_reply(ctx, kind="purchase", text="Tu lugar está confirmado para ayer; invented checkout")
    assert ctx.payment_link.url.endswith("verified-checkout")  # sent as the «Pagar ahora» button
    assert "mercadopago" not in ctx.validated_reply  # never also pasted into the text
    assert "20:00" in ctx.validated_reply and "21:00" in ctx.validated_reply
    assert "invented" not in ctx.validated_reply and "ayer" not in ctx.validated_reply


async def test_buying_cannot_be_answered_as_general_information(db):
    ctx = ChatbotContext(db, 474, None, "test", user_text="Quiero pagar para hoy a las 8")
    result = await finalize_reply(ctx, kind="information", text="No necesitas pagar porque tienes membresía activa.")
    assert ctx.validated_reply is None and "compra/reserva" in result


async def test_final_tool_records_verification_and_handles_invalid_dates(db):
    from app.services.chatbot.tools import build_tools
    ctx = ChatbotContext(db, 474, None, None)
    tool = next(tool for tool in build_tools(ctx) if tool.name == "respond_to_customer")
    await tool.ainvoke({"kind": "information", "text": "Hola, ¿en qué puedo ayudarte?"})
    assert ctx.validated_reply == "Hola, ¿en qué puedo ayudarte?"
    assert ctx.tools_used == ["respond_to_customer"]
    await tool.ainvoke({"kind": "reservation_confirmation", "requested_date": "not-a-date"})
    assert ctx.validated_reply is None and ctx.reply_request is None
