"""Local wall-clock time for generated classes (the six-hour bug) and local-day logic.

Production (asyncpg, container in UTC) stored template classes six hours early because the
generator built naive datetimes; asyncpg also returns TIMESTAMPTZ in UTC, so ``start_at.date()``
reported the next day for evening classes. These tests pin the fix and the data migration.
"""
from __future__ import annotations

import importlib.util
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app.core.timezones import local_date, local_datetime, to_local
from app.crud.classSessionCrud import generate_sessions_from_template
from app.crud.standing_bookings.materialization import materialize_standing_bookings
from app.models import ClassSession, Reservation, StandingBooking, StandingBookingException
from tests.special_event_factories import (
    make_class_type,
    make_person,
    make_subscription,
    make_template,
    make_venue,
    next_friday,
)

_MIGRATION = (
    Path(__file__).resolve().parents[1] / "alembic" / "versions" / "b2e5f8a1c4d7_fix_session_local_times.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("fix_session_local_times", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_helpers_never_use_the_utc_day():
    evening = datetime(2026, 10, 31, 1, 0, tzinfo=timezone.utc)  # Friday 19:00 in Mexico City
    assert local_date(evening) == date(2026, 10, 30)
    assert to_local(evening).strftime("%H:%M") == "19:00"
    assert local_datetime(date(2026, 10, 30), time(19, 0)) == evening


async def test_generated_class_keeps_its_local_time(db):
    venue, _ = await make_venue(db, bikes=2)
    ctype = await make_class_type(db)
    template = await make_template(db, venue, ctype, at=time(19, 0))
    day = next_friday()

    created = await generate_sessions_from_template(db, template.id, day, day, commit=False)
    assert len(created) == 1
    start = created[0].start_at
    assert to_local(start).strftime("%Y-%m-%d %H:%M") == f"{day.isoformat()} 19:00"
    assert start.astimezone(timezone.utc).strftime("%H:%M") == "01:00"
    await db.flush()
    stored = (await db.execute(select(ClassSession.start_at).where(ClassSession.id == created[0].id))).scalar()
    assert to_local(stored).hour == 19

    again = await generate_sessions_from_template(db, template.id, day, day, commit=False)
    assert again == []


async def test_migration_moves_shifted_sessions_back_and_is_idempotent(db):
    venue, _ = await make_venue(db, bikes=2)
    ctype = await make_class_type(db)
    template = await make_template(db, venue, ctype, at=time(19, 0))
    day = next_friday()
    # What the buggy generator stored: 19:00 *UTC* (13:00 in Mexico City).
    wrong = datetime.combine(day, time(19, 0), tzinfo=timezone.utc)
    shifted = ClassSession(
        class_type_id=ctype.id, venue_id=venue.id, template_id=template.id, name="Spinning 7:00 pm",
        start_at=wrong, end_at=wrong + timedelta(hours=1), capacity=14, status="scheduled",
    )
    correct_tpl = await make_template(db, venue, ctype, at=time(20, 0))
    right = ClassSession(
        class_type_id=ctype.id, venue_id=venue.id, template_id=correct_tpl.id, name="Spinning 8:00 pm",
        start_at=local_datetime(day, time(20, 0)), end_at=local_datetime(day, time(21, 0)),
        capacity=14, status="scheduled",
    )
    db.add_all([shifted, right])
    await db.flush()
    person = await make_person(db, "Con reserva")
    reservation = Reservation(session_id=shifted.id, person_id=person.id, status="reserved")
    db.add(reservation)
    await db.flush()

    migration = _load_migration()
    first = await db.execute(migration._FIX_SQL, {"tz": migration.STUDIO_TZ})
    assert first.rowcount >= 1
    second = await db.execute(migration._FIX_SQL, {"tz": migration.STUDIO_TZ})
    assert second.rowcount == 0

    await db.refresh(shifted)
    await db.refresh(right)
    assert to_local(shifted.start_at).strftime("%H:%M") == "19:00"
    assert to_local(shifted.end_at).strftime("%H:%M") == "20:00"
    assert local_date(shifted.start_at) == day  # the calendar day never changes
    assert to_local(right.start_at).strftime("%H:%M") == "20:00"  # correct rows untouched
    await db.refresh(reservation)
    assert reservation.session_id == shifted.id and reservation.status == "reserved"


async def test_skip_exception_applies_to_evening_classes(db):
    """With UTC ``.date()`` an 8 pm class looked like the next day and ignored its exception."""
    venue, seats = await make_venue(db, bikes=2)
    ctype = await make_class_type(db)
    template = await make_template(db, venue, ctype, at=time(20, 0))
    day = next_friday()
    session = ClassSession(
        class_type_id=ctype.id, venue_id=venue.id, template_id=template.id, name="Spinning 8:00 pm",
        start_at=local_datetime(day, time(20, 0)), end_at=local_datetime(day, time(21, 0)),
        capacity=2, status="scheduled",
    )
    db.add(session)
    person = await make_person(db, "Se salta el viernes")
    sub = await make_subscription(db, person)
    booking = StandingBooking(
        person_id=person.id, subscription_id=sub.id, template_id=template.id, seat_id=seats[0].id,
        start_date=day - timedelta(days=1), end_date=day + timedelta(days=1), status="active",
    )
    db.add(booking)
    await db.flush()
    db.add(StandingBookingException(standing_booking_id=booking.id, session_date=day, action="skip"))
    await db.flush()

    await materialize_standing_bookings(db, window_weeks=12, start_date=day - timedelta(days=1))
    booked = (
        await db.execute(select(Reservation).where(Reservation.session_id == session.id))
    ).scalars().all()
    assert booked == []
