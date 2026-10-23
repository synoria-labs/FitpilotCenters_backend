"""Taking over a regular class slot for a special event, and giving it back.

The takeover is explicit and audited: staff preview the affected classes and clients, then
apply. The regular class goes to status ``displaced`` and each of its reservations too — kept
with their seat, source and history, never deleted. Nothing about the clients' memberships
changes and nobody is messaged: every displaced client lands in a follow-up list where staff
record when they were told and how they were compensated.

Restoring (after a cancellation, or a takeover applied by mistake on a draft) is previewed the
same way and only reactivates a reservation that is still exactly as the takeover left it and
has no compensation recorded; everything else stays displaced, with the reason.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezones import local_date
from app.crud.locks import lock_class_session, lock_special_event
from app.models import (
    Account,
    ClassSession,
    MembershipPlan,
    MembershipSubscription,
    People,
    Reservation,
    Seat,
    StandingBooking,
    StandingBookingException,
)
from app.models.specialEventModel import (
    COMPENSATION_BONUS_CLASS,
    COMPENSATION_TYPES,
    EVENT_CANCELED,
    EVENT_COMPLETED,
    EVENT_DRAFT,
    FOLLOW_UP_NOTIFIED,
    FOLLOW_UP_PENDING,
    FOLLOW_UP_RESOLVED,
    RESERVATION_DISPLACED,
    RESTORATION_KEPT,
    RESTORATION_RESTORED,
    SESSION_DISPLACED,
    TAKEOVER_ACTIVE,
    TAKEOVER_RESTORED,
    SpecialEvent,
    SpecialEventDisplacement,
    SpecialEventTakeover,
)

from .bonus import bonus_previews
from .common import SpecialEventError, local_schedule_label, utcnow
from .events import overlapping_regular_sessions

# Reservations a takeover displaces. 'canceled'/'no_show' are history and are left alone.
_DISPLACEABLE = ("reserved", "checked_in", "waitlisted")


@dataclass
class AffectedClient:
    person_id: int
    person_name: str
    phone: Optional[str]
    reservation_id: Optional[int]
    reservation_status: Optional[str]
    source: Optional[str]
    seat_id: Optional[int]
    seat_label: Optional[str]
    subscription_id: Optional[int]
    plan_name: Optional[str]
    membership_ends_at: Optional[datetime]
    standing_booking_id: Optional[int]
    # A standing booking covers the date but its reservation was never materialized.
    without_reservation: bool = False


@dataclass
class AffectedSession:
    session_id: int
    name: str
    start_at: datetime
    end_at: datetime
    status: str
    capacity: int
    taken_by_this_event: bool
    clients: List[AffectedClient] = field(default_factory=list)


@dataclass
class TakeoverPreview:
    event_id: int
    sessions: List[AffectedSession]
    blockers: List[str]
    summary: str

    @property
    def pending_session_ids(self) -> List[int]:
        return [s.session_id for s in self.sessions if not s.taken_by_this_event]

    @property
    def affected_clients(self) -> int:
        return sum(len(s.clients) for s in self.sessions if not s.taken_by_this_event)


# ---------------------------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------------------------
async def _membership_for(
    db: AsyncSession, person_id: int, at: datetime, preferred_subscription_id: Optional[int] = None
):
    """(subscription_id, plan_name, end_at) that covers ``at`` for the person, if any."""
    stmt = (
        select(MembershipSubscription.id, MembershipPlan.name, MembershipSubscription.end_at)
        .join(MembershipPlan, MembershipPlan.id == MembershipSubscription.plan_id)
        .where(MembershipSubscription.person_id == person_id)
    )
    if preferred_subscription_id is not None:
        row = (
            await db.execute(stmt.where(MembershipSubscription.id == preferred_subscription_id))
        ).first()
        if row is not None:
            return row
    row = (
        await db.execute(
            stmt.where(
                MembershipSubscription.start_at <= at,
                MembershipSubscription.end_at >= at,
                MembershipSubscription.status.in_(("active", "pending")),
            )
            .order_by(MembershipSubscription.end_at.desc())
            .limit(1)
        )
    ).first()
    return row


async def _standing_bookings_for_session(
    db: AsyncSession, session: ClassSession
) -> List[StandingBooking]:
    """Active standing bookings of the session's template that cover its local day, minus the
    ones with a skip/reschedule exception for that day (those clients already moved)."""
    if session.template_id is None:
        return []
    day = local_date(session.start_at)
    bookings = (
        await db.execute(
            select(StandingBooking).where(
                StandingBooking.template_id == session.template_id,
                StandingBooking.status == "active",
                StandingBooking.start_date <= day,
                StandingBooking.end_date >= day,
            )
        )
    ).scalars().all()
    if not bookings:
        return []
    excepted = {
        sb_id
        for (sb_id,) in (
            await db.execute(
                select(StandingBookingException.standing_booking_id).where(
                    StandingBookingException.standing_booking_id.in_([b.id for b in bookings]),
                    StandingBookingException.session_date == day,
                )
            )
        ).all()
    }
    return [b for b in bookings if b.id not in excepted]


async def _clients_of_session(db: AsyncSession, session: ClassSession) -> List[AffectedClient]:
    reservations = (
        await db.execute(
            select(Reservation).where(
                Reservation.session_id == session.id,
                Reservation.status.in_(_DISPLACEABLE),
            )
        )
    ).scalars().all()
    standing = await _standing_bookings_for_session(db, session)
    standing_by_person = {sb.person_id: sb for sb in standing}
    all_rows = (
        await db.execute(select(Reservation.person_id).where(Reservation.session_id == session.id))
    ).all()
    has_row = {pid for (pid,) in all_rows}

    person_ids = {r.person_id for r in reservations} | {
        sb.person_id for sb in standing if sb.person_id not in has_row
    }
    people = {
        p.id: p
        for p in (
            await db.execute(select(People).where(People.id.in_(list(person_ids))))
        ).scalars().all()
    } if person_ids else {}
    seat_ids = {r.seat_id for r in reservations if r.seat_id} | {
        sb.seat_id for sb in standing if sb.seat_id
    }
    labels = {
        sid: label
        for sid, label in (
            await db.execute(select(Seat.id, Seat.label).where(Seat.id.in_(list(seat_ids))))
        ).all()
    } if seat_ids else {}

    clients: List[AffectedClient] = []
    for r in reservations:
        sb = standing_by_person.get(r.person_id)
        membership = await _membership_for(
            db, r.person_id, session.start_at, sb.subscription_id if sb else None
        )
        person = people.get(r.person_id)
        clients.append(
            AffectedClient(
                person_id=r.person_id,
                person_name=(person.full_name if person else None) or f"Persona {r.person_id}",
                phone=person.phone_number if person else None,
                reservation_id=r.id,
                reservation_status=r.status,
                source=r.source,
                seat_id=r.seat_id,
                seat_label=labels.get(r.seat_id),
                subscription_id=membership[0] if membership else None,
                plan_name=membership[1] if membership else None,
                membership_ends_at=membership[2] if membership else None,
                standing_booking_id=sb.id if sb else None,
            )
        )
    for sb in standing:
        if sb.person_id in has_row:
            continue
        membership = await _membership_for(db, sb.person_id, session.start_at, sb.subscription_id)
        person = people.get(sb.person_id)
        clients.append(
            AffectedClient(
                person_id=sb.person_id,
                person_name=(person.full_name if person else None) or f"Persona {sb.person_id}",
                phone=person.phone_number if person else None,
                reservation_id=None,
                reservation_status=None,
                source="standing",
                seat_id=sb.seat_id,
                seat_label=labels.get(sb.seat_id),
                subscription_id=membership[0] if membership else None,
                plan_name=membership[1] if membership else None,
                membership_ends_at=membership[2] if membership else None,
                standing_booking_id=sb.id,
                without_reservation=True,
            )
        )
    clients.sort(key=lambda c: (c.seat_label or "~", c.person_name))
    return clients


async def _active_takeover_for_session(db: AsyncSession, session_id: int) -> Optional[SpecialEventTakeover]:
    return (
        await db.execute(
            select(SpecialEventTakeover).where(
                SpecialEventTakeover.session_id == session_id,
                SpecialEventTakeover.status == TAKEOVER_ACTIVE,
            )
        )
    ).scalars().first()


def _reason(event: SpecialEvent) -> str:
    return (
        f"Clase sustituida por el evento especial «{event.name}» "
        f"({local_schedule_label(event.start_at, event.end_at, event.timezone)})."
    )


# ---------------------------------------------------------------------------------------------
# Preview / apply
# ---------------------------------------------------------------------------------------------
async def preview_takeover(db: AsyncSession, event: SpecialEvent) -> TakeoverPreview:
    blockers: List[str] = []
    if event.status in (EVENT_CANCELED, EVENT_COMPLETED):
        blockers.append("El evento está cancelado o terminado.")
    if event.end_at is None:
        blockers.append(
            "Define la hora de fin del evento: sin duración no se sabe qué clases se sustituyen."
        )
        return TakeoverPreview(event.id, [], blockers, "Falta la duración del evento.")

    sessions = await overlapping_regular_sessions(
        db, event, statuses=("scheduled", SESSION_DISPLACED)
    )
    out: List[AffectedSession] = []
    for session in sessions:
        taken_by_this = False
        if session.status == SESSION_DISPLACED:
            takeover = await _active_takeover_for_session(db, session.id)
            if takeover is None or takeover.event_id != event.id:
                blockers.append(
                    f"La clase {session.name or session.id} ya fue sustituida por otro evento."
                )
                continue
            taken_by_this = True
        clients = [] if taken_by_this else await _clients_of_session(db, session)
        out.append(
            AffectedSession(
                session_id=session.id,
                name=session.name or "Clase",
                start_at=session.start_at,
                end_at=session.end_at,
                status=session.status,
                capacity=session.capacity,
                taken_by_this_event=taken_by_this,
                clients=clients,
            )
        )

    preview = TakeoverPreview(event.id, out, blockers, "")
    preview.summary = _preview_summary(event, preview)
    return preview


def _preview_summary(event: SpecialEvent, preview: TakeoverPreview) -> str:
    pending = [s for s in preview.sessions if not s.taken_by_this_event]
    done = [s for s in preview.sessions if s.taken_by_this_event]
    if not preview.sessions:
        return "No hay clases regulares en ese horario y sede. No hace falta sustituir nada."
    parts: List[str] = []
    if pending:
        names = ", ".join(
            f"{s.name} ({local_schedule_label(s.start_at, s.end_at, event.timezone)})" for s in pending
        )
        n = preview.affected_clients
        where = "esa clase" if len(pending) == 1 else "esas clases"
        parts.append(
            f"Se sustituirá {len(pending)} clase(s): {names}. "
            f"{n} cliente(s) con lugar en {where} quedarán desplazados y aparecerán en la lista "
            "de seguimiento. Su reserva se conserva marcada como «desplazada»: no cuenta como "
            "falta ni consume clase, y su membresía no cambia. No se les envía ningún mensaje; "
            "el aviso y la compensación los registran ustedes."
        )
    if done:
        parts.append(f"{len(done)} clase(s) ya están sustituidas por este evento.")
    return " ".join(parts)


@dataclass
class TakeoverResult:
    sessions_taken: int
    clients_displaced: int
    takeover_ids: List[int]


async def apply_takeover(
    db: AsyncSession,
    event: SpecialEvent,
    *,
    actor_account_id: Optional[int],
    session_ids: Sequence[int],
    reason: Optional[str] = None,
) -> TakeoverResult:
    """Apply exactly the takeover the staff member previewed.

    ``session_ids`` must equal the classes the preview proposes *now*; if a class appeared or
    disappeared in between (another admin, a schedule change) the call is refused so nobody
    displaces clients they did not see.
    """
    await lock_special_event(db, event.id)
    preview = await preview_takeover(db, event)
    if preview.blockers:
        raise SpecialEventError(" ".join(preview.blockers))
    expected = sorted(preview.pending_session_ids)
    if not expected:
        raise SpecialEventError("No hay clases regulares por sustituir en ese horario.")
    if sorted(int(s) for s in session_ids) != expected:
        raise SpecialEventError(
            "Las clases afectadas cambiaron desde la vista previa. Vuelve a revisarla antes de "
            "aplicar la sustitución."
        )

    now = utcnow()
    result = TakeoverResult(sessions_taken=0, clients_displaced=0, takeover_ids=[])
    for session_id in expected:
        await lock_class_session(db, session_id)
        session = (
            await db.execute(
                select(ClassSession)
                .where(ClassSession.id == session_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars().one()
        if session.status != "scheduled":
            raise SpecialEventError(
                "Una de las clases cambió de estado mientras se aplicaba la sustitución."
            )
        takeover = SpecialEventTakeover(
            event_id=event.id,
            session_id=session.id,
            previous_status=session.status,
            status=TAKEOVER_ACTIVE,
            reason=(reason or "").strip() or None,
            applied_by=actor_account_id,
            applied_at=now,
        )
        db.add(takeover)
        await db.flush()
        session.status = SESSION_DISPLACED
        session.updated_at = now

        # Re-read the clients under the session lock (the preview was taken before it).
        clients = await _clients_of_session(db, session)
        for client in clients:
            await _displace_client(db, event, takeover, session, client, now)
            result.clients_displaced += 1
        result.sessions_taken += 1
        result.takeover_ids.append(takeover.id)
    await db.flush()
    return result


async def _displace_client(
    db: AsyncSession,
    event: SpecialEvent,
    takeover: SpecialEventTakeover,
    session: ClassSession,
    client: AffectedClient,
    now: datetime,
    *,
    reason_suffix: str = "",
) -> SpecialEventDisplacement:
    reservation_id = client.reservation_id
    previous_status = client.reservation_status
    if reservation_id is not None:
        reservation = await db.get(Reservation, reservation_id, populate_existing=True)
        previous_status = reservation.status
        reservation.status = RESERVATION_DISPLACED
    else:
        # The standing booking never materialized for this class: record the class the client
        # would have had (displaced from the start) so the follow-up list is complete and a
        # restoration can give it back.
        reservation = Reservation(
            session_id=session.id,
            person_id=client.person_id,
            seat_id=client.seat_id,
            status=RESERVATION_DISPLACED,
            reserved_at=now,
            source="standing",
        )
        db.add(reservation)
        await db.flush()
        reservation_id = reservation.id
        previous_status = "reserved"

    displacement = SpecialEventDisplacement(
        event_id=event.id,
        takeover_id=takeover.id,
        session_id=session.id,
        reservation_id=reservation_id,
        person_id=client.person_id,
        seat_id=client.seat_id,
        previous_reservation_status=previous_status,
        reservation_source=client.source,
        subscription_id=client.subscription_id,
        standing_booking_id=client.standing_booking_id,
        reason=_reason(event) + reason_suffix,
        follow_up_status=FOLLOW_UP_PENDING,
        created_at=now,
        updated_at=now,
    )
    db.add(displacement)
    await db.flush()
    return displacement


async def record_late_displacement(
    db: AsyncSession, session: ClassSession, standing_booking: StandingBooking
) -> Optional[SpecialEventDisplacement]:
    """Materialization hook: a standing booking reached a class that an event already took.

    Called for ``displaced`` sessions instead of creating a bookable reservation, so a client
    who enrolled after the takeover still shows up in the follow-up list (and is not booked into
    a class that will not happen). Returns None if the client is already recorded.
    """
    # Same per-class lock as apply_takeover/apply_restore and every booking path.
    await lock_class_session(db, session.id)
    takeover = await _active_takeover_for_session(db, session.id)
    if takeover is None:
        return None
    existing = (
        await db.execute(
            select(Reservation.id).where(
                Reservation.session_id == session.id,
                Reservation.person_id == standing_booking.person_id,
            )
        )
    ).first()
    if existing is not None:
        return None
    event = await db.get(SpecialEvent, takeover.event_id)
    membership = await _membership_for(
        db, standing_booking.person_id, session.start_at, standing_booking.subscription_id
    )
    person = await db.get(People, standing_booking.person_id)
    client = AffectedClient(
        person_id=standing_booking.person_id,
        person_name=(person.full_name if person else None) or "",
        phone=person.phone_number if person else None,
        reservation_id=None,
        reservation_status=None,
        source="standing",
        seat_id=standing_booking.seat_id,
        seat_label=None,
        subscription_id=membership[0] if membership else None,
        plan_name=membership[1] if membership else None,
        membership_ends_at=membership[2] if membership else None,
        standing_booking_id=standing_booking.id,
        without_reservation=True,
    )
    return await _displace_client(
        db, event, takeover, session, client, utcnow(),
        reason_suffix=" Inscripción posterior a la sustitución.",
    )


# ---------------------------------------------------------------------------------------------
# Restoration
# ---------------------------------------------------------------------------------------------
@dataclass
class RestoreDecision:
    displacement_id: int
    person_name: str
    session_id: int
    restore: bool
    reason: str


@dataclass
class RestorePreview:
    event_id: int
    sessions: List[Dict]
    decisions: List[RestoreDecision]
    blockers: List[str]
    summary: str


async def _restore_decisions(
    db: AsyncSession, takeover: SpecialEventTakeover, session: ClassSession, now: datetime
) -> List[RestoreDecision]:
    displacements = (
        await db.execute(
            select(SpecialEventDisplacement, People.full_name)
            .join(People, People.id == SpecialEventDisplacement.person_id)
            .where(
                SpecialEventDisplacement.takeover_id == takeover.id,
                SpecialEventDisplacement.restoration_status.is_(None),
            )
            .order_by(SpecialEventDisplacement.id)
        )
    ).all()
    decisions: List[RestoreDecision] = []
    for d, name in displacements:
        restore, reason = await _decide(db, d, session, now)
        decisions.append(RestoreDecision(d.id, name or f"Persona {d.person_id}", session.id, restore, reason))
    return decisions


async def _decide(db: AsyncSession, d: SpecialEventDisplacement, session: ClassSession, now: datetime):
    if session.start_at <= now:
        return False, "La clase ya pasó."
    if d.compensation_type is not None or d.follow_up_status == FOLLOW_UP_RESOLVED:
        return False, "Ya tiene una compensación registrada; no se reactiva automáticamente."
    reservation = (
        await db.get(Reservation, d.reservation_id, populate_existing=True)
        if d.reservation_id
        else None
    )
    if reservation is None or reservation.status != RESERVATION_DISPLACED:
        return False, "Su reserva cambió después de la sustitución; revísala manualmente."
    if reservation.seat_id is not None:
        taken = (
            await db.execute(
                select(Reservation.id).where(
                    Reservation.session_id == session.id,
                    Reservation.seat_id == reservation.seat_id,
                    Reservation.id != reservation.id,
                    Reservation.status.in_(("reserved", "checked_in")),
                )
            )
        ).first()
        if taken is not None:
            return False, "Su bicicleta ya está ocupada; asígnale otra manualmente."
    if d.notified_at is not None:
        return True, "Se reactiva. Ya se le había avisado de la sustitución: avísale que su clase vuelve."
    return True, "Se reactiva su reserva original."


async def preview_restore(db: AsyncSession, event: SpecialEvent) -> RestorePreview:
    blockers: List[str] = []
    if event.status not in (EVENT_CANCELED, EVENT_DRAFT):
        blockers.append(
            "Solo se puede restituir el horario regular de un evento cancelado o en borrador."
        )
    takeovers = (
        await db.execute(
            select(SpecialEventTakeover).where(
                SpecialEventTakeover.event_id == event.id,
                SpecialEventTakeover.status == TAKEOVER_ACTIVE,
            )
        )
    ).scalars().all()
    if not takeovers:
        blockers.append("Este evento no tiene clases sustituidas.")
    now = utcnow()
    sessions: List[Dict] = []
    decisions: List[RestoreDecision] = []
    for t in takeovers:
        session = await db.get(ClassSession, t.session_id)
        sessions.append(
            {
                "session_id": session.id,
                "name": session.name or "Clase",
                "start_at": session.start_at,
                "end_at": session.end_at,
                "in_past": session.start_at <= now,
            }
        )
        decisions.extend(await _restore_decisions(db, t, session, now))
    restored = sum(1 for d in decisions if d.restore)
    kept = len(decisions) - restored
    summary = (
        f"Se reactivarán {len(sessions)} clase(s) regular(es). {restored} reserva(s) vuelven a su "
        f"estado original; {kept} se quedan desplazadas (ya compensadas, modificadas o sin lugar) "
        "y siguen en la lista de seguimiento. No se envía ningún mensaje."
    ) if sessions else "No hay nada que restituir."
    return RestorePreview(event.id, sessions, decisions, blockers, summary)


@dataclass
class RestoreResult:
    sessions_restored: int
    reservations_restored: int
    reservations_kept: int


async def apply_restore(
    db: AsyncSession, event: SpecialEvent, *, actor_account_id: Optional[int]
) -> RestoreResult:
    await lock_special_event(db, event.id)
    preview = await preview_restore(db, event)
    if preview.blockers:
        raise SpecialEventError(" ".join(preview.blockers))
    now = utcnow()
    result = RestoreResult(0, 0, 0)
    takeovers = (
        await db.execute(
            select(SpecialEventTakeover)
            .where(
                SpecialEventTakeover.event_id == event.id,
                SpecialEventTakeover.status == TAKEOVER_ACTIVE,
            )
            .with_for_update()
        )
    ).scalars().all()
    for takeover in sorted(takeovers, key=lambda t: t.session_id):
        await lock_class_session(db, takeover.session_id)
        session = (
            await db.execute(
                select(ClassSession)
                .where(ClassSession.id == takeover.session_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars().one()
        decisions = await _restore_decisions(db, takeover, session, now)
        for decision in decisions:
            d = await db.get(SpecialEventDisplacement, decision.displacement_id)
            if decision.restore:
                reservation = await db.get(Reservation, d.reservation_id)
                reservation.status = d.previous_reservation_status or "reserved"
                d.restoration_status = RESTORATION_RESTORED
                result.reservations_restored += 1
            else:
                d.restoration_status = RESTORATION_KEPT
                result.reservations_kept += 1
            d.restoration_note = decision.reason
            d.restored_at = now
            d.updated_at = now
        if session.status == SESSION_DISPLACED:
            session.status = takeover.previous_status or "scheduled"
            session.updated_at = now
        takeover.status = TAKEOVER_RESTORED
        takeover.restored_at = now
        takeover.restored_by = actor_account_id
        result.sessions_restored += 1
    await db.flush()
    return result


# ---------------------------------------------------------------------------------------------
# Manual follow-up
# ---------------------------------------------------------------------------------------------
async def update_follow_up(
    db: AsyncSession,
    displacement_id: int,
    *,
    actor_account_id: Optional[int],
    notified: Optional[bool] = None,
    notified_at: Optional[datetime] = None,
    notification_channel: Optional[str] = None,
    notification_notes: Optional[str] = None,
    compensation_type: Optional[str] = None,
    compensation_notes: Optional[str] = None,
    resolved: Optional[bool] = None,
) -> SpecialEventDisplacement:
    """Record the manual follow-up. Nothing here messages the client or touches a membership:
    the extra class (the default compensation) is applied by ``bonus.apply_bonus_class``."""
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
    now = utcnow()

    if notified is True:
        d.notified_at = notified_at or d.notified_at or now
        d.notified_by = actor_account_id
    elif notified is False:
        d.notified_at = None
        d.notified_by = None
    if notification_channel is not None:
        d.notification_channel = notification_channel.strip()[:30] or None
    if notification_notes is not None:
        d.notification_notes = notification_notes.strip() or None

    if d.compensation_type == COMPENSATION_BONUS_CLASS and (
        (compensation_type is not None and compensation_type.strip() != COMPENSATION_BONUS_CLASS)
        or (compensation_notes is not None and compensation_notes.strip() != (d.compensation_notes or ""))
    ):
        raise SpecialEventError(
            "Ya se le dio la clase extra (su membresía ya se extendió): esa compensación no se cambia."
        )
    if compensation_type is not None:
        value = compensation_type.strip()
        if value == "":
            d.compensation_type = None
        elif value not in COMPENSATION_TYPES:
            raise SpecialEventError("Tipo de compensación desconocido.")
        elif value == COMPENSATION_BONUS_CLASS and d.compensation_type != COMPENSATION_BONUS_CLASS:
            raise SpecialEventError(
                "La clase extra se da con «Dar clase extra», que agrega la reserva y extiende la membresía."
            )
        else:
            d.compensation_type = value
    if compensation_notes is not None and d.compensation_type != COMPENSATION_BONUS_CLASS:
        d.compensation_notes = compensation_notes.strip() or None

    if resolved is True:
        if d.compensation_type is None:
            raise SpecialEventError("Indica cómo se resolvió (tipo de compensación) antes de cerrarlo.")
        d.follow_up_status = FOLLOW_UP_RESOLVED
        d.resolved_at = now
        d.resolved_by = actor_account_id
    elif resolved is False or d.follow_up_status != FOLLOW_UP_RESOLVED:
        d.follow_up_status = FOLLOW_UP_NOTIFIED if d.notified_at else FOLLOW_UP_PENDING
        if resolved is False:
            d.resolved_at = None
            d.resolved_by = None
    d.updated_at = now
    await db.flush()
    return d


@dataclass
class DisplacementRow:
    id: int
    event_id: int
    event_name: str
    session_id: int
    session_name: str
    session_start_at: datetime
    session_end_at: datetime
    session_status: str
    person_id: int
    person_name: str
    phone: Optional[str]
    seat_label: Optional[str]
    reservation_id: Optional[int]
    reservation_status: Optional[str]
    reservation_source: Optional[str]
    plan_name: Optional[str]
    membership_ends_at: Optional[datetime]
    reason: str
    follow_up_status: str
    notified_at: Optional[datetime]
    notified_by_name: Optional[str]
    notification_channel: Optional[str]
    notification_notes: Optional[str]
    compensation_type: Optional[str]
    compensation_notes: Optional[str]
    resolved_at: Optional[datetime]
    resolved_by_name: Optional[str]
    restoration_status: Optional[str]
    restoration_note: Optional[str]
    restored_at: Optional[datetime]
    created_at: datetime
    # While there is no compensation: the proposed extra class, or why there can be none.
    bonus_preview: Optional[str] = None
    bonus_blocker: Optional[str] = None


async def list_displacements(
    db: AsyncSession,
    *,
    event_id: Optional[int] = None,
    follow_up_status: Optional[str] = None,
) -> List[DisplacementRow]:
    stmt = (
        select(SpecialEventDisplacement, People, ClassSession, SpecialEvent)
        .join(People, People.id == SpecialEventDisplacement.person_id)
        .join(ClassSession, ClassSession.id == SpecialEventDisplacement.session_id)
        .join(SpecialEvent, SpecialEvent.id == SpecialEventDisplacement.event_id)
        .order_by(ClassSession.start_at, People.full_name)
    )
    if event_id is not None:
        stmt = stmt.where(SpecialEventDisplacement.event_id == int(event_id))
    if follow_up_status:
        stmt = stmt.where(SpecialEventDisplacement.follow_up_status == follow_up_status)
    rows = (await db.execute(stmt)).all()
    if not rows:
        return []

    seat_ids = {d.seat_id for d, *_ in rows if d.seat_id}
    labels = {
        sid: label
        for sid, label in (
            await db.execute(select(Seat.id, Seat.label).where(Seat.id.in_(list(seat_ids))))
        ).all()
    } if seat_ids else {}
    reservation_ids = {d.reservation_id for d, *_ in rows if d.reservation_id}
    statuses = {
        rid: status
        for rid, status in (
            await db.execute(
                select(Reservation.id, Reservation.status).where(
                    Reservation.id.in_(list(reservation_ids))
                )
            )
        ).all()
    } if reservation_ids else {}
    account_ids = {d.notified_by for d, *_ in rows if d.notified_by} | {
        d.resolved_by for d, *_ in rows if d.resolved_by
    }
    usernames = {
        aid: name
        for aid, name in (
            await db.execute(
                select(Account.id, People.full_name)
                .join(People, People.id == Account.person_id)
                .where(Account.id.in_(list(account_ids)))
            )
        ).all()
    } if account_ids else {}
    sub_ids = {d.subscription_id for d, *_ in rows if d.subscription_id}
    plans = {
        sid: (name, end_at)
        for sid, name, end_at in (
            await db.execute(
                select(MembershipSubscription.id, MembershipPlan.name, MembershipSubscription.end_at)
                .join(MembershipPlan, MembershipPlan.id == MembershipSubscription.plan_id)
                .where(MembershipSubscription.id.in_(list(sub_ids)))
            )
        ).all()
    } if sub_ids else {}
    bonuses = await bonus_previews(db, [d for d, *_ in rows])

    out: List[DisplacementRow] = []
    for d, person, session, event in rows:
        plan = plans.get(d.subscription_id)
        bonus_preview, bonus_blocker = bonuses.get(d.id, (None, None))
        out.append(
            DisplacementRow(
                id=d.id,
                event_id=event.id,
                event_name=event.name,
                session_id=session.id,
                session_name=session.name or "Clase",
                session_start_at=session.start_at,
                session_end_at=session.end_at,
                session_status=session.status,
                person_id=person.id,
                person_name=person.full_name or f"Persona {person.id}",
                phone=person.phone_number,
                seat_label=labels.get(d.seat_id),
                reservation_id=d.reservation_id,
                reservation_status=statuses.get(d.reservation_id),
                reservation_source=d.reservation_source,
                plan_name=plan[0] if plan else None,
                membership_ends_at=plan[1] if plan else None,
                reason=d.reason,
                follow_up_status=d.follow_up_status,
                notified_at=d.notified_at,
                notified_by_name=usernames.get(d.notified_by),
                notification_channel=d.notification_channel,
                notification_notes=d.notification_notes,
                compensation_type=d.compensation_type,
                compensation_notes=d.compensation_notes,
                resolved_at=d.resolved_at,
                resolved_by_name=usernames.get(d.resolved_by),
                restoration_status=d.restoration_status,
                restoration_note=d.restoration_note,
                restored_at=d.restored_at,
                created_at=d.created_at,
                bonus_preview=bonus_preview,
                bonus_blocker=bonus_blocker,
            )
        )
    return out


async def takeovers_for_event(db: AsyncSession, event_id: int) -> List[SpecialEventTakeover]:
    return list(
        (
            await db.execute(
                select(SpecialEventTakeover)
                .where(SpecialEventTakeover.event_id == int(event_id))
                .order_by(SpecialEventTakeover.applied_at)
            )
        ).scalars().all()
    )


__all__ = [
    "AffectedClient",
    "AffectedSession",
    "DisplacementRow",
    "RestoreDecision",
    "RestorePreview",
    "RestoreResult",
    "TakeoverPreview",
    "TakeoverResult",
    "apply_restore",
    "apply_takeover",
    "list_displacements",
    "preview_restore",
    "preview_takeover",
    "record_late_displacement",
    "takeovers_for_event",
    "update_follow_up",
]
