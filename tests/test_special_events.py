"""Special events over a booked regular class: takeover, inventory, follow-up and restoration.

Runs against the rolled-back ``db`` fixture with real rows (see special_event_factories), so the
CHECK/EXCLUDE constraints and partial unique indexes from the migration are exercised too.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.timezones import STUDIO_TIMEZONE, local_datetime, to_local
from app.crud import reservationsCrud
from app.crud.classSessionCrud import generate_sessions_from_template
from app.crud.special_events import events as events_crud
from app.crud.special_events import orders as orders_crud
from app.crud.special_events import takeover as takeover_crud
from app.crud.special_events.common import SpecialEventError
from app.crud.standing_bookings.materialization import materialize_standing_bookings
from app.models import (
    ClassSession,
    MembershipSubscription,
    Reservation,
    StandingBooking,
)
from app.models.specialEventModel import (
    SpecialEvent,
    SpecialEventDisplacement,
    SpecialEventOrder,
    SpecialEventTakeover,
)
from tests.special_event_factories import (
    book_member,
    make_account,
    make_event,
    make_person,
    make_subscription,
    regular_class_scenario,
)


async def _take_over(db, event, actor=None):
    preview = await takeover_crud.preview_takeover(db, event)
    return await takeover_crud.apply_takeover(
        db, event, actor_account_id=actor, session_ids=preview.pending_session_ids
    )


async def _publish(db, event, actor=None):
    """Even an empty regular class in the slot needs an explicit takeover before publishing."""
    await _take_over(db, event, actor)
    return await events_crud.publish_event(db, event, actor_account_id=actor)


# ---------------------------------------------------------------------------------------------
# Creating the event over a full regular class
# ---------------------------------------------------------------------------------------------
async def test_event_over_full_regular_class_previews_applies_and_publishes(db):
    sc = await regular_class_scenario(db, booked=14)  # every bike taken in the regular class
    actor = await make_account(db)
    event = await make_event(db, sc, actor_account_id=actor.id)

    assert event.status == "draft"
    # Publishing is blocked while the regular class still holds the slot.
    blockers = await events_crud.publish_blockers(db, event)
    assert any("clases regulares sin sustituir" in b for b in blockers)

    preview = await takeover_crud.preview_takeover(db, event)
    assert preview.pending_session_ids == [sc["session"].id]
    assert preview.affected_clients == 14
    assert "14 cliente(s)" in preview.summary and "No se les envía ningún mensaje" in preview.summary

    result = await takeover_crud.apply_takeover(
        db, event, actor_account_id=actor.id, session_ids=preview.pending_session_ids
    )
    assert result.sessions_taken == 1 and result.clients_displaced == 14

    await db.refresh(sc["session"])
    assert sc["session"].status == "displaced"
    statuses = (
        await db.execute(
            select(Reservation.status).where(Reservation.session_id == sc["session"].id)
        )
    ).scalars().all()
    assert statuses == ["displaced"] * 14  # kept, not deleted

    takeover = (
        await db.execute(select(SpecialEventTakeover).where(SpecialEventTakeover.event_id == event.id))
    ).scalar_one()
    assert takeover.applied_by == actor.id and takeover.applied_at is not None
    assert takeover.previous_status == "scheduled"

    published = await events_crud.publish_event(db, event, actor_account_id=actor.id)
    assert published.status == "published" and published.published_by == actor.id


async def test_event_times_are_converted_from_local_wall_clock(db):
    sc = await regular_class_scenario(db, booked=0)
    event = await make_event(db, sc)
    expected = local_datetime(sc["day"], time(19, 0), STUDIO_TIMEZONE)
    assert event.start_at == expected
    # America/Mexico_City is UTC-6 (no DST since 2022): 19:00 local is 01:00 UTC next day.
    assert event.start_at.astimezone(timezone.utc).hour == 1
    assert to_local(event.end_at).strftime("%H:%M") == "21:00"
    assert event.timezone == "America/Mexico_City"


async def test_preview_must_match_what_is_applied(db):
    sc = await regular_class_scenario(db, booked=2)
    event = await make_event(db, sc)
    with pytest.raises(SpecialEventError, match="cambiaron desde la vista previa"):
        await takeover_crud.apply_takeover(db, event, actor_account_id=None, session_ids=[])
    with pytest.raises(SpecialEventError, match="cambiaron desde la vista previa"):
        await takeover_crud.apply_takeover(
            db, event, actor_account_id=None, session_ids=[sc["session"].id + 999]
        )


async def test_event_only_takes_classes_it_overlaps(db):
    sc = await regular_class_scenario(db, booked=1)
    # A 20:00 class the same day: a 19:00-20:00 event must not touch it.
    later = ClassSession(
        class_type_id=sc["ctype"].id,
        venue_id=sc["venue"].id,
        template_id=None,
        name="Spinning 8:00 pm",
        start_at=local_datetime(sc["day"], time(20, 0)),
        end_at=local_datetime(sc["day"], time(21, 0)),
        capacity=14,
        status="scheduled",
    )
    db.add(later)
    await db.flush()
    event = await make_event(db, sc, end=time(20, 0))
    preview = await takeover_crud.preview_takeover(db, event)
    assert preview.pending_session_ids == [sc["session"].id]

    # A longer event (until 21:00) also takes the 20:00 class.
    await events_crud.update_event(db, event, actor_account_id=None, end_time=time(21, 0))
    preview = await takeover_crud.preview_takeover(db, event)
    assert preview.pending_session_ids == [sc["session"].id, later.id]


# ---------------------------------------------------------------------------------------------
# Inventory: own seats, independent of the displaced class
# ---------------------------------------------------------------------------------------------
async def test_bike_taken_in_displaced_class_can_be_sold_for_the_event(db):
    sc = await regular_class_scenario(db, booked=3)
    event = await make_event(db, sc)
    await _take_over(db, event)
    await events_crud.publish_event(db, event, actor_account_id=None)

    hold = await orders_crud.create_hold(
        db, event.id, participant_name="Cliente Evento", participant_phone="8719990001"
    )
    bike_1 = sc["seats"][0]
    assert hold.order.seat_id == bike_1.id  # first bike of the event, booked in the regular class
    displaced = (
        await db.execute(
            select(Reservation).where(
                Reservation.session_id == sc["session"].id, Reservation.seat_id == bike_1.id
            )
        )
    ).scalar_one()
    assert displaced.status == "displaced"  # the regular booking keeps its bike, history intact


async def test_member_with_regular_booking_can_buy_event_at_same_time(db):
    sc = await regular_class_scenario(db, booked=1)
    person, sub, _sb, reservation = sc["members"][0]
    event = await make_event(db, sc)
    await _take_over(db, event)
    await events_crud.publish_event(db, event, actor_account_id=None)

    subs_before = (
        await db.execute(
            select(func.count(MembershipSubscription.id)).where(
                MembershipSubscription.person_id == person.id
            )
        )
    ).scalar()
    end_before = sub.end_at

    hold = await orders_crud.create_hold(
        db, event.id, participant_name=person.full_name, participant_phone=person.phone_number
    )
    assert hold.reused is False
    assert hold.order.person_id == person.id  # linked to the existing member by phone

    # Buying the event neither creates nor changes memberships, nor books regular classes.
    subs_after = (
        await db.execute(
            select(func.count(MembershipSubscription.id)).where(
                MembershipSubscription.person_id == person.id
            )
        )
    ).scalar()
    await db.refresh(sub)
    assert subs_after == subs_before and sub.end_at == end_before and sub.status == "active"
    await db.refresh(reservation)
    assert reservation.status == "displaced"


async def test_no_oversell_and_no_duplicate_participant(db):
    sc = await regular_class_scenario(db, booked=0)
    event = await make_event(db, sc, capacity=2)
    await _publish(db, event)

    first = await orders_crud.create_hold(db, event.id, participant_name="Ana", participant_phone="8711112222")
    second = await orders_crud.create_hold(db, event.id, participant_name="Beto", participant_phone="8713334444")
    assert first.order.seat_id != second.order.seat_id

    with pytest.raises(SpecialEventError, match="no hay lugares"):
        await orders_crud.create_hold(db, event.id, participant_name="Caro", participant_phone="8715556666")

    # The same participant again (WhatsApp-style number) reuses the hold: no second order/link.
    again = await orders_crud.create_hold(
        db, event.id, participant_name="Ana", participant_phone="+52 1 871 111 2222"
    )
    assert again.reused is True and again.order.id == first.order.id
    count = (
        await db.execute(select(func.count(SpecialEventOrder.id)).where(SpecialEventOrder.event_id == event.id))
    ).scalar()
    assert count == 2


async def test_database_refuses_same_bike_or_participant_twice(db):
    sc = await regular_class_scenario(db, booked=0)
    event = await make_event(db, sc)
    seat = sc["seats"][0]

    def order(key, seat_id, ref):
        return SpecialEventOrder(
            event_id=event.id,
            participant_name="X",
            participant_key=key,
            seat_id=seat_id,
            status="paid",
            amount=Decimal("500"),
            external_reference=ref,
        )

    db.add(order("1111111111", seat.id, "event-a"))
    await db.flush()
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            db.add(order("2222222222", seat.id, "event-b"))  # same bike
            await db.flush()
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            db.add(order("1111111111", sc["seats"][1].id, "event-c"))  # same participant
            await db.flush()
    # A canceled order frees both.
    async with db.begin_nested():
        db.add(
            SpecialEventOrder(
                event_id=event.id, participant_name="Y", participant_key="1111111111",
                seat_id=seat.id, status="canceled", amount=Decimal("500"),
                external_reference="event-d",
            )
        )
        await db.flush()


async def test_two_live_events_cannot_share_venue_or_instructor_time(db):
    sc = await regular_class_scenario(db, booked=0)
    first = await make_event(db, sc)
    with pytest.raises(SpecialEventError, match="Dos eventos activos"):
        await make_event(db, sc, start=time(20, 0), end=time(22, 0), name="Otro")

    # The EXCLUDE constraint is the backstop if the check were bypassed.
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            db.add(
                SpecialEvent(
                    name="Directo", venue_id=sc["venue"].id, start_at=first.start_at,
                    end_at=first.end_at, price=Decimal("1"), status="draft",
                    timezone="America/Mexico_City",
                )
            )
            await db.flush()

    # A canceled event releases the slot.
    await events_crud.cancel_event(db, first, actor_account_id=None, reason="prueba")
    again = await make_event(db, sc, name="Reprogramado")
    assert again.id != first.id


async def test_hold_expiry_releases_the_seat(db):
    sc = await regular_class_scenario(db, booked=0)
    event = await make_event(db, sc, capacity=1)
    await _publish(db, event)
    hold = await orders_crud.create_hold(db, event.id, participant_name="Ana", participant_phone="8711112222")
    hold.order.hold_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.flush()

    avail = await events_crud.availability(db, event)
    assert avail.available == 1  # a lapsed hold no longer counts, even before the sweep

    other = await orders_crud.create_hold(db, event.id, participant_name="Beto", participant_phone="8713334444")
    await db.refresh(hold.order)
    assert hold.order.status == "expired"
    assert other.order.seat_id == hold.order.seat_id


# ---------------------------------------------------------------------------------------------
# Regular rules stay as they were
# ---------------------------------------------------------------------------------------------
async def test_regular_booking_rules_unchanged(db):
    sc = await regular_class_scenario(db, booked=1)
    person, *_ = sc["members"][0]

    with pytest.raises(ValueError, match="already has an active reservation"):
        await reservationsCrud.create_reservation(
            db, session_id=sc["session"].id, person_id=person.id, commit=False
        )
    stranger = await make_person(db, "Nuevo")
    with pytest.raises(ValueError, match="already reserved"):
        await reservationsCrud.create_reservation(
            db, session_id=sc["session"].id, person_id=stranger.id,
            seat_id=sc["seats"][0].id, commit=False,
        )

    event = await make_event(db, sc)
    await _take_over(db, event)
    # Nobody can book the displaced class through the regular path.
    with pytest.raises(ValueError, match="status displaced"):
        await reservationsCrud.create_reservation(
            db, session_id=sc["session"].id, person_id=stranger.id, commit=False
        )
    upcoming = await reservationsCrud.get_available_sessions(
        db, start_date=datetime.now(timezone.utc), end_date=datetime.now(timezone.utc) + timedelta(days=60)
    )
    assert sc["session"].id not in {s.id for s in upcoming}


async def test_session_generation_does_not_restore_or_duplicate_displaced_slot(db):
    sc = await regular_class_scenario(db, booked=2)
    event = await make_event(db, sc)
    await _take_over(db, event)

    created = await generate_sessions_from_template(
        db, sc["template"].id, sc["day"], sc["day"], commit=False
    )
    assert created == []  # the displaced class still occupies the template/date

    await materialize_standing_bookings(db, window_weeks=12)
    reserved = (
        await db.execute(
            select(func.count(Reservation.id)).where(
                Reservation.session_id == sc["session"].id, Reservation.status == "reserved"
            )
        )
    ).scalar()
    assert reserved == 0

    # A member who enrolls AFTER the takeover is recorded as displaced, not booked.
    late_person = await make_person(db, "Inscrito Tarde", "8719998888")
    late_sub = await make_subscription(db, late_person)
    db.add(
        StandingBooking(
            person_id=late_person.id, subscription_id=late_sub.id, template_id=sc["template"].id,
            seat_id=sc["seats"][5].id, start_date=sc["day"] - timedelta(days=1),
            end_date=sc["day"] + timedelta(days=30), status="active",
        )
    )
    await db.flush()
    stats = await materialize_standing_bookings(db, window_weeks=12)
    assert stats.get("recorded_displaced") == 1
    late = (
        await db.execute(
            select(SpecialEventDisplacement).where(SpecialEventDisplacement.person_id == late_person.id)
        )
    ).scalar_one()
    assert late.follow_up_status == "pending" and "posterior a la sustitución" in late.reason
    # Re-running is idempotent.
    stats = await materialize_standing_bookings(db, window_weeks=12)
    assert not stats.get("recorded_displaced")


# ---------------------------------------------------------------------------------------------
# Displaced clients: history, membership, follow-up
# ---------------------------------------------------------------------------------------------
async def test_displaced_clients_keep_membership_and_history(db):
    sc = await regular_class_scenario(db, booked=3)
    event = await make_event(db, sc)
    before = {
        sub.id: (sub.end_at, sub.status) for _p, sub, _sb, _r in sc["members"]
    }
    await _take_over(db, event)

    rows = await takeover_crud.list_displacements(db, event_id=event.id)
    assert len(rows) == 3
    for row in rows:
        assert row.follow_up_status == "pending"
        assert row.reservation_status == "displaced"
        assert row.seat_label and row.plan_name
        assert "Clase sustituida por el evento especial" in row.reason
    for person, sub, sb, reservation in sc["members"]:
        await db.refresh(sub)
        assert (sub.end_at, sub.status) == before[sub.id]  # no automatic extension
        await db.refresh(reservation)
        assert reservation.seat_id is not None and reservation.source == "standing"

    from app.crud import dashboard_metrics
    from app.services import fitness_estimation_service, segmentation_service

    # A displaced booking is neither attendance nor an absence anywhere metrics are computed.
    assert "displaced" not in fitness_estimation_service.ATTENDANCE_STATUSES
    assert "displaced" not in segmentation_service._ATTENDANCE_STATUSES
    assert "displaced" not in dashboard_metrics._OCCUPIED_RESERVATION_STATUSES


async def test_follow_up_records_notice_and_compensation(db):
    sc = await regular_class_scenario(db, booked=1)
    actor = await make_account(db)
    event = await make_event(db, sc)
    await _take_over(db, event)
    row = (await takeover_crud.list_displacements(db, event_id=event.id))[0]

    d = await takeover_crud.update_follow_up(
        db, row.id, actor_account_id=actor.id, notified=True,
        notification_channel="whatsapp", notification_notes="Se le escribió",
    )
    assert d.follow_up_status == "notified" and d.notified_by == actor.id and d.notified_at

    with pytest.raises(SpecialEventError, match="tipo de compensación"):
        await takeover_crud.update_follow_up(db, row.id, actor_account_id=actor.id, resolved=True)

    d = await takeover_crud.update_follow_up(
        db, row.id, actor_account_id=actor.id, compensation_type="membership_extension",
        compensation_notes="+1 día", resolved=True,
    )
    assert d.follow_up_status == "resolved" and d.resolved_by == actor.id

    with pytest.raises(SpecialEventError, match="desconocido"):
        await takeover_crud.update_follow_up(db, row.id, actor_account_id=actor.id, compensation_type="gratis")


# ---------------------------------------------------------------------------------------------
# Cancellation and controlled restoration
# ---------------------------------------------------------------------------------------------
async def test_cancel_then_restore_respects_registered_compensations(db):
    sc = await regular_class_scenario(db, booked=3)
    actor = await make_account(db)
    event = await make_event(db, sc)
    await _take_over(db, event, actor.id)
    await events_crud.publish_event(db, event, actor_account_id=actor.id)
    paid = await orders_crud.add_manual_participant(
        db, event.id, actor_account_id=actor.id, participant_name="Pagó Efectivo",
        participant_phone="8710001111", payment_method="cash",
    )
    hold = await orders_crud.create_hold(db, event.id, participant_name="Apartó", participant_phone="8710002222")

    rows = {r.person_name: r for r in await takeover_crud.list_displacements(db, event_id=event.id)}
    compensated, notified, untouched = rows["Socio 1"], rows["Socio 2"], rows["Socio 3"]
    await takeover_crud.update_follow_up(
        db, compensated.id, actor_account_id=actor.id, compensation_type="make_up_class", resolved=True
    )
    await takeover_crud.update_follow_up(db, notified.id, actor_account_id=actor.id, notified=True)

    # While the event is published, the regular schedule cannot be given back.
    with pytest.raises(SpecialEventError, match="cancelado o en borrador"):
        await takeover_crud.apply_restore(db, event, actor_account_id=actor.id)

    summary = await events_crud.cancel_event(db, event, actor_account_id=actor.id, reason="Lluvia")
    assert summary.holds_canceled == 1 and summary.to_refund_order_ids == [paid.id]
    await db.refresh(hold.order)
    assert hold.order.status == "canceled"
    await db.refresh(sc["session"])
    assert sc["session"].status == "displaced"  # nothing restored implicitly

    preview = await takeover_crud.preview_restore(db, event)
    decisions = {d.person_name: d for d in preview.decisions}
    assert decisions["Socio 1"].restore is False and "compensación" in decisions["Socio 1"].reason
    assert decisions["Socio 2"].restore is True and "avísale" in decisions["Socio 2"].reason
    assert decisions["Socio 3"].restore is True

    result = await takeover_crud.apply_restore(db, event, actor_account_id=actor.id)
    assert (result.sessions_restored, result.reservations_restored, result.reservations_kept) == (1, 2, 1)
    await db.refresh(sc["session"])
    assert sc["session"].status == "scheduled"
    statuses = {
        r.person_id: r.status
        for r in (
            await db.execute(select(Reservation).where(Reservation.session_id == sc["session"].id))
        ).scalars()
    }
    p1, p2, p3 = (m[0].id for m in sc["members"])
    assert statuses == {p1: "displaced", p2: "reserved", p3: "reserved"}
    takeover = (
        await db.execute(select(SpecialEventTakeover).where(SpecialEventTakeover.event_id == event.id))
    ).scalar_one()
    assert takeover.status == "restored" and takeover.restored_by == actor.id


async def test_schedule_cannot_move_after_takeover_or_sales(db):
    sc = await regular_class_scenario(db, booked=1)
    event = await make_event(db, sc)
    await _take_over(db, event)
    with pytest.raises(SpecialEventError, match="Restituye el horario regular"):
        await events_crud.update_event(db, event, actor_account_id=None, start_time=time(18, 0))
    # Non-schedule edits are fine.
    await events_crud.update_event(db, event, actor_account_id=None, description="Trae disfraz")
    assert event.description == "Trae disfraz"


async def test_published_event_cannot_move_onto_untaken_regular_classes(db):
    sc = await regular_class_scenario(db, booked=1)  # regular class 19:00-20:00
    event = await make_event(db, sc, start=time(20, 0), end=time(21, 0))
    await events_crud.publish_event(db, event, actor_account_id=None)  # no overlap: fine
    with pytest.raises(SpecialEventError, match="sin sustituir"):
        await events_crud.update_event(db, event, actor_account_id=None, start_time=time(19, 0))


async def test_capacity_cannot_drop_below_sold_and_requires_data_to_publish(db):
    sc = await regular_class_scenario(db, booked=0)
    event = await make_event(db, sc, end=None, capacity=None)
    blockers = await events_crud.publish_blockers(db, event)
    assert any("hora de fin" in b for b in blockers) and any("cupo" in b for b in blockers)
    with pytest.raises(SpecialEventError):
        await events_crud.publish_event(db, event, actor_account_id=None)

    await events_crud.update_event(db, event, actor_account_id=None, end_time=time(21, 0), capacity=3)
    await _publish(db, event)
    await orders_crud.add_manual_participant(
        db, event.id, actor_account_id=None, participant_name="Uno", participant_phone="8711000001",
        payment_method="courtesy",
    )
    await orders_crud.add_manual_participant(
        db, event.id, actor_account_id=None, participant_name="Dos", participant_phone="8711000002",
        payment_method="cash",
    )
    with pytest.raises(SpecialEventError, match="no puede ser menor"):
        await events_crud.update_event(db, event, actor_account_id=None, capacity=1)
