"""Mercado Pago settlement for special-event tickets.

Mercado Pago and WhatsApp are always faked (monkeypatch): no real charge, refund or message is
ever attempted. Services that open their own DB session are pointed at the test session.
"""
from __future__ import annotations

import asyncio
import itertools
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select

from app.core.mercadopago_config import MercadoPagoConfig
from app.crud.special_events import events as events_crud
from app.crud.special_events import orders as orders_crud
from app.crud.special_events import takeover as takeover_crud
from app.crud.special_events.common import SpecialEventError
from app.models import MembershipSubscription, Payment, Seat, Venue, ClassType
from app.models.specialEventModel import SpecialEvent, SpecialEventOrder, SpecialEventSeat
from app.models.whatsappModel import WebhookLog
from app.services import mercadopago_service, special_event_payments as sep
from tests.special_event_factories import (
    make_class_type,
    make_event,
    make_venue,
    next_friday,
    regular_class_scenario,
    same_session_factory,
    unique,
)

_ids = itertools.count(9_000_000_001)


class FakeMercadoPago:
    """Records every call; never talks to Mercado Pago."""

    def __init__(self):
        self.preferences = []
        self.refunds = []
        self.expired = []
        self.search_results = {}
        self.fail_preference = False
        self.fail_refund = False

    async def create_preference(self, **kwargs):
        if self.fail_preference:
            raise mercadopago_service.MercadoPagoError("boom")
        self.preferences.append(kwargs)
        n = len(self.preferences)
        return {"preference_id": f"pref-{n}", "init_point": f"https://mp.test/checkout/{n}"}

    async def refund_payment(self, payment_id, amount=None, *, idempotency_key=None):
        if self.fail_refund:
            raise mercadopago_service.MercadoPagoError("refund failed")
        self.refunds.append((str(payment_id), idempotency_key))
        return {"id": 1, "status": "approved"}

    async def search_payments(self, reference):
        return self.search_results.get(reference, [])

    async def expire_preference(self, preference_id):
        self.expired.append(preference_id)

    async def get_payment(self, payment_id):
        raise AssertionError("tests pass the payment explicitly")


@pytest.fixture
def mp(monkeypatch, db):
    fake = FakeMercadoPago()
    monkeypatch.setattr(MercadoPagoConfig, "ACCESS_TOKEN", "APP_USR-test-token")
    for name in ("create_preference", "refund_payment", "search_payments", "expire_preference", "get_payment"):
        monkeypatch.setattr(mercadopago_service, name, getattr(fake, name))
    monkeypatch.setattr(sep, "async_session_factory", same_session_factory(db))
    fake.sent = []

    async def fake_send(_db, conversation_id, text):
        fake.sent.append((conversation_id, text))

    monkeypatch.setattr(sep, "_send", fake_send)
    return fake


def approved(order, *, amount=None, currency="MXN", payment_id=None):
    return {
        "id": payment_id or next(_ids),
        "status": "approved",
        "status_detail": "accredited",
        "external_reference": order.external_reference,
        "transaction_amount": float(order.amount if amount is None else amount),
        "currency_id": currency,
    }


async def published_event(db, *, capacity=14, booked=0):
    sc = await regular_class_scenario(db, booked=booked)
    event = await make_event(db, sc, capacity=capacity)
    preview = await takeover_crud.preview_takeover(db, event)
    await takeover_crud.apply_takeover(
        db, event, actor_account_id=None, session_ids=preview.pending_session_ids
    )
    await events_crud.publish_event(db, event, actor_account_id=None)
    return sc, event


async def start(db, event, name="Cliente", phone="8711234567", conversation_id=77):
    return await sep.start_whatsapp_purchase(
        db, event_id=event.id, participant_name=name, participant_phone=phone,
        conversation_id=conversation_id, wa_id=f"521{phone}",
    )


# ---------------------------------------------------------------------------------------------
async def test_purchase_is_refused_without_mercadopago(db, monkeypatch):
    monkeypatch.setattr(MercadoPagoConfig, "ACCESS_TOKEN", "")
    _sc, event = await published_event(db)
    result = await start(db, event)
    assert result.ok is False and "NO está reservado" in result.message
    assert (await db.execute(select(func.count(SpecialEventOrder.id)))).scalar() == 0
    logged = (
        await db.execute(select(WebhookLog).where(WebhookLog.event_type == "special_event_mp_unavailable"))
    ).scalars().all()
    assert logged and logged[0].processed == 0


