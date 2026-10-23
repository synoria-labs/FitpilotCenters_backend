"""Open payment links check inventory at payment time; refunds require an admin decision."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import delete, func, select, text

from app.crud.special_events import events as events_crud, orders as orders_crud
from app.crud.special_events.common import SpecialEventError
from app.graphql.special_events.builders import build_event
from app.models import ClassType, Payment, Seat, Venue
from app.models.specialEventModel import SpecialEvent, SpecialEventOrder, SpecialEventSeat
from app.models.whatsappModel import WebhookLog
from app.services import mercadopago_service as gateway, special_event_payments as service
from app.services.chatbot.tools import ChatbotContext, build_tools
from app.services.chatbot.verification import finalize_reply
from tests.special_event_factories import make_account, next_friday, unique
from tests.test_special_event_payments import FakeMercadoPago, approved, mp, published_event, start  # noqa: F401
from tests.test_special_events_graphql import _ctx, _run


async def open_event(db, capacity=1):
    sc, event = await published_event(db, capacity=capacity)
    await events_crud.update_event(db, event, actor_account_id=None, payment_link_expires=False)
    await db.flush()
    return sc, event


async def no_room_payment(db, mp):
    _, event = await open_event(db)
    first = await orders_crud.get_order(db, (await start(db, event)).order_id)
    other = await orders_crud.get_order(db, (await start(db, event, phone="8710000002")).order_id)
    paid = approved(other)
    assert await service.process_payment_notification(paid["id"], paid) == "confirmed"
    payment = approved(first)
    assert await service.process_payment_notification(payment["id"], payment) == "manual_refund_required"
    await db.refresh(first)
    return event, first, payment


async def test_open_links_never_hold_a_bike_or_reduce_capacity_and_retries_reuse_the_link(db, mp):
    _, event = await open_event(db)
    first = await start(db, event)
    second = await start(db, event, phone="8710000002")
    assert first.ok and second.ok and not first.payment_link_expires
    assert all(p["expires_at"] is None for p in mp.preferences)
    for identifier in (first.order_id, second.order_id):
        order = await orders_crud.get_order(db, identifier)
        assert order.hold_expires_at is None and order.seat_id is None
        assert order.payment_link_expires is False
    avail = await events_crud.availability(db, event)
    assert avail.held == 0 and avail.available == 1 and len(avail.free_seat_ids) == 14
    assert await events_crud.count_active_orders(db, event.id) == 0
    assert await events_crud.count_active_orders(db, event.id, include_open_links=True) == 2
    await events_crud.update_event(db, event, actor_account_id=None, capacity=1)
    again = await start(db, event)
    assert again.order_id == first.order_id and again.init_point == first.init_point
    assert len(mp.preferences) == 2


async def test_full_event_does_not_issue_or_resend_an_open_link(db, mp):
    _, event = await open_event(db)
    first = await start(db, event)
    await orders_crud.add_manual_participant(db, event.id, actor_account_id=None,
        participant_name="Paid at desk", participant_phone="8710000002", payment_method="cash")
    await db.commit()
    for phone in ("8711234567", "8710000003"):
        await db.refresh(event)  # a refused purchase rolls back the current transaction
        result = await start(db, event, phone=phone)
        assert not result.ok and "lugares" in result.message
    assert len(mp.preferences) == 1
    ctx = ChatbotContext(db, 77, None, "5218711234567")
    rejected = await finalize_reply(ctx, kind="purchase", order_id=first.order_id)
    assert ctx.validated_reply is None and "No envíes" in rejected


async def test_payment_without_space_is_recorded_and_never_automatically_refunded(db, mp):
    event, order, payment = await no_room_payment(db, mp)
    assert order.status == "needs_review" and order.manual_refund_required
    assert order.mp_payment_id == str(payment["id"]) and order.seat_id is None
    assert order.payment_id and (await db.get(Payment, order.payment_id)).status == "COMPLETED"
    assert (await build_event(db, event)).manual_refunds_pending == 1
    assert mp.refunds == []
    assert "reembolso manual" in mp.sent[-1][1] and "aún no se ha realizado" in mp.sent[-1][1]
    assert await service.process_payment_notification(payment["id"], payment) == "duplicate_notification"
    assert len(mp.sent) == 2 and mp.refunds == []  # one confirmation, one review notice
    with pytest.raises(SpecialEventError, match="verificación"):
        await service.execute_refund(db, order.id, order.mp_payment_id, "sin cupo", notify_customer=False)
    assert mp.refunds == []


async def test_open_payment_policy_is_snapshotted_even_if_event_mode_changed_before_payment(db, mp):
    _, event = await open_event(db)
    order = await orders_crud.get_order(db, (await start(db, event)).order_id)
    await events_crud.update_event(db, event, actor_account_id=None, payment_link_expires=True)
    other = await orders_crud.get_order(db, (await start(db, event, phone="8710000002")).order_id)
    await db.commit()
    payment = approved(order)
    assert await service.process_payment_notification(payment["id"], payment) == "manual_refund_required"
    assert mp.refunds == []
    await db.refresh(other)
    assert other.status == "awaiting_payment" and other.payment_link_expires


@pytest.mark.parametrize("provider_status", ["approved", "refunded"])
async def test_admin_review_is_required_and_previously_refunded_money_is_not_refunded_twice(db, mp, monkeypatch, provider_status):
    event, order, payment = await no_room_payment(db, mp)
    admin = await make_account(db)
    with pytest.raises(SpecialEventError, match="confirma la verificación"):
        await service.refund_order(db, order.id, actor_account_id=admin.id, reason="sin cupo")
    with pytest.raises(SpecialEventError, match="confirma la verificación"):
        await service.refund_order(db, order.id, actor_account_id=None, reason="sin cupo", review_confirmed=True)
    assert mp.refunds == []
    payment["status"] = provider_status
    get_payment = AsyncMock(return_value=payment)
    monkeypatch.setattr(gateway, "get_payment", get_payment)
    result = await service.refund_order(db, order.id, actor_account_id=admin.id,
                                        reason="Pago sin cupo revisado", review_confirmed=True)
    assert result.status == "refunded" and result.refund_reviewed_by == admin.id and result.refund_reviewed_at
    assert (await db.get(Payment, result.payment_id)).status == "REFUNDED"
    assert len(mp.refunds) == (1 if provider_status == "approved" else 0)
    assert (await build_event(db, event)).manual_refunds_pending == 0
    await service.refund_order(db, order.id, actor_account_id=admin.id, reason="reintento", review_confirmed=True)
    get_payment.assert_awaited_once_with(str(payment["id"]))


@pytest.mark.parametrize("bad", ["id", "reference", "amount", "currency", "pending", "unavailable", "partial"])
async def test_manual_refund_refuses_provider_mismatch_or_uncertainty(db, mp, monkeypatch, bad):
    _, order, payment = await no_room_payment(db, mp)
    admin = await make_account(db)
    changes = {"id": {"id": "another-payment"}, "reference": {"external_reference": "event-other"},
               "amount": {"transaction_amount": 1}, "currency": {"currency_id": "USD"},
               "pending": {"status": "pending"}, "partial": {"transaction_amount_refunded": 100}}
    payment.update(changes.get(bad, {}))
    get_payment = AsyncMock(return_value=payment, side_effect=RuntimeError("offline") if bad == "unavailable" else None)
    monkeypatch.setattr(gateway, "get_payment", get_payment)
    with pytest.raises(SpecialEventError):
        await service.refund_order(db, order.id, actor_account_id=admin.id, reason="sin cupo", review_confirmed=True)
    assert order.status == "needs_review" and order.refund_reviewed_at is None and mp.refunds == []


async def test_manual_refund_done_in_mercadopago_is_reconciled_by_its_webhook(db, mp):
    _, order, payment = await no_room_payment(db, mp)
    payment["status"] = "refunded"
    assert await service.process_payment_notification(payment["id"], payment) == "refunded"
    await db.refresh(order)
    assert order.status == "refunded" and (await db.get(Payment, order.payment_id)).status == "REFUNDED"
    assert mp.refunds == []


async def test_changing_event_mode_does_not_rewrite_existing_purchases(db, mp):
    _, event = await published_event(db, capacity=2)
    timed = await start(db, event)
    await events_crud.update_event(db, event, actor_account_id=None, payment_link_expires=False)
    opened = await start(db, event, phone="8710000002")
    await events_crud.update_event(db, event, actor_account_id=None, payment_link_expires=True, hold_minutes=60)
    await db.flush()
    old = await orders_crud.get_order(db, timed.order_id)
    new = await orders_crud.get_order(db, opened.order_id)
    assert old.payment_link_expires and old.hold_expires_at == timed.hold_expires_at
    assert not new.payment_link_expires and new.hold_expires_at is None
    assert (await events_crud.availability(db, event)).held == 1


@pytest.mark.parametrize("closed", ["canceled", "started"])
async def test_open_link_paid_after_sales_close_requires_manual_review(db, mp, closed):
    _, event = await open_event(db)
    order = await orders_crud.get_order(db, (await start(db, event)).order_id)
    if closed == "canceled":
        await events_crud.cancel_event(db, event, actor_account_id=None, reason="Cancelado")
    else:
        event.start_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db.flush()
    payment = approved(order)
    assert await service.process_payment_notification(payment["id"], payment) == "manual_refund_required"
    assert mp.refunds == []


async def test_sweep_reconciles_open_links_and_never_expires_unpaid_ones(db, mp):
    _, event = await open_event(db, capacity=2)
    paid = await orders_crud.get_order(db, (await start(db, event)).order_id)
    unpaid = await orders_crud.get_order(db, (await start(db, event, phone="8710000002")).order_id)
    mp.search_results[paid.external_reference] = [approved(paid)]
    result = await service.sweep_holds()
    await db.refresh(unpaid)
    await db.refresh(paid)
    assert result["reconciled"] == 1 and result["expired"] == 0
    assert unpaid.status == "awaiting_payment" and unpaid.hold_expires_at is None and paid.status == "paid"


async def test_chatbot_and_server_response_explain_no_seat_hold(db, mp):
    _, event = await open_event(db)
    ctx = ChatbotContext(db, 77, None, "5218711234567")
    tools = build_tools(ctx)
    detail = await next(t for t in tools if t.name == "get_special_event").ainvoke({"event_id": event.id})
    assert "NO aparta lugar" in detail and "30 minutos" not in detail
    proposal = await next(t for t in tools if t.name == "start_event_purchase").ainvoke({
        "event_id": event.id, "participant_name": "Compradora"})
    assert "SIN CADUCIDAD" in proposal and "NO hay lugar" in proposal
    order = await db.scalar(select(SpecialEventOrder).where(SpecialEventOrder.event_id == event.id))
    await finalize_reply(ctx, kind="purchase", order_id=order.id)
    assert "No aparta lugar" in ctx.validated_reply and "sin caducidad" in ctx.validated_reply
    assert ctx.payment_link.url == "https://mp.test/checkout/1"  # sent as the «Pagar ahora» button


async def test_published_event_and_graphql_keep_explicit_false_and_partial_updates(db, mp):
    _, event = await published_event(db)
    admin = await make_account(db)
    ctx = _ctx(db, admin)
    mutation = """mutation($id:Int!, $input:SpecialEventInput!) {
        updateSpecialEvent(id:$id,input:$input) { success error detail {
          event { paymentLinkExpires holdMinutes manualRefundsPending } } } }"""
    result = await _run(db, ctx, mutation, id=event.id, input={"paymentLinkExpires": False})
    assert result["updateSpecialEvent"]["detail"]["event"] == {
        "paymentLinkExpires": False, "holdMinutes": 30, "manualRefundsPending": 0}
    result = await _run(db, ctx, mutation, id=event.id, input={"name": "Cambio de nombre"})
    assert result["updateSpecialEvent"]["detail"]["event"]["paymentLinkExpires"] is False


async def test_graphql_refund_requires_capability_and_human_review(db, mp, monkeypatch):
    _, order, payment = await no_room_payment(db, mp)
    admin = await make_account(db)
    ctx, staff_ctx, order_id = _ctx(db, admin), _ctx(db, admin, role="staff"), order.id
    await db.commit()
    monkeypatch.setattr(gateway, "get_payment", AsyncMock(return_value=payment))
    mutation = """mutation($id:Int!, $reviewed:Boolean!) {
        refundSpecialEventOrder(orderId:$id,reviewConfirmed:$reviewed) { success error detail {
          orders { manualRefundRequired refundReviewedBy refundReviewedAt } } } }"""
    denied = await _run(db, staff_ctx, mutation, id=order_id, reviewed=True)
    assert not denied["refundSpecialEventOrder"]["success"] and mp.refunds == []
    missing = await _run(db, ctx, mutation, id=order_id, reviewed=False)
    assert not missing["refundSpecialEventOrder"]["success"] and mp.refunds == []
    result = await _run(db, ctx, mutation, id=order_id, reviewed=True)
    assert result["refundSpecialEventOrder"]["success"] and len(mp.refunds) == 1


@pytest.mark.parametrize("timed", [False, True])
async def test_real_preference_request_explicitly_sets_expiry(monkeypatch, timed):
    from app.core.mercadopago_config import MercadoPagoConfig
    monkeypatch.setattr(MercadoPagoConfig, "ACCESS_TOKEN", "APP_USR-test-only")
    captured = []

    def handle(request):
        import json
        captured.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "pref-test", "init_point": "https://mp.test/link"})

    client = httpx.AsyncClient
    monkeypatch.setattr(gateway.httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(handle), **kw))
    expires = datetime.now(timezone.utc) + timedelta(minutes=30) if timed else None
    await gateway.create_preference(amount=500, description="Evento", external_reference="event-test", expires_at=expires)
    assert captured[0]["expires"] is timed
    assert ("expiration_date_to" in captured[0]) is timed
    assert ("expiration_date_from" in captured[0]) is timed


@asynccontextmanager
async def committed_open_event():
    from app.db.postgresql import async_session_factory
    async with async_session_factory() as db:
        venue = Venue(name=unique("Open links concurrency"), capacity=1)
        db.add(venue)
        await db.flush()
        db.add(Seat(venue_id=venue.id, label="Bike 1", is_active=True))
        kind = ClassType(code=unique("open"), name="Spinning")
        db.add(kind)
        await db.flush()
        event = await events_crud.create_event(db, actor_account_id=None, name=unique("Open link event"),
            venue_id=venue.id, class_type_id=kind.id, local_day=next_friday(), start_time=time(19),
            end_time=time(21), price=Decimal("500"), capacity=1, payment_link_expires=False)
        await events_crud.publish_event(db, event, actor_account_id=None)
        await db.commit()
        event_id, venue_id, kind_id = event.id, venue.id, kind.id
    try:
        yield event_id
    finally:
        async with async_session_factory() as db:
            refs = list(await db.scalars(select(SpecialEventOrder.external_reference).where(SpecialEventOrder.event_id == event_id)))
            await db.execute(delete(WebhookLog).where(WebhookLog.payload["external_reference"].as_string().in_(refs)))
            await db.execute(delete(SpecialEventOrder).where(SpecialEventOrder.event_id == event_id))
            await db.execute(delete(Payment).where(Payment.external_reference.in_(refs)))
            await db.execute(delete(SpecialEventSeat).where(SpecialEventSeat.event_id == event_id))
            await db.execute(delete(SpecialEvent).where(SpecialEvent.id == event_id))
            await db.execute(delete(Seat).where(Seat.venue_id == venue_id))
            await db.execute(delete(Venue).where(Venue.id == venue_id))
            await db.execute(delete(ClassType).where(ClassType.id == kind_id))
            await db.commit()


@pytest.mark.parametrize("same_buyer", [False, True])
async def test_concurrent_open_checkouts_and_payments_do_not_duplicate_links_or_oversell(monkeypatch, same_buyer):
    from app.core.mercadopago_config import MercadoPagoConfig
    from app.db.postgresql import async_session_factory
    fake = FakeMercadoPago()
    monkeypatch.setattr(MercadoPagoConfig, "ACCESS_TOKEN", "APP_USR-test-only")
    monkeypatch.setattr(service, "async_session_factory", async_session_factory)
    monkeypatch.setattr(service, "_send", AsyncMock())
    monkeypatch.setattr(gateway, "refund_payment", fake.refund_payment)

    async def preference(**kwargs):
        await asyncio.sleep(0.1)
        return await fake.create_preference(**kwargs)

    monkeypatch.setattr(gateway, "create_preference", preference)
    async with committed_open_event() as event_id:
        async def buy(phone):
            async with async_session_factory() as db:
                result = await service.start_whatsapp_purchase(db, event_id=event_id, participant_name=phone,
                    participant_phone=phone, conversation_id=77, wa_id=phone)
                assert result.ok
                return result.order_id
        ids = await asyncio.gather(buy("8710000001"), buy("8710000001" if same_buyer else "8710000002"))
        assert len(fake.preferences) == (1 if same_buyer else 2)
        async with async_session_factory() as db:
            orders = (await db.scalars(select(SpecialEventOrder).where(SpecialEventOrder.id.in_(ids)))).all()
            payments = [approved(o) for o in orders]
        results = await asyncio.gather(*(service.process_payment_notification(p["id"], p) for p in payments))
        assert sorted(results) == (["confirmed"] if same_buyer else ["confirmed", "manual_refund_required"])
        async with async_session_factory() as db:
            assert await db.scalar(select(func.count()).select_from(SpecialEventOrder).where(
                SpecialEventOrder.event_id == event_id, SpecialEventOrder.status == "paid")) == 1
        assert fake.refunds == []


async def test_downgrade_is_blocked_while_an_open_link_is_unresolved():
    from alembic import command
    from alembic.config import Config
    from app.db.postgresql import async_session_factory

    async with committed_open_event() as event_id:
        async with async_session_factory() as db:
            version_before = await db.scalar(text("SELECT version_num FROM app.alembic_version"))
            await orders_crud.create_hold(db, event_id, participant_name="Pending buyer", participant_phone="8710000009")
            await db.commit()
        with pytest.raises(RuntimeError, match="Resolve open payment links"):
            await asyncio.to_thread(command.downgrade, Config("alembic.ini"), "a7d0e3f6b9c2")
        async with async_session_factory() as db:
            assert await db.scalar(text("SELECT version_num FROM app.alembic_version")) == version_before
