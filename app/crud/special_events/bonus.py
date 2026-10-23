"""Default compensation for a displaced client: one extra class after their membership ends.

The class the event took is given back at the end of the membership: it is extended to the
next day with a class at the client's usual hour that still has room, and that class is booked
(their usual bike when it is free). If their last class is a Tuesday, the Wednesday after it
they still show as active and have their reservation.

"Last" counts the membership's end and the end of their fixed schedule, whichever is later, so
a reservation already made past the membership is never mistaken for the extra one. If the
membership already ended, the extra class is the next one from today.

This is the only compensation the system applies itself; every other one is just recorded
(see takeover.update_follow_up). Classes are generated only as far as some membership needs
them (enrollment and renewal generate their slot), so the day after the last membership of a
slot often has no class yet: it is generated here the same way, and booked.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezones import local_date, local_datetime, to_local
from app.crud.classSessionCrud import generate_sessions_from_template
from app.crud.locks import lock_class_session
from app.crud.standing_bookings.utils import _resolve_seat_for_session, _session_has_capacity
from app.models import (
    ClassSession,
    ClassTemplate,
    MembershipSubscription,
    People,
    Reservation,
    Seat,
    StandingBooking,
)
from app.models.specialEventModel import (
    COMPENSATION_BONUS_CLASS,
    FOLLOW_UP_RESOLVED,
    RESTORATION_RESTORED,
    SpecialEvent,
    SpecialEventDisplacement,
)

from .common import SpecialEventError, utcnow

# How far past the membership's last day to look for a class with room at the usual hour.
SEARCH_DAYS = 21
# Memberships cover their whole last day (see memberships.utils._calculate_subscription_end).
_END_OF_DAY = time(23, 59, 59)
_WEEKDAYS_SHORT = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
_BOOKED = ("reserved", "checked_in", "waitlisted")


def _template_weekday(day: date) -> int:
    """``class_templates.weekday`` encoding: 0 = Sunday ... 6 = Saturday."""
    return (day.weekday() + 1) % 7


def _day_label(day: date) -> str:
    return f"{_WEEKDAYS_SHORT[day.weekday()]} {day:%d/%m}"


@dataclass
class BonusPlan:
    """Where the extra class goes and what changes, before anything is written."""

    subscription: MembershipSubscription
    day: date
    at: time
    template: ClassTemplate
    session: Optional[ClassSession]  # None while that day's class is not generated yet
    seat_id: Optional[int]
    seat_label: Optional[str]

    @property
    def new_end_at(self) -> datetime:
        return local_datetime(self.day, _END_OF_DAY)

    def summary(self) -> str:
        """'lun 09/11 20:00 · Bicicleta 7 · vence 09/11 (antes 06/11)' for the follow-up list."""
        parts = [f"{_day_label(self.day)} {self.at:%H:%M}"]
        if self.seat_label:
            parts.append(self.seat_label)
        previous = local_date(self.subscription.end_at)
        parts.append(f"vence {self.day:%d/%m} (antes {previous:%d/%m})")
        return " · ".join(parts)

    def notes(self, previous_end_at: datetime) -> str:
        """What was done, in the compensation's notes."""
        text = f"Clase extra el {_day_label(self.day)} a las {self.at:%H:%M}"
        if self.seat_label:
            text += f" en {self.seat_label}"
        return (
            f"{text}. Su membresía vence ahora el {self.day:%d/%m/%Y} "
            f"(antes el {local_date(previous_end_at):%d/%m/%Y})."
        )


