from datetime import date
from typing import Dict, List, Optional

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.classModel import ClassTemplate, ClassSession, StandingBooking, Reservation
from app.models.venueModel import Seat


def _normalize_weekday_to_iso(weekday: Optional[int]) -> Optional[int]:
    if weekday is None:
        return None
    normalized = int(weekday)
    if normalized <= 0:
        normalized = 7 if normalized == 0 else ((normalized % 7) or 7)
    elif normalized > 7:
        normalized = ((normalized - 1) % 7) + 1
    return normalized


def _build_template_weekday_map(templates: List[ClassTemplate]) -> Dict[int, ClassTemplate]:
    mapping: Dict[int, ClassTemplate] = {}
    for template in templates:
        weekday = _normalize_weekday_to_iso(getattr(template, "weekday", None))
        if weekday is None:
            continue
        mapping[weekday] = template
    return mapping


async def _get_templates_in_same_group(
    db: AsyncSession,
    template_id: int,
) -> List[ClassTemplate]:
    """Get all templates in the same timeslot group (class_type + venue + start_time + instructor)."""
    ref_stmt = select(ClassTemplate).where(ClassTemplate.id == template_id)
    ref_result = await db.execute(ref_stmt)
    ref_template = ref_result.scalar_one_or_none()
    if not ref_template:
        return []

    stmt = select(ClassTemplate).where(
        and_(
            ClassTemplate.class_type_id == ref_template.class_type_id,
            ClassTemplate.venue_id == ref_template.venue_id,
            ClassTemplate.start_time_local == ref_template.start_time_local,
            ClassTemplate.is_active == True,
        )
    )

    if ref_template.instructor_id is not None:
        stmt = stmt.where(ClassTemplate.instructor_id == ref_template.instructor_id)
    else:
        stmt = stmt.where(ClassTemplate.instructor_id.is_(None))

    stmt = stmt.order_by(ClassTemplate.weekday)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _load_sessions_by_template_ids(
    db: AsyncSession,
    template_ids: List[int],
    start_date: date,
    end_date: date,
) -> Dict[tuple[int, date], ClassSession]:
    if not template_ids:
        return {}

    stmt = select(ClassSession).where(
        and_(
            ClassSession.template_id.in_(template_ids),
            func.date(ClassSession.start_at) >= start_date,
            func.date(ClassSession.start_at) <= end_date,
            ClassSession.status == "scheduled",
        )
    )
    result = await db.execute(stmt)
    sessions = result.scalars().all()
    return {(session.template_id, session.start_at.date()): session for session in sessions}


async def _get_group_standing_bookings(
    db: AsyncSession,
    standing_booking: StandingBooking,
    template_ids: List[int],
) -> Dict[int, StandingBooking]:
    if not template_ids:
        return {}

    stmt = select(StandingBooking).where(
        and_(
            StandingBooking.person_id == standing_booking.person_id,
            StandingBooking.subscription_id == standing_booking.subscription_id,
            StandingBooking.status == "active",
            StandingBooking.template_id.in_(template_ids),
        )
    )
    result = await db.execute(stmt)
    bookings = result.scalars().all()
    return {booking.template_id: booking for booking in bookings}


