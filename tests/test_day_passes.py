"""Single-class expiry and all sales entry points, against isolated PostgreSQL."""
from datetime import datetime, timedelta, time, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models import MembershipPlan, MembershipSubscription, Payment, Reservation
from app.crud.memberships.day_passes import purchase_day_pass, expire_day_passes, effective_status
from app.crud.memberships.enrollment import create_member_enrollment_with_standing_booking, renew_subscription_with_standing_booking
from app.crud.memberships.subscriptions import create_membership_subscription, get_membership_subscriptions, get_member_active_subscription
from app.crud.memberships.utils import _calculate_subscription_end
from app.crud.memberships.data import _subscription_to_data
from app.crud.pos.sales import create_sale, SaleLineInput, SalePaymentInput
from app.services.chatbot.tools import _execute_pending
from tests.special_event_factories import make_venue, make_class_type, make_template, make_session, make_person, make_subscription


async def scenario(db, bikes=3):
    venue, seats = await make_venue(db, bikes=bikes)
    kind = await make_class_type(db)
    template = await make_template(db, venue, kind, at=time(20))
    session = await make_session(db, template, datetime.now(timezone.utc).date() + timedelta(days=3))
    person = await make_person(db, "Daily purchaser")
    plan = MembershipPlan(name="Daily", duration_unit="day", duration_value=1,
                          price=Decimal("50"), fixed_time_slot=True, is_active=True)
    db.add(plan)
    await db.flush()
    return SimpleNamespace(venue=venue, seats=seats, template=template, session=session, person=person, plan=plan)


@pytest.mark.parametrize("channel", ["subscription", "enrollment", "renewal", "pos_new", "pos_renewal", "chatbot_new", "chatbot_member"])
async def test_all_sales_channels_bind_one_session(db, channel):
    sc = await scenario(db)
    before = datetime.now(timezone.utc)
    existing = await make_subscription(db, sc.person)
    # Distinct paid periods coexist. Overlapping purchases now need admin approval.
    existing.start_at = sc.session.end_at + timedelta(days=1)
    existing.end_at = existing.start_at + timedelta(days=30)
    await db.flush()
    if channel == "subscription":
        sub = await create_membership_subscription(db, sc.person.id, sc.plan.id, session_id=sc.session.id, commit=False)
    elif channel == "enrollment":
        _, sub, _, _, sb, _ = await create_member_enrollment_with_standing_booking(
            db, "New Daily", sc.plan.id, session_id=sc.session.id, template_id=sc.template.id, commit=False)
        assert sb is None
    elif channel == "renewal":
        sub, _, _, sb, _ = await renew_subscription_with_standing_booking(
            db, sc.person.id, sc.plan.id, session_id=sc.session.id, template_id=sc.template.id, commit=False)
        assert sb is None
    elif channel.startswith("pos_"):
        sale = await create_sale(db, line_items=[SaleLineInput(
            line_type="membership_new" if channel == "pos_new" else "membership_renewal",
            full_name="POS daily", member_id=sc.person.id, plan_id=sc.plan.id,
            session_id=sc.session.id, template_id=sc.template.id,
        )], tenders=[SalePaymentInput("card", 50)], commit=False)
        sub = await db.get(MembershipSubscription, sale.line_items[0].subscription_id)
    else:
        pending = SimpleNamespace(action_type="buy_day_pass", member_id=sc.person.id if channel.endswith("member") else None,
            payload={"plan_id": sc.plan.id, "session_id": sc.session.id, "full_name": "Chat daily", "amount": "50"})
        reply = await _execute_pending(db, pending, payment_method="mercadopago", provider_payment_id="chat-payment")
        assert "20:00" in reply and "21:00" in reply
        sub = (await db.execute(select(MembershipSubscription).where(MembershipSubscription.session_id == sc.session.id))).scalar_one()
    assert sub.session_id == sc.session.id
    assert sub.end_at == sc.session.end_at
    assert before <= sub.start_at <= datetime.now(timezone.utc)
    assert existing.status == "active"  # a daily purchase must not cancel other memberships
    assert (await db.scalar(select(func.count()).select_from(Reservation).where(Reservation.session_id == sc.session.id))) == 1
    assert (await db.scalar(select(func.count()).select_from(Payment).where(Payment.subscription_id == sub.id))) == 1


