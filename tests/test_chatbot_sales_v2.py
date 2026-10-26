from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import func, select
from app.core.timezones import local_date, local_datetime
from app.models import MembershipSubscription, Payment, Reservation
from app.models.chatbotSalesModel import MembershipPurchaseOrder as Order
from app.services.chatbot import sales
from app.services.chatbot.intents import acknowledgment, payment_question
from app.services.chatbot.tools import ChatbotContext, build_tools
from app.services.chatbot.verification import finalize_reply
from tests.test_membership_coverage import group

pytestmark = pytest.mark.asyncio


async def scenario(db):
    sc = await group(db)
    sc.plan.duration_unit, sc.plan.duration_value, sc.plan.price = "day", 7, 200
    day = sc.day + timedelta(days=1)
    await db.flush()
    ctx = ChatbotContext(db, 90001, None, "5218710000123", sales_v2_enabled=True,
                         offer_in_person_registration=True, user_text=f"Quiero empezar el {day.isoformat()}", message_id=1)
    return sc, day, ctx


async def draft(db):
    sc, day, ctx = await scenario(db)
    await sales.propose(ctx, plan_id=sc.plan.id, template_id=sc.templates[2].id,
                        full_name="Cliente de prueba", start_date=day.isoformat())
    return sc, day, ctx, await sales.active_order(db, ctx.conversation_id)


@pytest.mark.parametrize("text", ["Hola", "Buenas noches", "Gracias", "Muchas gracias"])
async def test_soraya_and_karla_acknowledgments_cannot_confirm_reservations(db, text):
    ctx = ChatbotContext(db, 1, None, None, user_text=text)
    await finalize_reply(ctx, kind="reservation_confirmation", session_id=7377, reservation_id=7377, requested_date="2026-10-26")
    assert ctx.verification_result == "accepted" and "reserva" not in ctx.validated_reply.lower()
    assert ctx.validated_reply == acknowledgment(text)


@pytest.mark.parametrize("text", ["Podría pagar en efectivo?", "El martes que vaya pudiera pagar ahí?",
                                "Tendrán número de cuenta para hacer transferencia", "¿Puedo pagar cuando vaya?"])
async def test_yazmin_ivonne_payment_questions_are_information_not_checkout(db, text, monkeypatch):
    create = AsyncMock(side_effect=AssertionError("A question must not create a preference"))
    monkeypatch.setattr(sales.mp, "create_preference", create)
    ctx = ChatbotContext(db, 1, None, None, user_text=text, sales_v2_enabled=True, offer_in_person_registration=True)
    await finalize_reply(ctx, kind="information", text="irrelevante")
    assert ctx.verification_result == "accepted" and "mismo enlace" in ctx.validated_reply
    assert "recepción" in ctx.validated_reply and len(ctx.validated_reply.split()) <= 100
    create.assert_not_awaited()


async def test_choose_start_before_checkout_and_tuesday_week_has_five_classes(db, monkeypatch):
    sc, day, ctx = await scenario(db)
    create = AsyncMock(return_value={"preference_id": "test", "init_point": "https://example.com/test"})
    monkeypatch.setattr(sales.mp, "create_preference", create)
    assert "Qué día" in await sales.propose(ctx, plan_id=sc.plan.id)
    await sales.propose(ctx, plan_id=sc.plan.id, template_id=sc.templates[2].id,
                        start_date=day.isoformat(), full_name="Cliente de prueba")
    order = await sales.active_order(db, ctx.conversation_id)
    assert order.status == "draft" and order.facts["included_classes"] == 5
    assert local_date(sales.stamp(order.facts["start_at"])) == day
    assert local_date(sales.stamp(order.facts["end_at"])) == day + timedelta(days=7)
    assert "day(s)" not in sales.summary(order.facts) and "plantilla" not in sales.summary(order.facts)
    create.assert_not_awaited()
    ctx.user_text = "Sí"
    await sales.checkout(ctx)
    await sales.checkout(ctx)
    create.assert_awaited_once()
    assert await db.scalar(select(func.count()).select_from(Reservation)) == 0


