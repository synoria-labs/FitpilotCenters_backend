from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import func, select
from app.core.timezones import local_date, local_datetime
from app.models import MembershipSubscription, Payment, People, Reservation
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
    ctx.message_id = 2
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


async def test_same_turn_cannot_confirm_a_quote_it_just_created(db, monkeypatch):
    _, _, ctx, order = await draft(db)
    ctx.user_text = "Sí"
    create = AsyncMock(side_effect=AssertionError("Fresh human confirmation required"))
    monkeypatch.setattr(sales.mp, "create_preference", create)
    assert "nueva confirmación" in await sales.checkout(ctx)
    create.assert_not_awaited()


async def test_interrupted_fulfillment_is_retried_without_losing_the_payment(db, monkeypatch):
    _, day, ctx, order = await draft(db)
    order.status = "awaiting_payment"
    await db.commit()
    original = sales.apply_order
    provider_payment = approval(order, day)
    monkeypatch.setattr(sales, "apply_order", AsyncMock(side_effect=RuntimeError("Transient failure")))
    with pytest.raises(RuntimeError, match="Transient"):
        await sales.approved_payment(db, "interrupted-sale", provider_payment)
    await db.rollback()
    monkeypatch.setattr(sales, "apply_order", original)
    result = await sales.approved_payment(db, "interrupted-sale", provider_payment)
    assert "Pago acreditado" in result["text"]
    assert await db.scalar(select(func.count()).select_from(Payment)) == 1


async def test_bank_transfer_webhook_pending_then_approved_notifies_once(db, monkeypatch):
    from contextlib import asynccontextmanager
    from app.webhooks import mercadopago_webhook as webhook
    _, day, ctx, order = await draft(db)
    reference = order.external_reference
    order.status = "awaiting_payment"
    await db.commit()
    @asynccontextmanager
    async def factory():
        yield db
    monkeypatch.setattr(webhook, "async_session_factory", factory)
    notify = AsyncMock()
    monkeypatch.setattr(webhook, "_notify", notify)
    await webhook._process_approved_payment("transfer-v2", dict(status="pending", external_reference=reference))
    assert await db.scalar(select(func.count()).select_from(Payment)) == 0
    data = approval(order, day) | {"payment_type_id": "bank_transfer"}
    await webhook._process_approved_payment("transfer-v2", data)
    await webhook._process_approved_payment("transfer-v2", data)
    notify.assert_awaited_once()
    assert await db.scalar(select(func.count()).select_from(Payment)) == 1


async def test_manual_order_resolution_requires_subscription_permission(db, monkeypatch):
    from app.graphql.chatbot import sales as api
    check = AsyncMock(return_value="denied")
    monkeypatch.setattr(api, "require_capability", check)
    info = SimpleNamespace(context=SimpleNamespace(db=db, user=SimpleNamespace(id=1)))
    assert await api.MembershipSalesQuery().membership_purchase_orders(info) == []
    result = await api.MembershipSalesMutation().confirm_membership_purchase_start(info, 1, datetime.now(timezone.utc).date())
    assert not result.success and result.error == "denied"


async def test_new_start_keeps_the_price_already_paid_and_records_history(db):
    sc, day, ctx, order = await draft(db)
    order.status = "awaiting_payment"
    await db.commit()
    await sales.approved_payment(db, "reconfirm-frozen-price", approval(order, day, late=True))
    sc.plan.price = 250
    await db.flush()
    await sales.confirm_start(db, order.id, day + timedelta(days=7), conversation_id=ctx.conversation_id, message_id=3)
    assert Decimal(order.facts["amount"]) == 200
    assert order.facts["start_history"][-1]["previous_start"] == day.isoformat()


async def test_paid_late_has_no_refund_or_new_preference(db, monkeypatch):
    _, day, ctx, order = await draft(db)
    refund, create = AsyncMock(), AsyncMock()
    monkeypatch.setattr(sales.mp, "refund_payment", refund)
    monkeypatch.setattr(sales.mp, "create_preference", create)
    await sales.approved_payment(db, "late-no-refund", approval(order, day, late=True))
    refund.assert_not_awaited()
    create.assert_not_awaited()
    assert order.status == "paid_awaiting_start"


async def test_requested_start_clause_ignores_the_day_the_customer_cannot_attend():
    from app.services.chatbot.verification import requested_day
    at = local_datetime(datetime(2026, 10, 25).date(), time(21, 47))
    assert requested_day("Solo que podría ir a partir del martes\nMañana se me complica un poco", at) == datetime(2026, 10, 27).date()
    assert requested_day("Podría iniciar martes o miércoles", at) is None


async def test_schedule_question_cannot_create_reservations(db):
    ctx = ChatbotContext(db, 1, 10, None, sales_v2_enabled=True, user_text="¿Cuál es mi horario?")
    tool = next(t for t in build_tools(ctx) if t.name == "reserve_with_membership")
    assert "no autoriza" in await tool.ainvoke({"session_id": 123})
    assert await db.scalar(select(func.count()).select_from(Reservation)) == 0


async def test_non_seated_quote_uses_capacity_and_not_number_of_bikes(db):
    from sqlalchemy import delete
    from app.models import Seat
    sc, day, ctx = await scenario(db)
    await db.execute(delete(Seat).where(Seat.venue_id == sc.templates[0].venue_id))
    facts = await sales.facts_for(db, plan_id=sc.plan.id, template_id=sc.templates[0].id, start_date=day)
    assert facts["available_places"] == 14 and facts["included_classes"] == 5


