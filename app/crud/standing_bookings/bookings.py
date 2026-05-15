from datetime import date
from typing import List, Optional

from sqlalchemy import and_, select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.classModel import (
    StandingBooking,
    StandingBookingException,
    ClassTemplate,
    ClassSession,
    Reservation,
)
from app.models.membershipsModel import MembershipSubscription
from app.models.venueModel import Seat

from .data import StandingBookingData, _standing_booking_to_data


async def create_standing_booking(
    db: AsyncSession,
    person_id: int,
    subscription_id: int,
    template_id: int,
    start_date: date,
    end_date: date,
    seat_id: Optional[int] = None,
) -> StandingBooking:
    """Create a new standing booking."""
    subscription_stmt = select(MembershipSubscription).where(
        and_(
            MembershipSubscription.id == subscription_id,
            MembershipSubscription.person_id == person_id,
            MembershipSubscription.status == "active",
        )
    )
    subscription_result = await db.execute(subscription_stmt)
    subscription = subscription_result.scalar_one_or_none()

    if not subscription:
        raise ValueError("Active subscription not found for this person")

    template_stmt = select(ClassTemplate).where(ClassTemplate.id == template_id)
    template_result = await db.execute(template_stmt)
    template = template_result.scalar_one_or_none()

    if not template:
        raise ValueError("Class template not found")

    if not template.is_active:
        raise ValueError("Class template is not active")

    if seat_id:
        seat_stmt = select(Seat).where(
            and_(
                Seat.id == seat_id,
                Seat.venue_id == template.venue_id,
                Seat.is_active == True,
            )
        )
        seat_result = await db.execute(seat_stmt)
        seat = seat_result.scalar_one_or_none()

        if not seat:
            raise ValueError("Seat not found or not available for this venue")

    existing_stmt = select(StandingBooking).where(
        and_(
            StandingBooking.person_id == person_id,
            StandingBooking.template_id == template_id,
            StandingBooking.status == "active",
            StandingBooking.start_date <= end_date,
            StandingBooking.end_date >= start_date,
        )
    )
    existing_result = await db.execute(existing_stmt)
    existing = existing_result.scalar_one_or_none()

    if existing:
        raise ValueError("Active standing booking already exists for this person and template")

    if seat_id:
        seat_taken_stmt = (
            select(Reservation.id)
            .join(ClassSession, Reservation.session_id == ClassSession.id)
            .where(
                and_(
                    ClassSession.template_id == template_id,
                    func.date(ClassSession.start_at) >= start_date,
                    func.date(ClassSession.start_at) <= end_date,
                    Reservation.seat_id == seat_id,
                    Reservation.status.in_(["reserved", "checked_in"]),
                    Reservation.person_id != person_id,
                )
            )
            .limit(1)
        )
        seat_taken_result = await db.execute(seat_taken_stmt)
        seat_taken = seat_taken_result.scalar_one_or_none()

        if seat_taken:
            raise ValueError(f"Seat {seat_id} is already reserved by another person for this template")

    standing_booking = StandingBooking(
        person_id=person_id,
        subscription_id=subscription_id,
        template_id=template_id,
        seat_id=seat_id,
        start_date=start_date,
        end_date=end_date,
        status="active",
    )

    db.add(standing_booking)
    await db.flush()

    return standing_booking


async def get_standing_booking_by_id(
    db: AsyncSession,
    standing_booking_id: int,
) -> Optional[StandingBookingData]:
    """Get standing booking by ID with related data."""
    stmt = (
        select(StandingBooking)
        .options(
            joinedload(StandingBooking.person),
            joinedload(StandingBooking.template).joinedload(ClassTemplate.class_type),
            joinedload(StandingBooking.template).joinedload(ClassTemplate.venue),
        )
        .where(StandingBooking.id == standing_booking_id)
    )

    result = await db.execute(stmt)
    sb = result.scalar_one_or_none()

    if not sb:
        return None

    return _standing_booking_to_data(sb)