async def _target_subscription(
    db: AsyncSession, d: SpecialEventDisplacement, *, for_update: bool = False
) -> MembershipSubscription:
    """The membership the extra class extends: the displaced one, or the renewal that already
    follows it (the extra class goes after the *last* membership)."""
    if d.subscription_id is None:
        raise SpecialEventError(
            "No tiene membresía con la cual darle la clase extra: registra otra compensación."
        )
    base = await db.get(MembershipSubscription, d.subscription_id)
    if base is None:
        raise SpecialEventError("No encontré su membresía: registra otra compensación.")
    stmt = (
        select(MembershipSubscription)
        .where(
            MembershipSubscription.person_id == d.person_id,
            MembershipSubscription.status.in_(("active", "pending")),
            MembershipSubscription.end_at >= base.end_at,
        )
        .order_by(MembershipSubscription.end_at.desc(), MembershipSubscription.id.desc())
        .limit(1)
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    target = (await db.execute(stmt)).scalars().first()
    if target is None:
        if base.status == "canceled":
            raise SpecialEventError("Su membresía está cancelada: registra otra compensación.")
        target = base
        if for_update:
            target = (
                await db.execute(
                    select(MembershipSubscription)
                    .where(MembershipSubscription.id == base.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars().first()
    return target


async def plan_bonus_class(
    db: AsyncSession, d: SpecialEventDisplacement, *, subscription: Optional[MembershipSubscription] = None
) -> BonusPlan:
    """Work out the extra class for a displaced client, or say why there is none."""
    if d.compensation_type is not None:
        raise SpecialEventError("Ya tiene una compensación registrada.")
    if d.restoration_status == RESTORATION_RESTORED:
        raise SpecialEventError("Su clase se restituyó: no hace falta compensarla.")
    sub = subscription or await _target_subscription(db, d)

    displaced = await db.get(ClassSession, d.session_id)
    if displaced is None:
        raise SpecialEventError("No encontré la clase original.")
    usual_template = await db.get(ClassTemplate, displaced.template_id) if displaced.template_id else None
    at = usual_template.start_time_local if usual_template else to_local(displaced.start_at).time()
    venue_id = displaced.venue_id

    standing = (
        await db.execute(
            select(StandingBooking).where(
                StandingBooking.subscription_id == sub.id, StandingBooking.status == "active"
            )
        )
    ).scalars().all()
    last_day = max([local_date(sub.end_at), *(sb.end_date for sb in standing)])
    first_day = max(last_day + timedelta(days=1), local_date(utcnow()))
    window_end = first_day + timedelta(days=SEARCH_DAYS)

    templates: Dict[int, ClassTemplate] = {
        t.weekday: t
        for t in (
            await db.execute(
                select(ClassTemplate).where(
                    ClassTemplate.venue_id == venue_id,
                    ClassTemplate.start_time_local == at,
                    ClassTemplate.is_active.is_(True),
                )
            )
        ).scalars().all()
    }
    if not templates:
        raise SpecialEventError(f"No hay clases regulares a las {at:%H:%M} para darle la clase extra.")
    sessions: Dict[Tuple[int, date], ClassSession] = {
        (s.template_id, local_date(s.start_at)): s
        for s in (
            await db.execute(
                select(ClassSession).where(
                    ClassSession.template_id.in_([t.id for t in templates.values()]),
                    ClassSession.start_at >= local_datetime(first_day, time(0, 0)),
                    ClassSession.start_at < local_datetime(window_end + timedelta(days=1), time(0, 0)),
                )
            )
        ).scalars().all()
    }
    already_booked = set(
        (
            await db.execute(
                select(Reservation.session_id).where(
                    Reservation.person_id == d.person_id,
                    Reservation.session_id.in_([s.id for s in sessions.values()] or [0]),
                    Reservation.status.in_(_BOOKED),
                )
            )
        ).scalars().all()
    )
    preferred_seat = d.seat_id
    now = utcnow()

    day = first_day
    while day <= window_end:
        template = templates.get(_template_weekday(day))
        if template is not None:
            session = sessions.get((template.id, day))
            if session is None:
                # Not generated yet: applying generates it (empty, so their bike is free).
                if day > local_date(now):
                    label = (await db.get(Seat, preferred_seat)).label if preferred_seat else None
                    return BonusPlan(sub, day, at, template, None, preferred_seat, label)
            elif (
                session.status == "scheduled"
                and session.start_at > now
                and session.id not in already_booked
                and await _session_has_capacity(db, session)
            ):
                seat_id, note = await _resolve_seat_for_session(db, session, preferred_seat)
                # No free bike that day: try the next one. A bike that no longer exists only
                # means booking the spot without one, as a class without bikes would.
                if seat_id is not None or preferred_seat is None or note == "Preferred seat not available":
                    label = (await db.get(Seat, seat_id)).label if seat_id else None
                    return BonusPlan(sub, day, at, template, session, seat_id, label)
        day += timedelta(days=1)
    raise SpecialEventError(
        f"No hay una clase a las {at:%H:%M} con lugar en las {SEARCH_DAYS // 7} semanas después de "
        f"que termina su membresía ({last_day:%d/%m}): registra otra compensación."
    )


async def apply_bonus_class(
    db: AsyncSession,
    displacement_id: int,
    *,
    actor_account_id: Optional[int],
    resolve: bool = True,
) -> SpecialEventDisplacement:
    """Give a displaced client their extra class: extend the membership, book the class and
    record it as the compensation (resolved unless ``resolve`` is False)."""
    d = (
        await db.execute(
            select(SpecialEventDisplacement)
            .where(SpecialEventDisplacement.id == int(displacement_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if d is None:
        raise SpecialEventError("No se encontró el registro de seguimiento.")
    sub = await _target_subscription(db, d, for_update=True)
    plan = await plan_bonus_class(db, d, subscription=sub)
    if plan.session is None:
        # The way an enrollment generates its slot's classes (it skips a day that has one).
        await generate_sessions_from_template(db, plan.template.id, plan.day, plan.day, commit=False)
        plan = await plan_bonus_class(db, d, subscription=sub)
    if plan.session is None:
        raise SpecialEventError("No se pudo generar la clase de ese día; inténtalo de nuevo.")
    # Same per-class lock as every booking path; plan again under it, since the bike or the
    # last spot may have been taken in between.
    await lock_class_session(db, plan.session.id)
    locked = await plan_bonus_class(db, d, subscription=sub)
    if locked.session is None or locked.session.id != plan.session.id:
        raise SpecialEventError("Esa clase se acaba de llenar; inténtalo de nuevo.")
    plan = locked

    now = utcnow()
    previous_end_at = sub.end_at
    if plan.new_end_at > sub.end_at:
        sub.end_at = plan.new_end_at
    if sub.status == "expired" and sub.end_at > now:
        sub.status = "active"
    sub.updated_at = now
    try:
        async with db.begin_nested():
            db.add(
                Reservation(
                    session_id=plan.session.id,
                    person_id=d.person_id,
                    seat_id=plan.seat_id,
                    status="reserved",
                    source="override",
                )
            )
            await db.flush()
    except IntegrityError:
        raise SpecialEventError("La bici o el lugar se acaba de ocupar; inténtalo de nuevo.") from None

    d.compensation_type = COMPENSATION_BONUS_CLASS
    d.compensation_notes = plan.notes(previous_end_at)
    if resolve:
        d.follow_up_status = FOLLOW_UP_RESOLVED
        d.resolved_at = now
        d.resolved_by = actor_account_id
    d.updated_at = now
    await db.flush()
    return d


@dataclass
class BonusBatchResult:
    applied: List[str]
    skipped: List[Tuple[str, str]]  # (client, reason)


async def apply_bonus_classes(
    db: AsyncSession, event: SpecialEvent, *, actor_account_id: Optional[int]
) -> BonusBatchResult:
    """The extra class for every displaced client of the event without a compensation yet.
    Each client is applied on its own; one that cannot get it is reported, not fatal."""
    pending = (
        await db.execute(
            select(SpecialEventDisplacement.id, People.full_name)
            .join(People, People.id == SpecialEventDisplacement.person_id)
            .where(
                SpecialEventDisplacement.event_id == event.id,
                SpecialEventDisplacement.compensation_type.is_(None),
                or_(
                    SpecialEventDisplacement.restoration_status.is_(None),
                    SpecialEventDisplacement.restoration_status != RESTORATION_RESTORED,
                ),
            )
            .order_by(SpecialEventDisplacement.id)
        )
    ).all()
    result = BonusBatchResult(applied=[], skipped=[])
    for displacement_id, name in pending:
        try:
            async with db.begin_nested():
                await apply_bonus_class(db, displacement_id, actor_account_id=actor_account_id)
        except SpecialEventError as exc:
            result.skipped.append((name, str(exc)))
            continue
        result.applied.append(name)
    return result


async def bonus_previews(
    db: AsyncSession, displacements: List[SpecialEventDisplacement]
) -> Dict[int, Tuple[Optional[str], Optional[str]]]:
    """{displacement id: (proposed extra class, or why there is none)} for the clients still
    without a compensation, so staff see it before applying."""
    out: Dict[int, Tuple[Optional[str], Optional[str]]] = {}
    for d in displacements:
        if d.compensation_type is not None or d.restoration_status == RESTORATION_RESTORED:
            continue
        try:
            plan = await plan_bonus_class(db, d)
        except SpecialEventError as exc:
            out[d.id] = (None, str(exc))
        else:
            out[d.id] = (plan.summary(), None)
    return out
