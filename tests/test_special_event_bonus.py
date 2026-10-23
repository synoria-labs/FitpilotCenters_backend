"""The default compensation for a displaced client: one extra class after the membership ends.

Production's schedule is spinning Monday to Friday at the same hours, each member with a fixed
slot every weekday, so "the class after the last one" is the next weekday at the usual hour:
last class on Tuesday, still active and booked on the Wednesday after.
"""
from __future__ import annotations

from datetime import date, time, timedelta
from typing import Optional

import pytest
from sqlalchemy import select

from app.core.timezones import local_datetime, to_local
from app.crud.special_events import bonus as bonus_crud
from app.crud.special_events import takeover as takeover_crud
from app.crud.special_events.common import SpecialEventError, utcnow
from app.crud.standing_bookings.materialization import materialize_standing_bookings
from app.models import ClassSession, Reservation, StandingBooking
from tests.special_event_factories import (
    make_account,
    make_class_type,
    make_event,
    make_person,
    make_session,
    make_subscription,
    make_template,
    make_venue,
    next_friday,
)
from tests.test_special_events_graphql import _ctx, _run

_END_OF_DAY = time(23, 59, 59)


async def _weekday_scenario(db):
    """Spinning at 19:00 Monday to Friday, generated from today until four weeks after the
    event's Friday (the event takes that Friday's class)."""
    venue, seats = await make_venue(db)
    ctype = await make_class_type(db)
    templates = {weekday: await make_template(db, venue, ctype, weekday=weekday) for weekday in range(1, 6)}
    day = next_friday()
    sessions = {}
    current = date.today()
    while current <= day + timedelta(days=28):
        template = templates.get((current.weekday() + 1) % 7)
        if template is not None:
            sessions[current] = await make_session(db, template, current)
        current += timedelta(days=1)
    return {
        "venue": venue, "seats": seats, "ctype": ctype, "templates": templates,
        "day": day, "sessions": sessions,
    }


async def _member(
    db, sc, name, seat, *, ends: date, standing_until: Optional[date] = None, status: str = "active"
):
    """A member whose membership covers through ``ends``, with a fixed slot every weekday at
    19:00 and booked into the event's Friday class."""
    person = await make_person(db, name)
    sub = await make_subscription(db, person)
    sub.end_at = local_datetime(ends, _END_OF_DAY)
    sub.status = status
    for template in sc["templates"].values():
        db.add(
            StandingBooking(
                person_id=person.id, subscription_id=sub.id, template_id=template.id, seat_id=seat.id,
                start_date=date.today() - timedelta(days=3), end_date=standing_until or ends, status="active",
            )
        )
    db.add(Reservation(
        session_id=sc["sessions"][sc["day"]].id, person_id=person.id, seat_id=seat.id,
        status="reserved", source="standing",
    ))
    await db.flush()
    return person, sub


async def _displaced(db, sc):
    """Take over the Friday class with the event; {client name: follow-up row}."""
    event = await make_event(db, sc)
    preview = await takeover_crud.preview_takeover(db, event)
    await takeover_crud.apply_takeover(
        db, event, actor_account_id=None, session_ids=preview.pending_session_ids
    )
    rows = await takeover_crud.list_displacements(db, event_id=event.id)
    return event, {row.person_name: row for row in rows}


async def _booked_days(db, person, after: date):
    rows = (
        await db.execute(
            select(Reservation.session_id).where(
                Reservation.person_id == person.id, Reservation.status == "reserved"
            )
        )
    ).scalars().all()
    return sorted(
        to_local(s.start_at).date()
        for s in await _sessions(db, rows)
        if to_local(s.start_at).date() > after
    )


async def _sessions(db, ids):
    if not ids:
        return []
    return (await db.execute(select(ClassSession).where(ClassSession.id.in_(ids)))).scalars().all()