async def test_purchase_holds_seat_and_link_expires_with_the_hold(db, mp):
    _sc, event = await published_event(db)
    result = await start(db, event)
    assert result.ok and result.init_point == "https://mp.test/checkout/1"
    order = await orders_crud.get_order(db, result.order_id)
    assert order.status == "awaiting_payment" and order.mp_preference_id == "pref-1"
    assert order.external_reference.startswith("event-")

    call = mp.preferences[0]
    assert call["expires_at"] == order.hold_expires_at
    assert call["binary_mode"] is True
    assert set(call["excluded_payment_types"]) >= {"ticket", "atm"}
    assert Decimal(str(call["amount"])) == Decimal("500.00") and call["currency"] == "MXN"
    remaining = order.hold_expires_at - datetime.now(timezone.utc)
    assert timedelta(minutes=29) < remaining <= timedelta(minutes=30)

    # Retrying returns the same hold and link (no second order, no second preference).
    again = await start(db, event)
    assert again.order_id == order.id and len(mp.preferences) == 1


async def test_preference_failure_releases_the_seat(db, mp):
    _sc, event = await published_event(db, capacity=1)
    mp.fail_preference = True
    result = await start(db, event)
    assert result.ok is False and "no quedó apartado" in result.message
    orders = (await db.execute(select(SpecialEventOrder))).scalars().all()
    assert [o.status for o in orders] == ["canceled"]
    assert (await events_crud.availability(db, event)).available == 1


async def test_approved_payment_confirms_once_even_if_notified_twice(db, mp):
    _sc, event = await published_event(db)
    order = await orders_crud.get_order(db, (await start(db, event)).order_id)
    payment = approved(order)

    assert await sep.process_payment_notification(payment["id"], payment) == "confirmed"
    assert await sep.process_payment_notification(payment["id"], payment) == "duplicate_notification"

    await db.refresh(order)
    assert order.status == "paid" and order.mp_payment_id == str(payment["id"])
    payments = (
        await db.execute(select(Payment).where(Payment.provider_payment_id == str(payment["id"])))
    ).scalars().all()
    assert len(payments) == 1 and payments[0].subscription_id is None
    assert payments[0].amount == Decimal("500.00") and payments[0].method == "mercadopago"
    assert len(mp.sent) == 1 and "Pago acreditado" in mp.sent[0][1]
    assert "Bicicleta 1" in mp.sent[0][1]


async def test_ticket_payment_never_touches_memberships(db, mp):
    sc, event = await published_event(db, booked=1)
    member, sub, _sb, _res = sc["members"][0]
    before = (await db.execute(select(func.count(MembershipSubscription.id)))).scalar()
    end_before = sub.end_at
    order = await orders_crud.get_order(
        db, (await start(db, event, name=member.full_name, phone=member.phone_number)).order_id
    )
    payment = approved(order)
    await sep.process_payment_notification(payment["id"], payment)
    after = (await db.execute(select(func.count(MembershipSubscription.id)))).scalar()
    await db.refresh(sub)
    assert after == before and sub.end_at == end_before


async def test_amount_or_currency_mismatch_is_not_confirmed(db, mp):
    _sc, event = await published_event(db)
    order = await orders_crud.get_order(db, (await start(db, event)).order_id)
    payment = approved(order, amount=50)
    assert await sep.process_payment_notification(payment["id"], payment) == "needs_review"
    await db.refresh(order)
    assert order.status == "needs_review" and "importe" in order.review_reason
    assert (await db.execute(select(func.count(Payment.id)))).scalar() == 0
    assert "revisarlo" in mp.sent[-1][1]

    order2 = await orders_crud.get_order(db, (await start(db, event, phone="8710000002")).order_id)
    payment2 = approved(order2, currency="USD")
    assert await sep.process_payment_notification(payment2["id"], payment2) == "needs_review"


async def test_rejected_payment_keeps_the_hold(db, mp):
    _sc, event = await published_event(db)
    order = await orders_crud.get_order(db, (await start(db, event)).order_id)
    payment = approved(order)
    payment["status"] = "rejected"
    assert await sep.process_payment_notification(payment["id"], payment) == "rejected"
    await db.refresh(order)
    assert order.status == "awaiting_payment" and order.mp_status == "rejected"
    assert mp.sent == []


async def test_late_payment_is_honored_when_a_spot_is_left(db, mp):
    _sc, event = await published_event(db, capacity=2)
    order = await orders_crud.get_order(db, (await start(db, event)).order_id)
    order.hold_expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    await db.flush()
    await events_crud.expire_stale_holds(db, event_id=event.id)
    await db.refresh(order)
    assert order.status == "expired"

    payment = approved(order)
    assert await sep.process_payment_notification(payment["id"], payment) == "late_confirmed"
    await db.refresh(order)
    assert order.status == "paid"
    assert "después de que venció" in mp.sent[-1][1]


