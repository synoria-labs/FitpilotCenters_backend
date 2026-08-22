"""Synthetic data for the local development database.

Enough to exercise the campaigns feature end to end without touching production data: a few
members with real booking histories, so the class-affinity segmentation has something to
segment on, and a mix of lapsed/active memberships so a win-back audience is non-empty.

Deliberately synthetic. Restoring a production dump onto a laptop would put real members'
names, phone numbers and payment history on a development machine, and nothing here needs
that: the test suite rolls back every transaction, and clicking through the UI only needs
data with the right *shape*.

Idempotent — re-running replaces the seeded rows rather than duplicating them. Everything it
creates is tagged with the SEED_TAG prefix so it can be told apart from anything you add by
hand, and removed with --clean.

    python scripts/dev_seed.py
    python scripts/dev_seed.py --clean
"""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import delete, select, text  # noqa: E402

from app.db.postgresql import async_session_factory  # noqa: E402
from app.models import (  # noqa: E402
    ClassSession,
    ClassTemplate,
    ClassType,
    MembershipPlan,
    MembershipSubscription,
    People,
    PersonRole,
    Reservation,
    Role,
    Venue,
)

SEED_TAG = "[dev-seed]"
# People has no free-text field, so seeded members are tagged by an email domain that cannot
# collide with a real one (.local is reserved and never routable).
SEED_EMAIL_DOMAIN = "dev-seed.local"

# A deterministic seed keeps runs comparable: the same member always prefers the same class,
# so "did my change alter the audience?" is answerable by diffing counts.
RNG = random.Random(20260822)

CLASS_TYPES = [
    ("spinning", "Spinning"),
    ("yoga", "Yoga"),
    ("funcional", "Funcional"),
]

# (class_type_code, weekday 0=Sunday, hour)
TEMPLATES = [
    ("spinning", 1, 7),
    ("spinning", 3, 19),
    ("yoga", 2, 9),
    ("yoga", 4, 19),
    ("funcional", 1, 18),
    ("funcional", 5, 10),
]

# (name, favourite class code, membership state)
MEMBERS = [
    ("Ana Ríos", "spinning", "expired"),
    ("Bruno Salas", "spinning", "expired"),
    ("Carla Méndez", "yoga", "expired"),
    ("Diego Fuentes", "yoga", "expired"),
    ("Elena Vargas", "funcional", "expired"),
    ("Faustino Lira", "spinning", "active"),
    ("Gabriela Ortiz", "yoga", "active"),
    ("Hugo Peralta", "funcional", "active"),
    # No booking history at all: proves the "sin clase favorita" path is handled and that a
    # class-affinity filter genuinely excludes people rather than silently passing them.
    ("Irene Cano", None, "expired"),
]


# Class schedules are local wall-clock times, and the affinity queries read the hour
# straight out of `class_sessions.start_at` in the session timezone. Building sessions from a
# UTC clock would put a "07:00 Spinning" class at 01:00 local and quietly break every
# hour-based expectation in the seeded data.
LOCAL_TZ = ZoneInfo("America/Mexico_City")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _local_now() -> datetime:
    return datetime.now(LOCAL_TZ)


async def _clean(db) -> None:
    """Remove everything a previous run created, children first."""
    people_ids = (
        await db.execute(
            select(People.id).where(People.email.like(f"%@{SEED_EMAIL_DOMAIN}"))
        )
    ).scalars().all()
    if people_ids:
        await db.execute(delete(Reservation).where(Reservation.person_id.in_(people_ids)))
        await db.execute(
            delete(MembershipSubscription).where(
                MembershipSubscription.person_id.in_(people_ids)
            )
        )
        await db.execute(delete(PersonRole).where(PersonRole.person_id.in_(people_ids)))
        await db.execute(delete(People).where(People.id.in_(people_ids)))

    sessions = (
        await db.execute(
            select(ClassSession.id).where(ClassSession.name.like(f"{SEED_TAG}%"))
        )
    ).scalars().all()
    if sessions:
        await db.execute(delete(Reservation).where(Reservation.session_id.in_(sessions)))
        await db.execute(delete(ClassSession).where(ClassSession.id.in_(sessions)))
    await db.execute(delete(ClassTemplate).where(ClassTemplate.name.like(f"{SEED_TAG}%")))
    await db.commit()


async def _get_or_create_venue(db) -> Venue:
    venue = (await db.execute(select(Venue).limit(1))).scalars().first()
    if venue is not None:
        return venue
    venue = Venue(name=f"{SEED_TAG} Sede Centro", capacity=30)
    db.add(venue)
    await db.flush()
    return venue


async def _get_or_create_role(db) -> Role:
    role = (
        await db.execute(select(Role).where(Role.code == "member"))
    ).scalars().first()
    if role is not None:
        return role
    role = Role(code="member", description="Socio")
    db.add(role)
    await db.flush()
    return role


