"""Calendar entitlements, independent seat capacity and server-owned package explanations."""
import asyncio
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select

from app.core.timezones import local_date, local_datetime
from app.crud.chatbotPendingCrud import upsert_pending
from app.crud.memberships.coverage import package_facts, subscription_coverage
from app.crud.memberships.enrollment import create_member_enrollment_with_standing_booking, renew_subscription_with_standing_booking
from app.crud.memberships.plans import create_membership_plan, update_membership_plan
from app.crud.memberships.utils import _calculate_subscription_end
from app.crud.reservationsCrud import cancel_reservation, create_reservation
from app.models import ClassSession, ClassTemplate, ClassType, MembershipPlan, MembershipSubscription, People, PersonRole, Reservation, Seat, StandingBooking, Venue
from app.services.chatbot.tools import ChatbotContext, _execute_pending, build_tools
from app.services.chatbot.verification import finalize_reply
from tests.special_event_factories import make_class_type, make_person, make_session, make_template, make_venue


async def group(db, days=5, *, unit="week", mode="fixed_schedule", limit=14):
    venue, seats = await make_venue(db)
    ctype = await make_class_type(db)
    templates = [await make_template(db, venue, ctype, at=time(20), weekday=d % 7) for d in range(1, days + 1)]
    plan = MembershipPlan(name="Paquete test", price=Decimal("600"), duration_value=1, duration_unit=unit,
                          fixed_time_slot=mode == "fixed_schedule", plan_type=mode, class_limit=limit, is_active=True)
    db.add(plan)
    await db.flush()
    day = local_date(datetime.now(timezone.utc)) + timedelta(days=20)
    day += timedelta(days=(-day.weekday()) % 7)
    start = local_datetime(day, time(8)).astimezone(timezone.utc)
    return SimpleNamespace(plan=plan, templates=templates, start=start, day=day, seats=seats)


@pytest.mark.parametrize("days", [5, 7])
@pytest.mark.parametrize("hour", [8, 22])
async def test_weekly_counts_every_day_in_group_and_not_an_eighth_day(db, days, hour):
    sc = await group(db, days)
    sc.start = local_datetime(sc.day, time(hour)).astimezone(timezone.utc)
    person, sub, _, _, _, stats = await create_member_enrollment_with_standing_booking(
        db, "Weekly member", sc.plan.id, template_id=sc.templates[2].id, start_at=sc.start, commit=False,
    )
    coverage = await subscription_coverage(db, sub)
    assert coverage.included_classes == days
    assert stats["included_classes"] == days
    rows = (await db.execute(select(Reservation, ClassSession).join(ClassSession).where(
        Reservation.person_id == person.id,
    ))).all()
    assert len(rows) == days
    assert all(r.subscription_id == sub.id for r, _ in rows)
    assert {local_date(s.start_at).weekday() for _, s in rows} == set(range(days))
    assert sub.end_at == sc.start + timedelta(days=7)
    eighth = await make_session(db, sc.templates[0], sc.day + timedelta(days=14))
    with pytest.raises(ValueError, match="no cubre"):
        await create_reservation(db, session_id=eighth.id, person_id=person.id, commit=False)


async def test_monthly_covers_full_calendar_even_when_wednesday_selected(db):
    sc = await group(db, unit="month")
    person, sub, _, _, _, stats = await create_member_enrollment_with_standing_booking(
        db, "Ana Luisa", sc.plan.id, template_id=sc.templates[2].id, start_at=sc.start, commit=False,
    )
    dates = []
    day = sc.day
    while day <= local_date(sub.end_at):
        if day.weekday() < 5:
            dates.append(day)
        day += timedelta(days=1)
    rows = (await db.execute(select(ClassSession).join(Reservation).where(Reservation.person_id == person.id))).scalars().all()
    assert sorted(local_date(s.start_at) for s in rows) == dates
    assert stats["included_classes"] == len(dates) > 14
    facts = await package_facts(db, plan_id=sc.plan.id, template_id=sc.templates[2].id, subscription=sub)
    assert "Lunes a Viernes a las 20:00" in facts
    assert "4–5" not in facts
    assert sc.plan.class_limit == 14  # no rewrite of historical plan data is necessary


