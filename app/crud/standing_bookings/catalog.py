from datetime import date, datetime, timedelta
from typing import List, Optional

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.classModel import ClassTemplate, ClassType, ClassSession, Reservation
from app.models.venueModel import Seat

from .data import ClassTemplateData, ClassTypeData, SeatData


async def get_class_types(db: AsyncSession) -> List[ClassTypeData]:
    """Get all class types."""
    stmt = select(ClassType).order_by(ClassType.name)
    result = await db.execute(stmt)
    class_types = result.scalars().all()

    return [
        ClassTypeData(
            id=ct.id,
            code=ct.code,
            name=ct.name,
            description=ct.description,
        )
        for ct in class_types
    ]


async def get_class_templates(
    db: AsyncSession,
    class_type_id: Optional[int] = None,
    venue_id: Optional[int] = None,
    active_only: bool = True,
) -> List[ClassTemplateData]:
    """Get class templates with optional filtering."""
    stmt = select(ClassTemplate).options(
        joinedload(ClassTemplate.class_type),
        joinedload(ClassTemplate.venue),
    )

    if active_only:
        stmt = stmt.where(ClassTemplate.is_active == True)

    if class_type_id:
        stmt = stmt.where(ClassTemplate.class_type_id == class_type_id)

    if venue_id:
        stmt = stmt.where(ClassTemplate.venue_id == venue_id)

    stmt = stmt.order_by(ClassTemplate.weekday, ClassTemplate.start_time_local)

    result = await db.execute(stmt)
    templates = result.scalars().all()

    return [
        ClassTemplateData(
            id=tmpl.id,
            class_type_id=tmpl.class_type_id,
            venue_id=tmpl.venue_id,
            default_capacity=tmpl.default_capacity,
            default_duration_min=tmpl.default_duration_min,
            weekday=tmpl.weekday,
            start_time_local=str(tmpl.start_time_local),
            instructor_id=tmpl.instructor_id,
            name=tmpl.name,
            is_active=tmpl.is_active,
            class_type_name=tmpl.class_type.name if tmpl.class_type else None,
            venue_name=tmpl.venue.name if tmpl.venue else None,
        )
        for tmpl in templates
    ]


async def get_available_seats_for_template(
    db: AsyncSession,
    template_id: int,
    date_to_check: Optional[date] = None,
) -> List[SeatData]:
    """
    Get available seats for a specific template.

    Availability is determined by actual reservations for the session date.
    """
    # First get the template to know the venue
    template_stmt = select(ClassTemplate).where(ClassTemplate.id == template_id)
    template_result = await db.execute(template_stmt)
    template = template_result.scalar_one_or_none()

    if not template:
        return []

    # Get all seats for this venue
    seats_stmt = select(Seat).where(
        and_(
            Seat.venue_id == template.venue_id,
            Seat.is_active == True,
        )
    ).order_by(Seat.label)

    seats_result = await db.execute(seats_stmt)
    seats = seats_result.scalars().all()

    if date_to_check is None:
        base_date = date.today()
        weekday = getattr(template, "weekday", None)
        if isinstance(weekday, int):
            target = (weekday - 1) % 7
            delta = (target - base_date.weekday()) % 7
            date_to_check = base_date + timedelta(days=delta)
        else:
            date_to_check = base_date
    elif isinstance(date_to_check, datetime):
        date_to_check = date_to_check.date()

    # Check which seats are taken for the specific date
    session_stmt = select(ClassSession).where(
        and_(
            ClassSession.template_id == template_id,
            func.date(ClassSession.start_at) == date_to_check,
        )
    )
    session_result = await db.execute(session_stmt)
    session = session_result.scalar_one_or_none()

    taken_seat_ids = set()
    if session:
        # Get reserved seat IDs for this session
        reservations_stmt = select(Reservation.seat_id).where(
            and_(
                Reservation.session_id == session.id,
                Reservation.seat_id.isnot(None),
                Reservation.status.in_(["reserved", "checked_in"]),
            )
        )
        reservations_result = await db.execute(reservations_stmt)
        taken_seat_ids = {seat_id for seat_id, in reservations_result.fetchall() if seat_id}

    return [
        SeatData(
            id=seat.id,
            label=seat.label,
            venue_id=seat.venue_id,
            is_active=seat.is_active,
            is_available=seat.id not in taken_seat_ids,
        )
        for seat in seats
    ]