async def _get_or_create_plan(db) -> MembershipPlan:
    plan = (
        await db.execute(
            select(MembershipPlan).where(MembershipPlan.name.like(f"{SEED_TAG}%"))
        )
    ).scalars().first()
    if plan is not None:
        return plan
    plan = MembershipPlan(
        name=f"{SEED_TAG} Mensualidad",
        price=500,
        duration_value=1,
        duration_unit="month",
        plan_type="flexible",
    )
    db.add(plan)
    await db.flush()
    return plan


async def seed() -> None:
    async with async_session_factory() as db:
        await _clean(db)

        venue = await _get_or_create_venue(db)
        role = await _get_or_create_role(db)
        plan = await _get_or_create_plan(db)

        types: dict[str, ClassType] = {}
        for code, name in CLASS_TYPES:
            existing = (
                await db.execute(select(ClassType).where(ClassType.code == code))
            ).scalars().first()
            if existing is None:
                existing = ClassType(code=code, name=name)
                db.add(existing)
                await db.flush()
            types[code] = existing

        templates: dict[str, list[ClassTemplate]] = {code: [] for code, _ in CLASS_TYPES}
        for code, weekday, hour in TEMPLATES:
            template = ClassTemplate(
                class_type_id=types[code].id,
                venue_id=venue.id,
                default_capacity=20,
                default_duration_min=60,
                weekday=weekday,
                start_time_local=time(hour, 0),
                name=f"{SEED_TAG} {types[code].name} {weekday}-{hour}",
                is_active=True,
            )
            db.add(template)
            await db.flush()
            templates[code].append(template)

        # Twelve weeks of past sessions, so the 180-day affinity window has real history.
        sessions_by_code: dict[str, list[ClassSession]] = {c: [] for c, _ in CLASS_TYPES}
        today = _now()
        local_today = _local_now()
        for code, template_list in templates.items():
            for template in template_list:
                for week in range(1, 13):
                    start = (local_today - timedelta(weeks=week)).replace(
                        hour=template.start_time_local.hour,
                        minute=0,
                        second=0,
                        microsecond=0,
                    )
                    session = ClassSession(
                        class_type_id=template.class_type_id,
                        venue_id=venue.id,
                        template_id=template.id,
                        name=f"{SEED_TAG} {types[code].name}",
                        start_at=start,
                        end_at=start + timedelta(minutes=60),
                        capacity=20,
                        status="completed",
                    )
                    db.add(session)
                    sessions_by_code[code].append(session)
        await db.flush()

        created = 0
        for index, (full_name, favourite, state) in enumerate(MEMBERS):
            slug = full_name.split()[0].lower()
            person = People(
                full_name=full_name,
                phone_number=f"52181970{8000 + index:04d}",
                email=f"{slug}@{SEED_EMAIL_DOMAIN}",
            )
            db.add(person)
            await db.flush()
            db.add(PersonRole(person_id=person.id, role_id=role.id))

            if state == "active":
                start_at = today - timedelta(days=10)
                end_at = today + timedelta(days=20)
                status = "active"
            else:
                # Inside the default win-back window (expired between 90 and 7 days ago).
                start_at = today - timedelta(days=70)
                end_at = today - timedelta(days=RNG.randint(10, 80))
                status = "expired"
            db.add(
                MembershipSubscription(
                    person_id=person.id,
                    plan_id=plan.id,
                    start_at=start_at,
                    end_at=end_at,
                    status=status,
                )
            )

            if favourite:
                # Mostly the favourite class, plus a little noise so `favorite` mode is
                # actually choosing a winner rather than reading the only option present.
                for session in RNG.sample(sessions_by_code[favourite], 8):
                    db.add(
                        Reservation(
                            session_id=session.id,
                            person_id=person.id,
                            status="checked_in",
                            reserved_at=session.start_at - timedelta(days=1),
                            checkin_at=session.start_at,
                            source="manual",
                        )
                    )
                other = RNG.choice([c for c, _ in CLASS_TYPES if c != favourite])
                for session in RNG.sample(sessions_by_code[other], 2):
                    db.add(
                        Reservation(
                            session_id=session.id,
                            person_id=person.id,
                            status="reserved",
                            reserved_at=session.start_at - timedelta(days=1),
                            source="manual",
                        )
                    )
            created += 1

        await db.commit()

        counts = (
            await db.execute(
                text(
                    "SELECT (SELECT count(*) FROM app.people WHERE email LIKE :mail),"
                    "       (SELECT count(*) FROM app.class_sessions WHERE name LIKE :tag),"
                    "       (SELECT count(*) FROM app.reservations r"
                    "          JOIN app.people p ON p.id = r.person_id"
                    "         WHERE p.email LIKE :mail)"
                ),
                {"tag": f"{SEED_TAG}%", "mail": f"%@{SEED_EMAIL_DOMAIN}"},
            )
        ).one()
        print(
            f"Sembrados {created} socios · {counts[1]} sesiones · {counts[2]} reservas.\n"
            "Prueba una campana win-back filtrando por Spinning: deberian entrar Ana y Bruno."
        )


async def clean_only() -> None:
    async with async_session_factory() as db:
        await _clean(db)
    print("Datos de prueba eliminados.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean", action="store_true", help="borrar los datos sembrados")
    args = parser.parse_args()
    asyncio.run(clean_only() if args.clean else seed())