async def test_count_excludes_canceled_or_displaced_occurrences(db):
    sc = await group(db)
    canceled = await make_session(db, sc.templates[1], sc.day + timedelta(days=1))
    canceled.status = "canceled"
    displaced = await make_session(db, sc.templates[3], sc.day + timedelta(days=3))
    displaced.status = "displaced"
    person = await make_person(db, "Calendar member")
    sub = MembershipSubscription(person_id=person.id, plan_id=sc.plan.id, start_at=sc.start,
                                  end_at=_calculate_subscription_end(sc.plan, sc.start), status="active")
    db.add(sub)
    await db.flush()
    for t in sc.templates:
        db.add(StandingBooking(person_id=person.id, subscription_id=sub.id, template_id=t.id,
                              start_date=sc.day, end_date=local_date(sub.end_at), status="active"))
    await db.flush()
    assert (await subscription_coverage(db, sub)).included_classes == 3


async def test_fixed_membership_cannot_book_another_hour_or_instructor(db):
    sc = await group(db)
    person, sub, *_ = await create_member_enrollment_with_standing_booking(
        db, "Fixed member", sc.plan.id, template_id=sc.templates[0].id, start_at=sc.start, commit=False,
    )
    template = await make_template(db, await db.get(Venue, sc.templates[0].venue_id),
                                   await db.get(ClassType, sc.templates[0].class_type_id), at=time(19), weekday=1)
    session = await make_session(db, template, sc.day)
    with pytest.raises(ValueError, match="no cubre"):
        await create_reservation(db, session_id=session.id, person_id=person.id, commit=False)


async def open_subscription(db, sc):
    person = await make_person(db, "Open member")
    sub = MembershipSubscription(person_id=person.id, plan_id=sc.plan.id, start_at=sc.start,
                                  end_at=_calculate_subscription_end(sc.plan, sc.start), status="active")
    db.add(sub)
    await db.flush()
    return person, sub


async def test_full_bikes_cannot_be_bypassed_with_a_seatless_package(db):
    sc = await group(db)
    first = await make_session(db, sc.templates[0], sc.day, capacity=100)
    for seat in sc.seats:
        occupant = await make_person(db, f"Occupant {seat.id}")
        db.add(Reservation(person_id=occupant.id, session_id=first.id, seat_id=seat.id, status="reserved"))
    await db.flush()
    with pytest.raises(ValueError, match="bicicleta/asiento"):
        async with db.begin_nested():
            await create_member_enrollment_with_standing_booking(
                db, "Cannot bypass", sc.plan.id, template_id=sc.templates[2].id, start_at=sc.start, commit=False,
            )


async def test_unlimited_membership_ignores_legacy_limits_and_respects_capacity(db):
    sc = await group(db, mode="flexible", limit=1)
    person, sub = await open_subscription(db, sc)
    for index in range(5):
        session = await make_session(db, sc.templates[index], sc.day + timedelta(days=index))
        reservation = await create_reservation(db, session_id=session.id, person_id=person.id, commit=False)
        assert reservation.subscription_id == sub.id
    assert (await subscription_coverage(db, sub)).included_classes is None
    session = await make_session(db, sc.templates[0], sc.day + timedelta(days=1), capacity=1)
    occupant = await make_person(db, "Occupant")
    db.add(Reservation(person_id=occupant.id, session_id=session.id, status="reserved"))
    await db.flush()
    with pytest.raises(ValueError, match="full capacity"):
        await create_reservation(db, session_id=session.id, person_id=person.id, commit=False)


async def test_credits_are_consumed_and_cancellation_restores_one(db):
    sc = await group(db, mode="credit_pack", limit=1)
    person, sub = await open_subscription(db, sc)
    first = await make_session(db, sc.templates[0], sc.day)
    second = await make_session(db, sc.templates[1], sc.day + timedelta(days=1))
    reservation = await create_reservation(db, session_id=first.id, person_id=person.id, commit=False)
    assert reservation.subscription_id == sub.id
    with pytest.raises(ValueError, match="créditos"):
        await create_reservation(db, session_id=second.id, person_id=person.id, commit=False)
    await cancel_reservation(db, reservation.id, commit=False)
    replacement = await create_reservation(db, session_id=first.id, person_id=person.id, commit=False)
    assert replacement.id == reservation.id
    assert await db.scalar(select(func.count(Reservation.id)).where(Reservation.person_id == person.id)) == 1


