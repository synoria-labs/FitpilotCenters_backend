from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.classModel import (
    ClassSession,
    Reservation,
    StandingBooking,
    StandingBookingException,
)

from app.crud.locks import lock_class_session

from .utils import _session_has_capacity


def _init_materialization_stats() -> Dict[str, Any]:
    return {
        "processed_bookings": 0,
        "created_reservations": 0,
        "skipped_no_capacity": 0,
        "skipped_seat_taken": 0,
        "skipped_existing": 0,
        "skipped_exceptions": 0,
        "errors": [],
    }


async def _get_existing_reservation(
    db: AsyncSession,
    session_id: int,
    person_id: int,
) -> Optional[Reservation]:
    stmt = select(Reservation).where(
        and_(
            Reservation.session_id == session_id,
            Reservation.person_id == person_id,
        )
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _is_seat_taken(
    db: AsyncSession,
    session_id: int,
    seat_id: int,
) -> bool:
    seat_check_stmt = select(Reservation).where(
        and_(
            Reservation.session_id == session_id,
            Reservation.seat_id == seat_id,
            Reservation.status.in_(["reserved", "checked_in"]),
        )
    )
    seat_check_result = await db.execute(seat_check_stmt)
    return seat_check_result.scalar_one_or_none() is not None


async def materialize_standing_bookings(
    db: AsyncSession,
    window_weeks: int = 8,
    start_date: Optional[date] = None,
    subscription_id: Optional[int] = None,
    template_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Materialize standing bookings into actual reservations.

    This is the core algorithm that creates future reservations based on
    standing booking rules, respecting capacity and seat constraints.
    """
    if start_date is None:
        start_date = date.today()

    end_date = start_date + timedelta(weeks=window_weeks)
    stats = _init_materialization_stats()

    conditions = [
        StandingBooking.status == "active",
        StandingBooking.start_date <= end_date,
        StandingBooking.end_date >= start_date,
    ]

    if subscription_id is not None:
        conditions.append(StandingBooking.subscription_id == subscription_id)

    if template_id is not None:
        conditions.append(StandingBooking.template_id == template_id)

    stmt = (
        select(StandingBooking)
        .options(
            joinedload(StandingBooking.template),
            joinedload(StandingBooking.person),
        )
        .where(and_(*conditions))
    )

    result = await db.execute(stmt)
    standing_bookings = result.scalars().all()

    for sb in standing_bookings:
        stats["processed_bookings"] += 1

        try:
            await _materialize_single_standing_booking(db, sb, start_date, end_date, stats)
        except Exception as exc:
            stats["errors"].append(f"Error processing standing booking {sb.id}: {exc}")
            continue

    stats["materialized_count"] = stats["created_reservations"]
    stats["reservations_created"] = stats["created_reservations"]
    return stats


async def materialize_standing_bookings_for_session(
    db: AsyncSession,
    session_id: int,
) -> Dict[str, Any]:
    """
    Materialize standing bookings into reservations for a single session.
    Intended for real-time flows when a new session is created.
    """
    stats = _init_materialization_stats()

    session_stmt = select(ClassSession).options(joinedload(ClassSession.template)).where(
        ClassSession.id == session_id
    )
    session_result = await db.execute(session_stmt)
    session = session_result.scalar_one_or_none()

    if not session or not session.template_id or session.status != "scheduled":
        stats["materialized_count"] = 0
        stats["reservations_created"] = 0
        return stats

    template = session.template
    if not template or not template.is_active:
        stats["materialized_count"] = 0
        stats["reservations_created"] = 0
        return stats

    session_date = session.start_at.date()

    standing_stmt = select(StandingBooking).where(
        and_(
            StandingBooking.template_id == session.template_id,
            StandingBooking.status == "active",
            StandingBooking.start_date <= session_date,
            StandingBooking.end_date >= session_date,
        )
    )
    standing_result = await db.execute(standing_stmt)
    standing_bookings = standing_result.scalars().all()

    for sb in standing_bookings:
        stats["processed_bookings"] += 1
        try:
            await _create_reservation_if_possible(db, sb, session.id, stats, source="standing")
        except Exception as exc:
            stats["errors"].append(
                f"Error processing standing booking {sb.id} for session {session.id}: {exc}"
            )
            continue

    stats["materialized_count"] = stats["created_reservations"]
    stats["reservations_created"] = stats["created_reservations"]
    return stats


async def _materialize_single_standing_booking(
    db: AsyncSession,
    standing_booking: StandingBooking,
    start_date: date,
    end_date: date,
    stats: Dict[str, Any],
) -> None:
    """Materialize a single standing booking into reservations."""
    template = standing_booking.template
    if not template or not template.is_active:
        return

    exceptions_stmt = select(StandingBookingException).where(
        StandingBookingException.standing_booking_id == standing_booking.id
    )
    exceptions_result = await db.execute(exceptions_stmt)
    exceptions = {exc.session_date: exc for exc in exceptions_result.scalars().all()}

    sessions_stmt = (
        select(ClassSession)
        .where(
            and_(
                ClassSession.template_id == template.id,
                func.date(ClassSession.start_at) >= max(start_date, standing_booking.start_date),
                func.date(ClassSession.start_at) <= min(end_date, standing_booking.end_date),
                ClassSession.status == "scheduled",
            )
        )
        .order_by(ClassSession.start_at)
    )

    sessions_result = await db.execute(sessions_stmt)
    sessions = sessions_result.scalars().all()

    for session in sessions:
        session_date = session.start_at.date()

        if session_date in exceptions:
            exception = exceptions[session_date]
            stats["skipped_exceptions"] += 1

            if exception.action == "reschedule" and exception.new_session_id:
                await _create_reservation_if_possible(
                    db,
                    standing_booking,
                    exception.new_session_id,
                    stats,
                    source="override",
                    seat_id_override=exception.new_seat_id,
                )
            continue

        await _create_reservation_if_possible(db, standing_booking, session.id, stats, source="standing")


async def _create_reservation_if_possible(
    db: AsyncSession,
    standing_booking: StandingBooking,
    session_id: int,
    stats: Dict[str, Any],
    source: str = "standing",
    seat_id_override: Optional[int] = None,
) -> None:
    """Create a reservation if possible, respecting capacity and seat constraints."""
    # Serialize check+insert per session: the capacity count has no backing DB
    # constraint, so two concurrent materializations (or a manual booking racing a
    # materialization) could both see the last free spot and oversell (TOCTOU).
    await lock_class_session(db, session_id)

    existing = await _get_existing_reservation(db, session_id, standing_booking.person_id)
    if existing:
        stats["skipped_existing"] += 1
        return

    session_stmt = select(ClassSession).where(ClassSession.id == session_id)
    session_result = await db.execute(session_stmt)
    session = session_result.scalar_one_or_none()

    if not session:
        return

    seat_id = seat_id_override if seat_id_override is not None else standing_booking.seat_id

    if seat_id:
        if await _is_seat_taken(db, session_id, seat_id):
            stats["skipped_seat_taken"] += 1
            return
    else:
        has_capacity = await _session_has_capacity(db, session)
        if not has_capacity:
            stats["skipped_no_capacity"] += 1
            return

    reservation = Reservation(
        session_id=session_id,
        person_id=standing_booking.person_id,
        seat_id=seat_id,
        status="reserved",
        source=source,
    )

    # SAVEPOINT so a lone constraint violation (uq_session_person /
    # uq_reservations_seat_once) skips this reservation without poisoning the
    # whole batch's transaction.
    try:
        async with db.begin_nested():
            db.add(reservation)
            await db.flush()
    except IntegrityError as exc:
        if "uq_reservations_seat_once" in str(exc):
            stats["skipped_seat_taken"] += 1
        else:
            stats["skipped_existing"] += 1
        return

    stats["created_reservations"] += 1


async def get_materialization_preview(
    db: AsyncSession,
    standing_booking_id: int,
    window_weeks: int = 4,
) -> List[Dict[str, Any]]:
    """
    Preview what reservations would be created for a standing booking.
    Useful for showing users what their standing booking will generate.
    """
    sb_stmt = select(StandingBooking).options(joinedload(StandingBooking.template)).where(
        StandingBooking.id == standing_booking_id
    )

    sb_result = await db.execute(sb_stmt)
    standing_booking = sb_result.scalar_one_or_none()

    if not standing_booking:
        return []

    template = standing_booking.template
    if not template:
        return []

    start_date = max(date.today(), standing_booking.start_date)
    end_date = min(start_date + timedelta(weeks=window_weeks), standing_booking.end_date)

    exceptions_stmt = select(StandingBookingException).where(
        StandingBookingException.standing_booking_id == standing_booking_id
    )
    exceptions_result = await db.execute(exceptions_stmt)
    exceptions = {exc.session_date: exc for exc in exceptions_result.scalars().all()}

    sessions_stmt = (
        select(ClassSession)
        .where(
            and_(
                ClassSession.template_id == template.id,
                func.date(ClassSession.start_at) >= start_date,
                func.date(ClassSession.start_at) <= end_date,
                ClassSession.status == "scheduled",
            )
        )
        .order_by(ClassSession.start_at)
    )

    sessions_result = await db.execute(sessions_stmt)
    sessions = sessions_result.scalars().all()

    preview = []
    for session in sessions:
        session_date = session.start_at.date()

        exception = exceptions.get(session_date)
        if exception:
            if exception.action == "skip":
                preview.append(
                    {
                        "date": session_date,
                        "session_id": session.id,
                        "session_name": session.name,
                        "start_time": session.start_at,
                        "status": "skipped",
                        "reason": "Exception: skip",
                    }
                )
                continue
            if exception.action == "reschedule":
                preview.append(
                    {
                        "date": session_date,
                        "session_id": exception.new_session_id,
                        "session_name": f"Rescheduled: {session.name}",
                        "start_time": session.start_at,
                        "status": "rescheduled",
                        "reason": f"Rescheduled to session {exception.new_session_id}",
                    }
                )
                continue

        existing = await _get_existing_reservation(db, session.id, standing_booking.person_id)
        if existing:
            preview.append(
                {
                    "date": session_date,
                    "session_id": session.id,
                    "session_name": session.name,
                    "start_time": session.start_at,
                    "status": "existing",
                    "reason": "Reservation already exists",
                }
            )
            continue

        status = "will_create"
        reason = "Will be created"

        if standing_booking.seat_id:
            if await _is_seat_taken(db, session.id, standing_booking.seat_id):
                status = "blocked"
                reason = "Seat already taken"
        else:
            has_capacity = await _session_has_capacity(db, session)
            if not has_capacity:
                status = "blocked"
                reason = "Session at full capacity"

        preview.append(
            {
                "date": session_date,
                "session_id": session.id,
                "session_name": session.name,
                "start_time": session.start_at,
                "status": status,
                "reason": reason,
            }
        )

    return preview