# ---------------------------------------------------------------------------------------------
# Where the extra class goes
# ---------------------------------------------------------------------------------------------
async def test_last_class_on_tuesday_means_active_and_booked_the_wednesday_after(db):
    sc = await _weekday_scenario(db)
    actor = await make_account(db)
    tuesday = sc["day"] + timedelta(days=4)
    wednesday = tuesday + timedelta(days=1)
    person, sub = await _member(db, sc, "Socia Martes", sc["seats"][0], ends=tuesday)
    _event, rows = await _displaced(db, sc)
    row = rows["Socia Martes"]

    assert row.bonus_blocker is None
    assert row.bonus_preview == (
        f"mié {wednesday:%d/%m} 19:00 · Bicicleta 1 · vence {wednesday:%d/%m} (antes {tuesday:%d/%m})"
    )

    d = await bonus_crud.apply_bonus_class(db, row.id, actor_account_id=actor.id)
    await db.refresh(sub)
    assert sub.end_at == local_datetime(wednesday, _END_OF_DAY) and sub.status == "active"
    extra = (
        await db.execute(
            select(Reservation).where(
                Reservation.person_id == person.id,
                Reservation.session_id == sc["sessions"][wednesday].id,
            )
        )
    ).scalar_one()
    assert (extra.status, extra.source, extra.seat_id) == ("reserved", "override", sc["seats"][0].id)
    # Exactly one class past the old end.
    assert await _booked_days(db, person, tuesday) == [wednesday]

    assert (d.compensation_type, d.follow_up_status, d.resolved_by) == (
        "membership_extension", "resolved", actor.id
    )
    assert d.compensation_notes == (
        f"Clase extra el mié {wednesday:%d/%m} a las 19:00 en Bicicleta 1. "
        f"Su membresía vence ahora el {wednesday:%d/%m/%Y} (antes el {tuesday:%d/%m/%Y})."
    )
    listed = (await takeover_crud.list_displacements(db, event_id=d.event_id))[0]
    assert (listed.bonus_preview, listed.bonus_blocker) == (None, None)


async def test_a_friday_end_skips_the_weekend(db):
    sc = await _weekday_scenario(db)
    friday = sc["day"] + timedelta(days=7)
    person, sub = await _member(db, sc, "Socio Viernes", sc["seats"][1], ends=friday)
    _event, rows = await _displaced(db, sc)

    await bonus_crud.apply_bonus_class(db, rows["Socio Viernes"].id, actor_account_id=None)
    monday = friday + timedelta(days=3)
    await db.refresh(sub)
    assert sub.end_at == local_datetime(monday, _END_OF_DAY)
    assert await _booked_days(db, person, friday) == [monday]


async def test_a_class_already_booked_past_the_membership_counts_as_the_last_one(db):
    """Production has members whose fixed slot runs a day past the membership: that class is
    theirs already, so the extra one is the next weekday after it."""
    sc = await _weekday_scenario(db)
    thursday, friday = sc["day"] + timedelta(days=6), sc["day"] + timedelta(days=7)
    person, sub = await _member(db, sc, "Socia Desfasada", sc["seats"][2], ends=thursday, standing_until=friday)
    db.add(Reservation(
        session_id=sc["sessions"][friday].id, person_id=person.id, seat_id=sc["seats"][2].id,
        status="reserved", source="standing",
    ))
    await db.flush()
    _event, rows = await _displaced(db, sc)

    await bonus_crud.apply_bonus_class(db, rows["Socia Desfasada"].id, actor_account_id=None)
    monday = friday + timedelta(days=3)
    await db.refresh(sub)
    assert sub.end_at == local_datetime(monday, _END_OF_DAY)
    assert await _booked_days(db, person, thursday) == [friday, monday]


async def test_their_usual_bike_taken_gets_a_free_one(db):
    sc = await _weekday_scenario(db)
    tuesday = sc["day"] + timedelta(days=4)
    wednesday = tuesday + timedelta(days=1)
    await _member(db, sc, "Socia Bici", sc["seats"][0], ends=tuesday)
    other = await make_person(db, "Otra Persona")
    db.add(Reservation(
        session_id=sc["sessions"][wednesday].id, person_id=other.id, seat_id=sc["seats"][0].id,
        status="reserved", source="manual",
    ))
    await db.flush()
    _event, rows = await _displaced(db, sc)

    d = await bonus_crud.apply_bonus_class(db, rows["Socia Bici"].id, actor_account_id=None)
    extra = (
        await db.execute(
            select(Reservation).where(
                Reservation.person_id == rows["Socia Bici"].person_id,
                Reservation.session_id == sc["sessions"][wednesday].id,
            )
        )
    ).scalar_one()
    assert extra.seat_id not in (None, sc["seats"][0].id)
    assert f"mié {wednesday:%d/%m} a las 19:00 en Bicicleta" in d.compensation_notes


async def test_a_full_class_moves_the_extra_one_to_the_next_day(db):
    sc = await _weekday_scenario(db)
    tuesday = sc["day"] + timedelta(days=4)
    wednesday, thursday = tuesday + timedelta(days=1), tuesday + timedelta(days=2)
    person, sub = await _member(db, sc, "Socio Lleno", sc["seats"][0], ends=tuesday)
    other = await make_person(db, "Ocupa El Lugar")
    db.add(Reservation(
        session_id=sc["sessions"][wednesday].id, person_id=other.id, seat_id=sc["seats"][5].id,
        status="reserved", source="manual",
    ))
    sc["sessions"][wednesday].capacity = 1
    await db.flush()
    _event, rows = await _displaced(db, sc)

    assert rows["Socio Lleno"].bonus_preview.startswith(f"jue {thursday:%d/%m} 19:00")
    await bonus_crud.apply_bonus_class(db, rows["Socio Lleno"].id, actor_account_id=None)
    await db.refresh(sub)
    assert sub.end_at == local_datetime(thursday, _END_OF_DAY)
    assert await _booked_days(db, person, tuesday) == [thursday]