async def test_same_checkout_for_transfer_and_pending_payment_does_not_confirm(db, monkeypatch):
    _, _, ctx, order = await draft(db)
    order.mp_init_point, order.status = "https://example.com/pay", "awaiting_payment"
    await db.flush()
    monkeypatch.setattr(sales.mp, "search_payments", AsyncMock(return_value=[]))
    ctx.user_text = "¿Puedo hacer transferencia?"
    await finalize_reply(ctx, kind="payment_options")
    assert ctx.payment_link.url == order.mp_init_point
    monkeypatch.setattr(sales.mp, "search_payments", AsyncMock(return_value=[{"status": "pending"}]))
    await finalize_reply(ctx, kind="payment_options")
    assert ctx.payment_link is None and "proceso" in ctx.validated_reply
    assert await db.scalar(select(func.count()).select_from(MembershipSubscription)) == 0


def approval(order, day, *, late=False):
    at = local_datetime(day if late else day - timedelta(days=2), time(21)).astimezone(timezone.utc)
    return dict(status="approved", external_reference=order.external_reference, transaction_amount=200,
                currency_id="MXN", date_approved=at.isoformat())


async def test_paid_early_reserves_from_chosen_date_and_is_idempotent(db):
    _, day, ctx, order = await draft(db)
    order.status = "awaiting_payment"
    await db.commit()
    result = await sales.approved_payment(db, "sales-v2-paid-1", approval(order, day))
    assert "Pago acreditado" in result["text"]
    assert (await sales.approved_payment(db, "sales-v2-paid-1", approval(order, day)))["text"] is None
    sub = await db.get(MembershipSubscription, order.subscription_id)
    assert sub.period_rule_version == 2 and local_date(sub.start_at) == day
    dates = (await db.execute(select(Reservation).where(Reservation.person_id == sub.person_id))).scalars().all()
    assert len(dates) == 5
    assert await db.scalar(select(func.count()).select_from(Payment)) == 1


async def test_late_transfer_retains_payment_and_reconfirms_start_without_new_charge(db):
    _, day, ctx, order = await draft(db)
    order.status = "awaiting_payment"
    await db.commit()
    result = await sales.approved_payment(db, "sales-v2-late-1", approval(order, day, late=True))
    assert order.status == "paid_awaiting_start" and order.payment_id
    assert "No necesitas pagar" in result["text"]
    assert await db.scalar(select(func.count()).select_from(MembershipSubscription)) == 0
    new_day = day + timedelta(days=7)
    result = await sales.confirm_start(db, order.id, new_day, conversation_id=ctx.conversation_id)
    assert "Pago acreditado" in result and order.status == "confirmed"
    sub = await db.get(MembershipSubscription, order.subscription_id)
    assert local_date(sub.start_at) == new_day
    assert await db.scalar(select(func.count()).select_from(Payment)) == 1


async def test_in_person_choice_never_records_money_or_reservation(db):
    ctx = ChatbotContext(db, 2, None, None, user_text="Prefiero pagar en recepción", sales_v2_enabled=True,
                         offer_in_person_registration=True)
    await finalize_reply(ctx, kind="payment_options")
    assert "recepción" in ctx.validated_reply
    assert await db.scalar(select(func.count()).select_from(Payment)) == 0
    assert await db.scalar(select(func.count()).select_from(Reservation)) == 0


async def test_start_cannot_be_invented_from_model_argument(db):
    sc, day, ctx = await scenario(db)
    ctx.user_text = "Me interesa el semanal"
    result = await sales.propose(ctx, plan_id=sc.plan.id, start_date=day.isoformat(),
                                 template_id=sc.templates[0].id, full_name="Prueba")
    assert "confirme la fecha" in result
    assert await sales.active_order(db, ctx.conversation_id) is None


async def test_confirmation_is_bound_to_its_conversation(db):
    _, day, ctx, order = await draft(db)
    with pytest.raises(ValueError, match="conversación"):
        await sales.confirm_start(db, order.id, day, conversation_id=123)