async def test_catalog_price_change_requires_another_confirmation_before_checkout(db, monkeypatch):
    sc, day, ctx, order = await draft(db)
    sc.plan.price = 250
    ctx.message_id, ctx.user_text = 2, "Sí"
    create = AsyncMock(return_value={"preference_id": "test-pref", "init_point": "https://example.invalid/payment"})
    monkeypatch.setattr(sales.mp, "create_preference", create)
    assert "cambiaron" in await sales.checkout(ctx)
    assert order.amount == 250
    create.assert_not_awaited()
    ctx.message_id = 3
    await sales.checkout(ctx)
    assert create.await_args.kwargs["amount"] == 250
    assert "excluded_payment_types" not in create.await_args.kwargs


async def test_receipt_without_provider_approval_never_confirms_a_reservation(db, monkeypatch):
    _, _, ctx, order = await draft(db)
    order.mp_init_point, order.status = "https://example.invalid/payment", "awaiting_payment"
    await db.commit()
    ctx.user_text = "Envié un comprobante, ¿ya quedó pagado?"
    monkeypatch.setattr(sales.mp, "search_payments", AsyncMock(return_value=[{"status": "pending"}]))
    await finalize_reply(ctx, kind="reservation_confirmation", reservation_id=7377)
    assert "pendiente" in ctx.validated_reply and "no confirma" in ctx.validated_reply
    assert ctx.payment_link is None
    assert await db.scalar(select(func.count()).select_from(Payment)) == 0


async def test_provider_checkout_does_not_exclude_bank_transfers(monkeypatch):
    import json
    import httpx
    original = httpx.AsyncClient
    payloads = []
    def respond(request):
        payloads.append(json.loads(request.content))
        assert request.headers["X-Idempotency-Key"] == "test-order"
        return httpx.Response(200, json={"id": "test-only", "init_point": "https://example.invalid/checkout"})
    monkeypatch.setattr(sales.mp.cfg, "is_configured", lambda: True)
    monkeypatch.setattr(sales.mp.cfg, "is_test", lambda: False)
    monkeypatch.setattr(sales.mp.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    await sales.mp.create_preference(amount=200, description="Weekly", external_reference="test-order", idempotency_key="test-order")
    assert not payloads[0].get("binary_mode")
    assert not payloads[0].get("payment_methods", {}).get("excluded_payment_types")


async def test_future_membership_is_not_active_in_chat_or_campaign_filters(db):
    from app.crud import whatsappCrud
    from app.services.segmentation_service import _membership_status_condition
    _, day, ctx, order = await draft(db)
    await sales.approved_payment(db, "future-status-test", approval(order, day))
    now = datetime.now(timezone.utc)
    status = await db.scalar(select(whatsappCrud._reference_membership_status_sq(now)).where(People.id == order.member_id))
    assert status == "scheduled"
    active = await db.scalar(select(func.count()).select_from(People).where(People.id == order.member_id, _membership_status_condition(["active"], now)))
    assert active == 0


async def test_delayed_daily_webhook_uses_approval_time_and_preserves_the_chosen_class(db, monkeypatch):
    from tests.special_event_factories import make_session
    sc, _, ctx = await scenario(db)
    sc.plan.duration_value = 1
    day = local_date(datetime.now(timezone.utc)) - timedelta(days=1)
    session = await make_session(db, sc.templates[0], day)
    monkeypatch.setattr(sales, "now", lambda: session.start_at - timedelta(days=1))
    ctx.user_text = f"Quiero empezar el {day.isoformat()}"
    await sales.propose(ctx, plan_id=sc.plan.id, session_id=session.id, start_date=day.isoformat(), full_name="Delayed webhook buyer")
    order = await sales.active_order(db, ctx.conversation_id)
    order.status, session.status = "awaiting_payment", "completed"
    await db.commit()
    real_now = datetime.now(timezone.utc)
    monkeypatch.setattr(sales, "now", lambda: real_now)
    payment = dict(status="approved", external_reference=order.external_reference, transaction_amount=200,
                   currency_id="MXN", date_approved=(session.start_at - timedelta(minutes=20)).isoformat())
    result = await sales.approved_payment(db, "delayed-daily-webhook", payment)
    assert order.status == "confirmed" and "Pago acreditado" in result["text"]
    sub = await db.get(MembershipSubscription, order.subscription_id)
    assert sub.session_id == session.id and sub.end_at == session.end_at
    assert sub.status == "expired"


@pytest.mark.parametrize("mode", ["flexible", "credit_pack"])
async def test_late_open_access_or_credits_keeps_the_money_and_asks_for_a_new_start(db, mode):
    sc = await group(db, mode=mode)
    day = sc.day
    ctx = ChatbotContext(db, 9, None, None, sales_v2_enabled=True, user_text=f"Quiero iniciar el {day.isoformat()}", message_id=1)
    await sales.propose(ctx, plan_id=sc.plan.id, start_date=day.isoformat(), full_name="Anonymous flexible buyer")
    order = await sales.active_order(db, 9)
    payment = approval(order, day + timedelta(days=3)) | {"transaction_amount": float(order.amount)}
    await sales.approved_payment(db, "late-flexible-credits", payment)
    assert order.payment_id is not None and order.status == "paid_awaiting_start"
    assert await db.scalar(select(func.count()).select_from(MembershipSubscription)) == 0
    await sales.confirm_start(db, order.id, day + timedelta(days=7), conversation_id=9)
    sub = await db.get(MembershipSubscription, order.subscription_id)
    assert local_date(sub.start_at) == day + timedelta(days=7)
    assert await db.scalar(select(func.count()).select_from(Payment)) == 1