async def test_a_membership_that_already_ended_gets_the_next_class_from_today(db):
    """Compensating late: by the time staff apply it, the membership has run out."""
    sc = await _weekday_scenario(db)
    person, sub = await _member(db, sc, "Socia Vencida", sc["seats"][3], ends=sc["day"] + timedelta(days=4))
    _event, rows = await _displaced(db, sc)
    yesterday = date.today() - timedelta(days=1)
    sub.end_at, sub.status = local_datetime(yesterday, _END_OF_DAY), "expired"
    for slot in (await db.execute(select(StandingBooking).where(StandingBooking.subscription_id == sub.id))).scalars():
        slot.end_date = yesterday
    await db.flush()

    await bonus_crud.apply_bonus_class(db, rows["Socia Vencida"].id, actor_account_id=None)
    now = to_local(utcnow())
    expected = now.date()
    while expected.weekday() > 4 or (expected == now.date() and now.time() >= time(19, 0)):
        expected += timedelta(days=1)
    await db.refresh(sub)
    assert sub.end_at == local_datetime(expected, _END_OF_DAY) and sub.status == "active"


async def test_a_class_not_generated_yet_is_generated_and_booked(db):
    """Classes exist only as far as some membership needed them: the last member of a slot
    usually gets an extra day nobody generated (production: 7 pm only until 20/11)."""
    sc = await _weekday_scenario(db)
    last_generated = sc["day"] + timedelta(days=28)  # a Friday
    monday = last_generated + timedelta(days=3)
    person, sub = await _member(db, sc, "Socio Futuro", sc["seats"][4], ends=last_generated)
    _event, rows = await _displaced(db, sc)
    assert rows["Socio Futuro"].bonus_preview.startswith(f"lun {monday:%d/%m} 19:00 · Bicicleta 5")

    d = await bonus_crud.apply_bonus_class(db, rows["Socio Futuro"].id, actor_account_id=None)
    await db.refresh(sub)
    assert sub.end_at == local_datetime(monday, _END_OF_DAY)
    generated = (
        await db.execute(
            select(ClassSession).where(
                ClassSession.template_id == sc["templates"][1].id,
                ClassSession.start_at == local_datetime(monday, time(19, 0)),
            )
        )
    ).scalar_one()
    assert generated.status == "scheduled"
    booked = (
        await db.execute(
            select(Reservation).where(Reservation.person_id == person.id, Reservation.session_id == generated.id)
        )
    ).scalar_one()
    assert (booked.status, booked.seat_id) == ("reserved", sc["seats"][4].id)
    assert f"lun {monday:%d/%m} a las 19:00 en Bicicleta 5" in d.compensation_notes

    # The regular generation later finds the class already there and adds nothing.
    await materialize_standing_bookings(db, start_date=monday, window_weeks=1, subscription_id=sub.id)
    assert await _booked_days(db, person, last_generated) == [monday]


# ---------------------------------------------------------------------------------------------
# What it refuses
# ---------------------------------------------------------------------------------------------
async def test_no_membership_or_a_second_time_is_refused(db):
    sc = await _weekday_scenario(db)
    tuesday = sc["day"] + timedelta(days=4)
    await _member(db, sc, "Con Membresía", sc["seats"][0], ends=tuesday)
    walk_in = await make_person(db, "Sin Membresía")
    db.add(Reservation(
        session_id=sc["sessions"][sc["day"]].id, person_id=walk_in.id, seat_id=sc["seats"][1].id,
        status="reserved", source="manual",
    ))
    await db.flush()
    _event, rows = await _displaced(db, sc)

    assert rows["Sin Membresía"].bonus_preview is None
    assert "No tiene membresía" in rows["Sin Membresía"].bonus_blocker
    with pytest.raises(SpecialEventError, match="No tiene membresía"):
        await bonus_crud.apply_bonus_class(db, rows["Sin Membresía"].id, actor_account_id=None)

    await bonus_crud.apply_bonus_class(db, rows["Con Membresía"].id, actor_account_id=None)
    with pytest.raises(SpecialEventError, match="Ya tiene una compensación"):
        await bonus_crud.apply_bonus_class(db, rows["Con Membresía"].id, actor_account_id=None)