@pytest.mark.parametrize("status", ["pending", "canceled", "expired"])
async def test_nonactive_membership_cannot_book(db, status):
    sc = await group(db, mode="flexible")
    person, sub = await open_subscription(db, sc)
    sub.status = status
    session = await make_session(db, sc.templates[0], sc.day)
    with pytest.raises(ValueError, match="no cubre"):
        await create_reservation(db, session_id=session.id, person_id=person.id, commit=False)


async def test_renewal_keeps_current_coverage_and_starts_at_previous_end(db):
    sc = await group(db)
    person, current, *_ = await create_member_enrollment_with_standing_booking(
        db, "Renew member", sc.plan.id, template_id=sc.templates[0].id, start_at=sc.start, commit=False,
    )
    new, _, _, _, stats = await renew_subscription_with_standing_booking(
        db, person.id, sc.plan.id, template_id=sc.templates[2].id, commit=False,
    )
    assert current.status == "active"
    assert new.start_at == current.end_at
    assert stats["included_classes"] == 5


async def test_ana_luisa_stale_pending_summary_cannot_narrow_coverage(db):
    sc = await group(db, unit="month")
    await upsert_pending(db, conversation_id=463, action_type="buy_package", member_id=None,
        payload={"plan_id": sc.plan.id, "template_id": sc.templates[2].id}, status="awaiting_payment",
        mp_init_point="https://www.mercadopago.com.mx/verified", summary="Solo miércoles: 4–5 clases")
    ctx = ChatbotContext(db, 463, None, None, user_text="Entonces mi mes empieza desde el miércoles o desde hoy?")
    await finalize_reply(ctx, kind="information", text="Cubre solo los miércoles, 4–5 clases")
    assert "Lunes a Viernes a las 20:00" in ctx.validated_reply
    assert "al acreditarse el pago" in ctx.validated_reply
    assert "4–5" not in ctx.validated_reply
    await finalize_reply(ctx, kind="purchase", text="Solo miércoles")
    assert "Lunes a Viernes" in ctx.validated_reply and ctx.payment_link.url.endswith("/verified")


async def test_weekly_catalog_returns_group_ids_and_all_days(db):
    sc = await group(db, days=7)
    ctx = ChatbotContext(db, 463, None, None)
    tool = next(t for t in build_tools(ctx) if t.name == "get_weekly_schedule")
    result = await tool.ainvoke({})
    assert f"template_id={sc.templates[-1].id}" in result or f"template_id={sc.templates[0].id}" in result
    assert "Lunes a Domingo a las 20:00" in result


@pytest.mark.parametrize("channel", ["pos_new", "pos_renewal", "chatbot_new", "chatbot_member"])
async def test_package_sales_channels_use_the_full_group(db, channel):
    from app.crud.pos.sales import SaleLineInput, SalePaymentInput, create_sale

    sc = await group(db)
    member = await make_person(db, "Existing package member")
    if channel.startswith("pos_"):
        sale = await create_sale(db, line_items=[SaleLineInput(
            line_type="membership_new" if channel == "pos_new" else "membership_renewal",
            full_name="Package buyer", member_id=member.id, plan_id=sc.plan.id,
            template_id=sc.templates[2].id,
        )], tenders=[SalePaymentInput("card", 600)], commit=False)
        sub = await db.get(MembershipSubscription, sale.line_items[0].subscription_id)
    else:
        pending = SimpleNamespace(action_type="buy_package", member_id=member.id if channel.endswith("member") else None,
                                  payload={"plan_id": sc.plan.id, "template_id": sc.templates[2].id,
                                           "full_name": "Ana Luisa", "amount": "600"})
        result = await _execute_pending(db, pending, payment_method="mercadopago", provider_payment_id=channel)
        assert "Lunes a Viernes" in result and "Clases incluidas: 5" in result
        sub = await db.scalar(select(MembershipSubscription).where(MembershipSubscription.plan_id == sc.plan.id))
    assert (await subscription_coverage(db, sub)).included_classes == 5
    assert await db.scalar(select(func.count(Reservation.id)).where(Reservation.subscription_id == sub.id)) == 5


async def test_open_membership_checkout_needs_no_template_or_recurring_bookings(db):
    sc = await group(db, mode="flexible")
    pending = SimpleNamespace(action_type="buy_package", member_id=None,
                              payload={"plan_id": sc.plan.id, "full_name": "Open customer", "amount": "600"})
    result = await _execute_pending(db, pending)
    assert "sin límite de clases" in result
    assert "no reserva lugares automáticamente" in result
    assert await db.scalar(select(func.count(StandingBooking.id)).where(StandingBooking.template_id.in_(
        [t.id for t in sc.templates]))) == 0