async def test_late_payment_without_space_is_refunded(db, mp):
    _sc, event = await published_event(db, capacity=1)
    late = await orders_crud.get_order(db, (await start(db, event, phone="8710000001")).order_id)
    late.hold_expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    await db.flush()
    # Someone else takes (and pays) the only spot after the hold lapsed.
    other = await orders_crud.get_order(db, (await start(db, event, phone="8710000002", conversation_id=88)).order_id)
    other_payment = approved(other, payment_id=555)
    assert await sep.process_payment_notification(other_payment["id"], other_payment) == "confirmed"

    payment = approved(late)
    assert await sep.process_payment_notification(payment["id"], payment) == "refund"
    await db.refresh(late)
    assert late.status == "refunded" and late.mp_payment_id == str(payment["id"])
    assert mp.refunds[-1] == (str(payment["id"]), f"event-refund-{payment['id']}")
    assert "reembolsamos" in mp.sent[-1][1]


async def test_refund_failure_is_left_for_review(db, mp):
    _sc, event = await published_event(db, capacity=1)
    order = await orders_crud.get_order(db, (await start(db, event)).order_id)
    await orders_crud.cancel_order(db, order.id, actor_account_id=None, reason="El cliente canceló")
    mp.fail_refund = True
    payment = approved(order)
    assert await sep.process_payment_notification(payment["id"], payment) == "refund"
    await db.refresh(order)
    assert order.status == "needs_review" and "reembólsalo manualmente" in order.review_reason
    logged = (
        await db.execute(select(WebhookLog).where(WebhookLog.event_type == "special_event_refund_failed"))
    ).scalars().all()
    assert logged


async def test_second_payment_for_a_paid_ticket_is_refunded(db, mp):
    _sc, event = await published_event(db)
    order = await orders_crud.get_order(db, (await start(db, event)).order_id)
    first = approved(order, payment_id=1001)
    second = approved(order, payment_id=1002)
    await sep.process_payment_notification(first["id"], first)
    assert await sep.process_payment_notification(second["id"], second) == "extra_payment"
    await db.refresh(order)
    assert order.status == "paid" and order.mp_payment_id == "1001"
    assert mp.refunds[-1][0] == "1002"
    assert (await db.execute(select(func.count(Payment.id)))).scalar() == 1


async def test_payment_after_event_cancellation_is_refunded(db, mp):
    _sc, event = await published_event(db)
    order = await orders_crud.get_order(db, (await start(db, event)).order_id)
    summary = await events_crud.cancel_event(db, event, actor_account_id=None, reason="Fuerza mayor")
    assert summary.preference_ids == [order.mp_preference_id]
    payment = approved(order)
    assert await sep.process_payment_notification(payment["id"], payment) == "refund"
    await db.refresh(order)
    assert order.status == "refunded"


async def test_unknown_reference_is_recorded_for_reconciliation(db, mp):
    payment = {"id": 4242, "status": "approved", "external_reference": "event-nope", "transaction_amount": 500}
    assert await sep.process_payment_notification(4242, payment) == "unmatched"
    logged = (
        await db.execute(select(WebhookLog).where(WebhookLog.event_type == "special_event_unmatched_payment"))
    ).scalars().all()
    assert logged and logged[0].x_request_id == "4242"


async def test_sweep_reconciles_lost_webhook_then_expires_the_rest(db, mp):
    _sc, event = await published_event(db)
    paid_late = await orders_crud.get_order(db, (await start(db, event, phone="8710000001")).order_id)
    abandoned = await orders_crud.get_order(db, (await start(db, event, phone="8710000002")).order_id)
    for o in (paid_late, abandoned):
        o.hold_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db.flush()
    mp.search_results[paid_late.external_reference] = [approved(paid_late, payment_id=7001)]

    stats = await sep.sweep_holds()
    assert stats["reconciled"] == 1 and stats["expired"] == 1
    await db.refresh(paid_late)
    await db.refresh(abandoned)
    assert paid_late.status == "paid" and abandoned.status == "expired"