async def test_the_follow_up_dialog_cannot_fake_or_change_the_extra_class(db):
    sc = await _weekday_scenario(db)
    actor = await make_account(db)
    tuesday = sc["day"] + timedelta(days=4)
    await _member(db, sc, "Socia Registro", sc["seats"][0], ends=tuesday)
    _event, rows = await _displaced(db, sc)
    row_id = rows["Socia Registro"].id

    # Recording it by hand would leave the membership unextended and nobody booked.
    with pytest.raises(SpecialEventError, match="Dar clase extra"):
        await takeover_crud.update_follow_up(
            db, row_id, actor_account_id=actor.id, compensation_type="membership_extension"
        )
    d = await bonus_crud.apply_bonus_class(db, row_id, actor_account_id=actor.id, resolve=False)
    assert d.follow_up_status == "pending"
    for change in ({"compensation_type": "make_up_class"}, {"compensation_type": ""},
                   {"compensation_notes": "otra cosa"}):
        with pytest.raises(SpecialEventError, match="Ya se le dio la clase extra"):
            await takeover_crud.update_follow_up(db, row_id, actor_account_id=actor.id, **change)
    # The rest of the follow-up still works.
    d = await takeover_crud.update_follow_up(db, row_id, actor_account_id=actor.id, notified=True)
    d = await takeover_crud.update_follow_up(db, row_id, actor_account_id=actor.id, resolved=True)
    assert (d.follow_up_status, d.compensation_type) == ("resolved", "membership_extension")


async def test_bulk_gives_it_to_every_client_still_without_compensation(db):
    sc = await _weekday_scenario(db)
    tuesday = sc["day"] + timedelta(days=4)
    people = {}
    for i, name in enumerate(("Uno", "Dos", "Tres")):
        people[name] = await _member(db, sc, name, sc["seats"][i], ends=tuesday)
    walk_in = await make_person(db, "Visita")
    db.add(Reservation(
        session_id=sc["sessions"][sc["day"]].id, person_id=walk_in.id, seat_id=sc["seats"][9].id,
        status="reserved", source="manual",
    ))
    await db.flush()
    event, rows = await _displaced(db, sc)
    await takeover_crud.update_follow_up(
        db, rows["Tres"].id, actor_account_id=None, compensation_type="make_up_class", resolved=True
    )

    result = await bonus_crud.apply_bonus_classes(db, event, actor_account_id=None)
    assert sorted(result.applied) == ["Dos", "Uno"]
    assert [name for name, _reason in result.skipped] == ["Visita"]
    wednesday = tuesday + timedelta(days=1)
    for name in ("Uno", "Dos"):
        person, sub = people[name]
        await db.refresh(sub)
        assert sub.end_at == local_datetime(wednesday, _END_OF_DAY)
    person, sub = people["Tres"]
    await db.refresh(sub)
    assert sub.end_at == local_datetime(tuesday, _END_OF_DAY)  # its own compensation stands


# ---------------------------------------------------------------------------------------------
# Through GraphQL, as the desktop screen drives it
# ---------------------------------------------------------------------------------------------
async def test_graphql_previews_and_applies_the_extra_class(db):
    sc = await _weekday_scenario(db)
    admin = await make_account(db)
    ctx = _ctx(db, admin)
    tuesday = sc["day"] + timedelta(days=4)
    wednesday = tuesday + timedelta(days=1)
    await _member(db, sc, "Por GraphQL", sc["seats"][0], ends=tuesday)
    await _member(db, sc, "En Lote", sc["seats"][1], ends=tuesday)
    event, rows = await _displaced(db, sc)

    listed = (
        await _run(
            db, ctx,
            """query($id: Int!) { specialEventDisplacements(eventId: $id) {
                id personName bonusPreview bonusBlocker } }""",
            id=event.id,
        )
    )["specialEventDisplacements"]
    assert {r["personName"]: r["bonusPreview"].split(" · ")[0] for r in listed} == {
        "Por GraphQL": f"mié {wednesday:%d/%m} 19:00", "En Lote": f"mié {wednesday:%d/%m} 19:00",
    }

    single = (
        await _run(
            db, ctx,
            """mutation($id: Int!) { applyDisplacementBonusClass(displacementId: $id) {
                success error displacement { followUpStatus compensationType compensationNotes bonusPreview } } }""",
            id=rows["Por GraphQL"].id,
        )
    )["applyDisplacementBonusClass"]
    assert single["success"], single["error"]
    assert single["displacement"]["compensationType"] == "membership_extension"
    assert single["displacement"]["followUpStatus"] == "resolved"
    assert single["displacement"]["bonusPreview"] is None

    bulk = (
        await _run(
            db, ctx,
            """mutation($id: Int!) { applySpecialEventBonusClasses(eventId: $id) {
                success error message detail { displacements { personName compensationType } } } }""",
            id=event.id,
        )
    )["applySpecialEventBonusClasses"]
    assert bulk["success"], bulk["error"]
    assert bulk["message"] == "Clase extra para 1 cliente(s)."
    assert {r["personName"]: r["compensationType"] for r in bulk["detail"]["displacements"]} == {
        "Por GraphQL": "membership_extension", "En Lote": "membership_extension",
    }