async def _get_person_reservation(
    db: AsyncSession,
    person_id: int,
    session_id: int,
) -> Optional[Reservation]:
    stmt = select(Reservation).where(
        and_(
            Reservation.person_id == person_id,
            Reservation.session_id == session_id,
            Reservation.status.in_(["reserved", "checked_in", "waitlisted"]),
        )
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _session_has_capacity(
    db: AsyncSession,
    session: ClassSession,
) -> bool:
    count_stmt = select(func.count(Reservation.id)).where(
        and_(
            Reservation.session_id == session.id,
            Reservation.status.in_(["reserved", "checked_in"]),
        )
    )
    result = await db.execute(count_stmt)
    reserved = result.scalar() or 0
    return reserved < session.capacity


async def _resolve_seat_for_session(
    db: AsyncSession,
    session: ClassSession,
    preferred_seat_id: Optional[int],
) -> tuple[Optional[int], Optional[str]]:
    if preferred_seat_id is None:
        return None, None

    seat_stmt = select(Seat).where(
        and_(
            Seat.id == preferred_seat_id,
            Seat.venue_id == session.venue_id,
            Seat.is_active == True,
        )
    )
    seat_result = await db.execute(seat_stmt)
    seat = seat_result.scalar_one_or_none()
    if not seat:
        return None, "Preferred seat not available"

    taken_stmt = select(Reservation).where(
        and_(
            Reservation.session_id == session.id,
            Reservation.seat_id == preferred_seat_id,
            Reservation.status.in_(["reserved", "checked_in"]),
        )
    )
    taken_result = await db.execute(taken_stmt)
    if taken_result.scalar_one_or_none() is None:
        return preferred_seat_id, None

    seats_stmt = select(Seat).where(
        and_(
            Seat.venue_id == session.venue_id,
            Seat.is_active == True,
        )
    ).order_by(Seat.label)
    seats_result = await db.execute(seats_stmt)
    seats = seats_result.scalars().all()

    for seat_item in seats:
        taken_stmt = select(Reservation).where(
            and_(
                Reservation.session_id == session.id,
                Reservation.seat_id == seat_item.id,
                Reservation.status.in_(["reserved", "checked_in"]),
            )
        )
        taken_result = await db.execute(taken_stmt)
        if taken_result.scalar_one_or_none() is None:
            return seat_item.id, "Preferred seat taken, auto-selected another"

    return None, "No seats available for target session"


async def _resolve_group_seat(
    db: AsyncSession,
    templates: List[ClassTemplate],
    window_start: date,
    window_end: date,
    person_id: int,
    preferred_seat_id: Optional[int],
) -> tuple[Optional[int], Optional[str]]:
    """Pick a seat free across ALL templates of a timeslot group for the whole window.

    A fixed-slot package reserves the SAME seat for every template in the group (e.g. Mon-Fri at
    8pm) across the entire membership period, so the seat must be free for every session of every
    group template in ``[window_start, window_end]`` -- mirroring the per-template check in
    ``create_standing_booking`` (bookings.py).

    Returns ``(seat_id, label)``:
    - keeps ``preferred_seat_id`` when it is free across the whole group/window (so a renewal
      preserves the member's existing bike);
    - otherwise returns the first free seat by label (auto-reassign to another available bike);
    - returns ``(preferred_seat_id, None)`` when NO seat is free, so the caller's existing
      failure/refund path still applies (genuinely full class);
    - returns ``(None, None)`` when the venue has no seats (capacity-based class).
    """
    if not templates:
        return preferred_seat_id, None

    venue_id = templates[0].venue_id

    seats_stmt = (
        select(Seat)
        .where(and_(Seat.venue_id == venue_id, Seat.is_active == True))
        .order_by(Seat.label)
    )
    seats = (await db.execute(seats_stmt)).scalars().all()
    if not seats:
        return None, None

    template_ids = [t.id for t in templates]
    taken_stmt = (
        select(Reservation.seat_id)
        .join(ClassSession, Reservation.session_id == ClassSession.id)
        .where(
            and_(
                ClassSession.template_id.in_(template_ids),
                func.date(ClassSession.start_at) >= window_start,
                func.date(ClassSession.start_at) <= window_end,
                Reservation.seat_id.isnot(None),
                Reservation.status.in_(["reserved", "checked_in"]),
                Reservation.person_id != person_id,
            )
        )
        .distinct()
    )
    taken_ids = {seat_id for (seat_id,) in (await db.execute(taken_stmt)).all()}

    free_seats = [s for s in seats if s.id not in taken_ids]
    if not free_seats:
        return preferred_seat_id, None

    if preferred_seat_id is not None:
        kept = next((s for s in free_seats if s.id == preferred_seat_id), None)
        if kept is not None:
            return kept.id, kept.label

    chosen = free_seats[0]
    return chosen.id, chosen.label