async def get_standing_bookings(
    db: AsyncSession,
    person_id: Optional[int] = None,
    template_id: Optional[int] = None,
    status: Optional[str] = None,
    active_only: bool = False,
) -> List[StandingBookingData]:
    """Get standing bookings with optional filtering."""
    stmt = select(StandingBooking).options(
        joinedload(StandingBooking.person),
        joinedload(StandingBooking.template).joinedload(ClassTemplate.class_type),
        joinedload(StandingBooking.template).joinedload(ClassTemplate.venue),
    )

    if person_id:
        stmt = stmt.where(StandingBooking.person_id == person_id)

    if template_id:
        stmt = stmt.where(StandingBooking.template_id == template_id)

    if status:
        stmt = stmt.where(StandingBooking.status == status)
    elif active_only:
        stmt = stmt.where(StandingBooking.status == "active")

    stmt = stmt.order_by(StandingBooking.created_at.desc())

    result = await db.execute(stmt)
    standing_bookings = result.scalars().all()

    return [_standing_booking_to_data(sb) for sb in standing_bookings]


async def update_standing_booking_status(
    db: AsyncSession,
    standing_booking_id: int,
    new_status: str,
) -> StandingBooking:
    """Update standing booking status."""
    if new_status not in ["active", "paused", "canceled"]:
        raise ValueError("Invalid status. Must be 'active', 'paused', or 'canceled'")

    stmt = select(StandingBooking).where(StandingBooking.id == standing_booking_id)
    result = await db.execute(stmt)
    standing_booking = result.scalar_one_or_none()

    if not standing_booking:
        raise ValueError("Standing booking not found")

    standing_booking.status = new_status
    await db.flush()

    return standing_booking


async def create_standing_booking_exception(
    db: AsyncSession,
    standing_booking_id: int,
    session_date: date,
    action: str,
    new_session_id: Optional[int] = None,
    new_seat_id: Optional[int] = None,
    notes: Optional[str] = None,
) -> StandingBookingException:
    """Create an exception for a standing booking."""
    if action not in ["skip", "reschedule"]:
        raise ValueError("Action must be 'skip' or 'reschedule'")

    if action == "reschedule" and not new_session_id:
        raise ValueError("new_session_id is required for reschedule action")

    sb_stmt = select(StandingBooking).where(StandingBooking.id == standing_booking_id)
    sb_result = await db.execute(sb_stmt)
    standing_booking = sb_result.scalar_one_or_none()

    if not standing_booking:
        raise ValueError("Standing booking not found")

    if new_session_id:
        session_stmt = select(ClassSession).where(ClassSession.id == new_session_id)
        session_result = await db.execute(session_stmt)
        session = session_result.scalar_one_or_none()

        if not session:
            raise ValueError("New session not found")

        if new_seat_id:
            seat_stmt = select(Seat).where(
                and_(
                    Seat.id == new_seat_id,
                    Seat.venue_id == session.venue_id,
                    Seat.is_active == True,
                )
            )
            seat_result = await db.execute(seat_stmt)
            seat = seat_result.scalar_one_or_none()
            if not seat:
                raise ValueError("New seat not found or not available for this venue")

    existing_stmt = select(StandingBookingException).where(
        and_(
            StandingBookingException.standing_booking_id == standing_booking_id,
            StandingBookingException.session_date == session_date,
        )
    )
    existing_result = await db.execute(existing_stmt)
    existing = existing_result.scalar_one_or_none()

    if existing:
        raise ValueError("Exception already exists for this date")

    exception = StandingBookingException(
        standing_booking_id=standing_booking_id,
        session_date=session_date,
        action=action,
        new_session_id=new_session_id,
        new_seat_id=new_seat_id,
        notes=notes,
    )

    db.add(exception)
    await db.flush()

    return exception