async def test_plan_configuration_derives_fixed_and_unlimited_limits(db):
    for kind in ("fixed_schedule", "flexible"):
        plan = await create_membership_plan(db, "Plan", 100, 1, "week", plan_type=kind, class_limit=14,
                                            max_sessions_per_day=1, max_sessions_per_week=1)
        assert plan.class_limit is None
        assert plan.max_sessions_per_day is None and plan.max_sessions_per_week is None
        assert plan.fixed_time_slot == (kind == "fixed_schedule")
    with pytest.raises(ValueError, match="mayor que cero"):
        await create_membership_plan(db, "Bad credits", 100, 1, "month", plan_type="credit_pack")


async def test_partial_plan_update_preserves_existing_values(db):
    sc = await group(db, mode="credit_pack", limit=10)
    updated = await update_membership_plan(db, sc.plan.id, name="Renamed")
    assert updated.class_limit == 10 and updated.price == Decimal("600")


async def test_one_credit_cannot_be_spent_concurrently_on_two_sessions():
    from app.db.postgresql import SessionLocal

    async with SessionLocal() as setup:
        sc = await group(setup, mode="credit_pack", limit=1)
        person, sub = await open_subscription(setup, sc)
        first = await make_session(setup, sc.templates[0], sc.day)
        second = await make_session(setup, sc.templates[1], sc.day + timedelta(days=1))
        await setup.commit()

    async def book(session_id):
        async with SessionLocal() as connection:
            try:
                result = await create_reservation(connection, session_id=session_id, person_id=person.id, commit=False)
                await connection.commit()
                return result.id
            except ValueError:
                await connection.rollback()
                return None

    try:
        results = await asyncio.wait_for(asyncio.gather(book(first.id), book(second.id)), timeout=10)
        assert sum(result is not None for result in results) == 1
    finally:
        async with SessionLocal() as cleanup:
            await cleanup.execute(delete(Reservation).where(Reservation.person_id == person.id))
            await cleanup.execute(delete(MembershipSubscription).where(MembershipSubscription.id == sub.id))
            await cleanup.execute(delete(PersonRole).where(PersonRole.person_id == person.id))
            await cleanup.execute(delete(People).where(People.id == person.id))
            await cleanup.execute(delete(ClassSession).where(ClassSession.id.in_([first.id, second.id])))
            await cleanup.execute(delete(ClassTemplate).where(ClassTemplate.id.in_([t.id for t in sc.templates])))
            await cleanup.execute(delete(Seat).where(Seat.venue_id == sc.templates[0].venue_id))
            await cleanup.execute(delete(Venue).where(Venue.id == sc.templates[0].venue_id))
            await cleanup.execute(delete(ClassType).where(ClassType.id == sc.templates[0].class_type_id))
            await cleanup.execute(delete(MembershipPlan).where(MembershipPlan.id == sc.plan.id))
            await cleanup.commit()


async def test_current_membership_explanation_uses_its_actual_dates(db):
    sc = await group(db)
    person, sub, *_ = await create_member_enrollment_with_standing_booking(
        db, "Current member", sc.plan.id, template_id=sc.templates[0].id, start_at=sc.start, commit=False,
    )
    ctx = ChatbotContext(db, 999, person.id, None, user_text="Cuántas clases incluye mi membresía?")
    await finalize_reply(ctx, kind="membership_information")
    assert "Clases incluidas: 5" in ctx.validated_reply
    assert "Vigencia:" in ctx.validated_reply
    assert "previstas si se acredita" not in ctx.validated_reply


async def test_timing_excludes_classes_before_payment_in_local_evening(db):
    sc = await group(db)
    sc.start = local_datetime(sc.day, time(22)).astimezone(timezone.utc)
    person, sub, *_ = await create_member_enrollment_with_standing_booking(
        db, "Late buyer", sc.plan.id, template_id=sc.templates[2].id, start_at=sc.start, commit=False,
    )
    starts = (await db.execute(select(ClassSession.start_at).join(Reservation).where(
        Reservation.person_id == person.id,
    ))).scalars().all()
    assert len(starts) == 5
    assert all(sub.start_at <= start < sub.end_at for start in starts)
    assert local_date(max(starts)) == sc.day + timedelta(days=7)