@pytest.mark.parametrize("channel", ["subscription", "enrollment", "renewal", "pos"])
async def test_every_entry_point_requires_a_session(db, channel):
    sc = await scenario(db)
    with pytest.raises(ValueError, match="sessionId"):
        if channel == "subscription":
            await create_membership_subscription(db, sc.person.id, sc.plan.id, commit=False)
        elif channel == "enrollment":
            await create_member_enrollment_with_standing_booking(db, "New", sc.plan.id, commit=False)
        elif channel == "renewal":
            await renew_subscription_with_standing_booking(db, sc.person.id, sc.plan.id, commit=False)
        else:
            await create_sale(db, line_items=[SaleLineInput("membership_renewal", plan_id=sc.plan.id, member_id=sc.person.id)],
                              tenders=[SalePaymentInput("card", 50)], commit=False)
    assert await db.scalar(select(func.count()).select_from(Payment)) == 0


async def test_payment_replay_reuses_purchase_even_for_new_customer(db):
    sc = await scenario(db)
    args = dict(plan_id=sc.plan.id, session_id=sc.session.id, full_name="Idempotent new person",
                payment_provider="mercadopago", provider_payment_id="replayed-payment")
    first = await purchase_day_pass(db, **args)
    again = await purchase_day_pass(db, **args)
    assert again.payment.id == first.payment.id
    assert again.person.id == first.person.id
    assert again.subscription.id == first.subscription.id
    assert again.reservation.id == first.reservation.id
    first.payment.status = "REFUNDED"
    await db.flush()
    with pytest.raises(ValueError, match="conciliación"):
        await purchase_day_pass(db, **args)


async def test_mercadopago_webhook_retry_does_not_duplicate_or_reconfirm(db, monkeypatch):
    from app.crud.chatbotPendingCrud import upsert_pending
    from app.webhooks import mercadopago_webhook as webhook
    from tests.special_event_factories import same_session_factory
    sc = await scenario(db)
    pending = await upsert_pending(db, conversation_id=474, action_type="buy_day_pass", member_id=sc.person.id,
        payload={"plan_id": sc.plan.id, "session_id": sc.session.id, "amount": "50"},
        status="awaiting_payment", external_reference="daily-webhook-reference")
    await db.commit()
    notifications = []
    async def notify(_db, _conversation, text):
        notifications.append(text)
    monkeypatch.setattr(webhook, "async_session_factory", same_session_factory(db))
    monkeypatch.setattr(webhook, "_notify", notify)
    payment = {"status": "approved", "external_reference": "daily-webhook-reference"}
    await webhook._process_approved_payment("daily-webhook-payment", payment)
    await webhook._process_approved_payment("daily-webhook-payment", payment)
    assert pending.status == "confirmed" and len(notifications) == 1
    assert "20:00" in notifications[0] and "21:00" in notifications[0]
    assert await db.scalar(select(func.count()).select_from(Payment)) == 1
    assert await db.scalar(select(func.count()).select_from(Reservation)) == 1


async def test_rollout_opt_out_only_applies_when_session_is_omitted(db, monkeypatch):
    sc = await scenario(db)
    monkeypatch.setenv("DAY_PASS_SESSION_REQUIRED", "false")
    old_client = await create_membership_subscription(db, sc.person.id, sc.plan.id, commit=False)
    new_client = await create_membership_subscription(db, sc.person.id, sc.plan.id, session_id=sc.session.id, commit=False)
    assert old_client.session_id is None
    assert new_client.session_id == sc.session.id and new_client.end_at == sc.session.end_at