async def test_staff_refund_of_a_paid_ticket(db, mp):
    _sc, event = await published_event(db)
    order = await orders_crud.get_order(db, (await start(db, event)).order_id)
    payment = approved(order)
    await sep.process_payment_notification(payment["id"], payment)
    await events_crud.cancel_event(db, event, actor_account_id=None, reason="Se pospone")
    await db.refresh(order)
    assert order.status == "refund_pending"

    refunded = await sep.refund_order(db, order.id, actor_account_id=None, reason="Evento cancelado")
    assert refunded.status == "refunded"
    pay = (await db.execute(select(Payment).where(Payment.id == order.payment_id))).scalar_one()
    assert pay.status == "REFUNDED"

    cash = await orders_crud.add_manual_participant(
        db, (await published_event(db))[1].id, actor_account_id=None,
        participant_name="Efectivo", participant_phone="8719990000", payment_method="cash",
    )
    refunded_cash = await sep.refund_order(db, cash.id, actor_account_id=None, reason="No asistirá")
    assert refunded_cash.status == "refunded" and len(mp.refunds) == 1  # cash never hits the API


async def test_webhook_routes_event_payments_and_asks_for_retry_on_failure(db, monkeypatch):
    from app.webhooks import mercadopago_webhook as hook

    calls = []

    async def fake_get_payment(pid):
        return {"id": pid, "status": "approved", "external_reference": "event-abc"}

    async def fake_process(pid, payment):
        calls.append(pid)
        if pid == "fail":
            raise RuntimeError("db down")
        return "confirmed"

    monkeypatch.setattr(hook.mercadopago_service, "get_payment", fake_get_payment)
    monkeypatch.setattr(sep, "process_payment_notification", fake_process)
    assert await hook._process_payment("123") is False
    assert await hook._process_payment("fail") is True  # -> HTTP 500, Mercado Pago retries
    assert calls == ["123", "fail"]

    legacy = []

    async def fake_chatbot_get_payment(pid):
        return {"id": pid, "status": "approved", "external_reference": "chatbot-xyz"}

    async def fake_legacy(pid, payment=None):
        legacy.append(pid)

    monkeypatch.setattr(hook.mercadopago_service, "get_payment", fake_chatbot_get_payment)
    monkeypatch.setattr(hook, "_process_approved_payment", fake_legacy)
    assert await hook._process_payment("999") is False
    assert legacy == ["999"]


# ---------------------------------------------------------------------------------------------
# Real concurrency: two buyers, one spot, two transactions.
# ---------------------------------------------------------------------------------------------
async def test_concurrent_buyers_for_the_last_spot(monkeypatch):
    from app.db.postgresql import async_session_factory

    async with async_session_factory() as setup:
        venue = Venue(name=unique("Concurrencia"), capacity=2)
        setup.add(venue)
        await setup.flush()
        setup.add_all([Seat(venue_id=venue.id, label=f"Bicicleta {i}", is_active=True) for i in (1, 2)])
        ctype = ClassType(code=unique("spin"), name="Spinning")
        setup.add(ctype)
        await setup.flush()
        event = await events_crud.create_event(
            setup, actor_account_id=None, name=unique("Evento"), venue_id=venue.id,
            local_day=next_friday(), start_time=time(19, 0), end_time=time(21, 0),
            price=Decimal("500"), class_type_id=ctype.id, capacity=1,
        )
        await events_crud.publish_event(setup, event, actor_account_id=None)
        await setup.commit()
        event_id, venue_id, ctype_id = event.id, venue.id, ctype.id

    async def buy(phone):
        async with async_session_factory() as s:
            try:
                await orders_crud.create_hold(s, event_id, participant_name=phone, participant_phone=phone)
                await asyncio.sleep(0.2)  # keep the lock while the other buyer waits
                await s.commit()
                return "ok"
            except SpecialEventError as exc:
                await s.rollback()
                return str(exc)

    try:
        results = await asyncio.gather(buy("8711111111"), buy("8712222222"))
        assert sorted(results) == ["Ya no hay lugares disponibles.", "ok"]
        async with async_session_factory() as check:
            held = (
                await check.execute(
                    select(func.count(SpecialEventOrder.id)).where(
                        SpecialEventOrder.event_id == event_id,
                        SpecialEventOrder.status == "awaiting_payment",
                    )
                )
            ).scalar()
            assert held == 1
    finally:
        async with async_session_factory() as cleanup:
            await cleanup.execute(delete(SpecialEventOrder).where(SpecialEventOrder.event_id == event_id))
            await cleanup.execute(delete(SpecialEventSeat).where(SpecialEventSeat.event_id == event_id))
            await cleanup.execute(delete(SpecialEvent).where(SpecialEvent.id == event_id))
            await cleanup.execute(delete(Seat).where(Seat.venue_id == venue_id))
            await cleanup.execute(delete(Venue).where(Venue.id == venue_id))
            await cleanup.execute(delete(ClassType).where(ClassType.id == ctype_id))
            await cleanup.commit()
