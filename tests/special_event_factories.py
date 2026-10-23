"""Factories for the special-events tests (rows inside the rolled-back ``db`` fixture).

The scenario mirrors production: a 14-bike spinning room with a Friday 19:00 regular class
(template, generated session, standing-booking members) and an event at the same time.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import select

from app.core.timezones import local_datetime
from app.crud.special_events import events as events_crud
from app.models import (
    Account,
    ClassSession,
    ClassTemplate,
    ClassType,
    MembershipPlan,
    MembershipSubscription,
    People,
    PersonRole,
    Reservation,
    Role,
    Seat,
    StandingBooking,
    Venue,
)

NOW = datetime.now(timezone.utc)


def next_friday(min_days_ahead: int = 20) -> date:
    """A Friday comfortably in the future (weekday 4 in Python, 5 in the templates' encoding)."""
    day = date.today() + timedelta(days=min_days_ahead)
    while day.weekday() != 4:
        day += timedelta(days=1)
    return day


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def make_venue(db, *, bikes: int = 14) -> tuple[Venue, List[Seat]]:
    venue = Venue(name=unique("Sala"), capacity=bikes, address="Las Rosas #101, Gómez Palacio")
    db.add(venue)
    await db.flush()
    seats = []
    for i in range(1, bikes + 1):
        seat = Seat(venue_id=venue.id, label=f"Bicicleta {i}", is_active=True)
        db.add(seat)
        seats.append(seat)
    await db.flush()
    return venue, seats


async def make_class_type(db) -> ClassType:
    ctype = ClassType(code=unique("spin"), name="Spinning")
    db.add(ctype)
    await db.flush()
    return ctype


async def role(db, code: str) -> Role:
    existing = (await db.execute(select(Role).where(Role.code == code))).scalar_one_or_none()
    if existing:
        return existing
    r = Role(code=code, description=code)
    db.add(r)
    await db.flush()
    return r


async def make_person(db, name: str, phone: Optional[str] = None, *, role_code: Optional[str] = "member") -> People:
    person = People(full_name=name, phone_number=phone)
    db.add(person)
    await db.flush()
    if role_code:
        r = await role(db, role_code)
        db.add(PersonRole(person_id=person.id, role_id=r.id, created_at=NOW))
        await db.flush()
    return person


async def make_account(db, name: str = "Admin Test") -> Account:
    person = await make_person(db, name, role_code="admin")
    account = Account(person_id=person.id, username=unique("admin"), password_hash="x")
    db.add(account)
    await db.flush()
    return account


async def make_subscription(db, person: People, *, days_left: int = 30) -> MembershipSubscription:
    plan = MembershipPlan(
        name="Plan Spinning Mensual (test)",
        price=Decimal("600"),
        duration_value=1,
        duration_unit="month",
        fixed_time_slot=True,
    )
    db.add(plan)
    await db.flush()
    sub = MembershipSubscription(
        person_id=person.id,
        plan_id=plan.id,
        start_at=NOW - timedelta(days=5),
        end_at=NOW + timedelta(days=days_left),
        status="active",
    )
    db.add(sub)
    await db.flush()
    return sub


async def make_template(db, venue: Venue, ctype: ClassType, *, at: time = time(19, 0), weekday: int = 5) -> ClassTemplate:
    tpl = ClassTemplate(
        class_type_id=ctype.id,
        venue_id=venue.id,
        default_capacity=venue.capacity,
        default_duration_min=60,
        weekday=weekday,
        start_time_local=at,
        name="Spinning 7:00 pm",
        is_active=True,
    )
    db.add(tpl)
    await db.flush()
    return tpl


async def make_session(db, template: ClassTemplate, day: date, *, capacity: Optional[int] = None) -> ClassSession:
    start = local_datetime(day, template.start_time_local)
    session = ClassSession(
        class_type_id=template.class_type_id,
        venue_id=template.venue_id,
        template_id=template.id,
        name=template.name,
        start_at=start,
        end_at=start + timedelta(minutes=template.default_duration_min),
        capacity=capacity or template.default_capacity or 14,
        status="scheduled",
    )
    db.add(session)
    await db.flush()
    return session


async def book_member(
    db,
    session: ClassSession,
    template: ClassTemplate,
    seat: Seat,
    name: str,
    phone: Optional[str] = None,
    *,
    standing: bool = True,
):
    """A member with a subscription, a standing booking for the template and a reservation."""
    person = await make_person(db, name, phone)
    sub = await make_subscription(db, person)
    sb = None
    if standing:
        sb = StandingBooking(
            person_id=person.id,
            subscription_id=sub.id,
            template_id=template.id,
            seat_id=seat.id,
            start_date=date.today() - timedelta(days=3),
            end_date=date.today() + timedelta(days=90),
            status="active",
        )
        db.add(sb)
        await db.flush()
    reservation = Reservation(
        session_id=session.id,
        person_id=person.id,
        seat_id=seat.id,
        status="reserved",
        source="standing" if standing else "manual",
    )
    db.add(reservation)
    await db.flush()
    return person, sub, sb, reservation


async def regular_class_scenario(db, *, booked: int = 3, bikes: int = 14):
    """Venue + Friday 19:00 template + session with ``booked`` standing members on bikes 1..n."""
    venue, seats = await make_venue(db, bikes=bikes)
    ctype = await make_class_type(db)
    template = await make_template(db, venue, ctype)
    day = next_friday()
    session = await make_session(db, template, day)
    members = []
    for i in range(booked):
        members.append(
            await book_member(db, session, template, seats[i], f"Socio {i + 1}", f"87110000{i:02d}")
        )
    return {
        "venue": venue,
        "seats": seats,
        "ctype": ctype,
        "template": template,
        "day": day,
        "session": session,
        "members": members,
    }


async def make_event(
    db,
    scenario,
    *,
    start: time = time(19, 0),
    end: Optional[time] = time(21, 0),
    capacity: Optional[int] = 14,
    price: str = "500.00",
    name: str = "Halloween · 2.º aniversario (test)",
    actor_account_id: Optional[int] = None,
    hold_minutes: int = 30,
):
    return await events_crud.create_event(
        db,
        actor_account_id=actor_account_id,
        name=name,
        venue_id=scenario["venue"].id,
        local_day=scenario["day"],
        start_time=start,
        end_time=end,
        price=Decimal(price),
        class_type_id=scenario["ctype"].id,
        capacity=capacity,
        hold_minutes=hold_minutes,
        includes="Kit de Electrolit y toalla\nMesa de snacks",
    )


def same_session_factory(db):
    """Stand-in for ``async_session_factory`` so services that open their own session reuse the
    test session (whose commits are SAVEPOINTs rolled back at teardown)."""

    @asynccontextmanager
    async def _factory():
        yield db

    return _factory