@pytest.mark.parametrize("invalid", ["past", "canceled", "displaced", "full", "duplicate", "unpaid"])
async def test_failed_purchase_is_atomic(db, invalid):
    sc = await scenario(db, bikes=1)
    if invalid == "past":
        sc.session.start_at = datetime.now(timezone.utc) - timedelta(hours=1)
    elif invalid in ("canceled", "displaced"):
        sc.session.status = invalid
    elif invalid in ("full", "duplicate"):
        occupant = sc.person if invalid == "duplicate" else await make_person(db, "Other person")
        db.add(Reservation(person_id=occupant.id, session_id=sc.session.id, seat_id=sc.seats[0].id, status="reserved", source="manual"))
    await db.flush()
    before = await db.scalar(select(func.count()).select_from(MembershipSubscription))
    with pytest.raises(ValueError):
        async with db.begin_nested():
            await purchase_day_pass(db, plan_id=sc.plan.id, session_id=sc.session.id, person_id=sc.person.id,
                                    payment_status="PENDING" if invalid == "unpaid" else "COMPLETED")
    assert await db.scalar(select(func.count()).select_from(MembershipSubscription)) == before
    assert await db.scalar(select(func.count()).select_from(Payment)) == 0


async def test_expiry_is_exact_without_scheduler_and_does_not_touch_legacy(db):
    sc = await scenario(db)
    purchase = await purchase_day_pass(db, plan_id=sc.plan.id, session_id=sc.session.id, person_id=sc.person.id)
    end = sc.session.end_at
    legacy = MembershipSubscription(person_id=sc.person.id, plan_id=sc.plan.id,
        start_at=end - timedelta(days=3), end_at=end - timedelta(days=1), status="active")
    db.add(legacy)
    await db.flush()
    assert effective_status(purchase.subscription, end - timedelta(seconds=1)) == "active"
    assert effective_status(purchase.subscription, end) == "expired"
    assert _subscription_to_data(purchase.subscription, end).status == "expired"
    assert purchase.subscription.status == "active"  # reads do not require a sweep
    assert await expire_day_passes(db, end) == 1
    assert await expire_day_passes(db, end) == 0
    await db.refresh(legacy)
    assert legacy.status == "active" and legacy.session_id is None
    assert legacy.end_at == end - timedelta(days=1)


@pytest.mark.parametrize("state", ["pending", "canceled", "expired"])
def test_non_active_status_is_not_revived(state):
    sub = SimpleNamespace(status=state, end_at=datetime.now(timezone.utc) + timedelta(days=1))
    assert effective_status(sub) == state


async def test_expired_same_day_is_not_active_in_queries_or_member_summary(db):
    from app.crud.membersCrud import get_member_by_id
    sc = await scenario(db)
    end = datetime.now(timezone.utc) - timedelta(minutes=1)
    sub = MembershipSubscription(person_id=sc.person.id, plan_id=sc.plan.id,
        start_at=end - timedelta(hours=1), end_at=end, status="active")
    db.add(sub)
    await db.flush()
    assert await get_member_active_subscription(db, sc.person.id) is None
    active = await get_membership_subscriptions(db, status="active")
    assert sub.id not in [item.id for item in active]
    expired = await get_membership_subscriptions(db, status="expired")
    assert sub.id in [item.id for item in expired]
    member = await get_member_by_id(db, sc.person.id)
    assert member.active_membership.status == "expired"


@pytest.mark.parametrize("unit,value", [("day", 5), ("week", 1), ("month", 1)])
async def test_new_packages_use_calendar_dates_without_an_extra_day(db, unit, value):
    sc = await scenario(db)
    sc.plan.duration_unit, sc.plan.duration_value = unit, value
    await db.flush()
    start = datetime.now(timezone.utc)
    sub = await create_membership_subscription(db, sc.person.id, sc.plan.id, start_at=start, commit=False)
    assert sub.session_id is None
    from app.crud.memberships.calendar import period
    from app.core.timezones import to_local
    assert (sub.start_at, sub.end_at) == period(sc.plan, to_local(start).date())
    assert sub.period_rule_version == 2
