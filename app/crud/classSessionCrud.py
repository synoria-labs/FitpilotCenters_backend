"""
CRUD operations for ClassSession management
Implements session creation, management, and template-based generation
"""
import logging
from datetime import datetime, date, time, timedelta
from typing import List, Optional, Dict, Any
from sqlalchemy import select, and_, or_, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload

from app.models.classModel import ClassSession, ClassTemplate, ClassType, Reservation
from app.models.venueModel import Venue

logger = logging.getLogger(__name__)


async def create_class_session(
    db: AsyncSession,
    template_id: Optional[int],
    class_type_id: int,
    venue_id: int,
    start_at: datetime,
    end_at: datetime,
    capacity: int,
    instructor_id: Optional[int] = None,
    name: Optional[str] = None,
    status: str = "scheduled"
) -> ClassSession:
    """Create a new class session"""
    session = ClassSession(
        template_id=template_id,
        class_type_id=class_type_id,
        venue_id=venue_id,
        start_at=start_at,
        end_at=end_at,
        capacity=capacity,
        instructor_id=instructor_id,
        name=name,
        status=status,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )

    db.add(session)
    try:
        await db.commit()
        await db.refresh(session)
    except SQLAlchemyError:
        await db.rollback()
        raise

    try:
        if session.template_id:
            from app.crud.standingBookingsCrud import materialize_standing_bookings_for_session
            stats = await materialize_standing_bookings_for_session(db, session.id)
            if stats.get("created_reservations", 0) > 0:
                await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.error("Standing booking materialization failed for session %s: %s", session.id, exc)

    return session


async def get_class_session_by_id(
    db: AsyncSession,
    session_id: int
) -> Optional[ClassSession]:
    """Get a class session by ID with all relationships"""
    query = select(ClassSession).options(
        selectinload(ClassSession.class_type),
        selectinload(ClassSession.venue),
        selectinload(ClassSession.template),
        selectinload(ClassSession.instructor),
        selectinload(ClassSession.reservations)
    ).where(ClassSession.id == session_id)

    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_sessions_by_template(
    db: AsyncSession,
    template_id: int,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    status: Optional[str] = None
) -> List[ClassSession]:
    """Get all sessions for a template within date range"""
    query = select(ClassSession).where(ClassSession.template_id == template_id)

    if start_date:
        query = query.where(func.date(ClassSession.start_at) >= start_date)
    if end_date:
        query = query.where(func.date(ClassSession.start_at) <= end_date)
    if status:
        query = query.where(ClassSession.status == status)

    query = query.order_by(ClassSession.start_at)
    result = await db.execute(query)
    return result.scalars().all()


async def generate_sessions_from_template(
    db: AsyncSession,
    template_id: int,
    start_date: date,
    end_date: date,
    *,
    commit: bool = True,
) -> List[ClassSession]:
    """Generate class sessions from a template for the given date range.

    ``commit=True`` (default) persists the new sessions and is what standalone callers
    (admin session generation, rolling-window maintenance) expect. ``commit=False`` only
    flushes, so a caller that owns the transaction (atomic enrollment) can include session
    generation in a single outer commit and roll it back on failure.
    """

    # Get the template
    template_query = select(ClassTemplate).options(
        selectinload(ClassTemplate.class_type),
        selectinload(ClassTemplate.venue)
    ).where(ClassTemplate.id == template_id)

    result = await db.execute(template_query)
    template = result.scalar_one_or_none()

    if not template or not template.is_active:
        return []

    sessions_to_create = []
    current_date = start_date

    while current_date <= end_date:
        # Check if current date matches template weekday (0=Sunday, 6=Saturday)
        if current_date.weekday() == (template.weekday - 1) % 7:  # Convert to Python weekday

            # Create datetime for session start
            session_start = datetime.combine(current_date, template.start_time_local)
            session_end = session_start + timedelta(minutes=template.default_duration_min)

            # Check if session already exists for this date
            existing_query = select(ClassSession).where(
                and_(
                    ClassSession.template_id == template_id,
                    func.date(ClassSession.start_at) == current_date
                )
            )
            existing_result = await db.execute(existing_query)
            existing_session = existing_result.scalar_one_or_none()

            if not existing_session:
                session = ClassSession(
                    template_id=template_id,
                    class_type_id=template.class_type_id,
                    venue_id=template.venue_id,
                    instructor_id=template.instructor_id,
                    name=template.name or f"{template.class_type.name} - {current_date.strftime('%Y-%m-%d')}",
                    start_at=session_start,
                    end_at=session_end,
                    capacity=template.default_capacity or 20,
                    status="scheduled",
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow()
                )
                sessions_to_create.append(session)

        current_date += timedelta(days=1)

    # Bulk insert sessions
    if sessions_to_create:
        db.add_all(sessions_to_create)
        try:
            if commit:
                await db.commit()
                # Refresh all created sessions
                for session in sessions_to_create:
                    await db.refresh(session)
            else:
                # Caller owns the transaction: only flush so the new sessions are visible
                # in-transaction (for materialization) without committing them.
                await db.flush()
        except SQLAlchemyError:
            await db.rollback()
            raise

    return sessions_to_create


async def update_session_capacity(
    db: AsyncSession,
    session_id: int,
    new_capacity: int
) -> Optional[ClassSession]:
    """Update session capacity"""
    session = await get_class_session_by_id(db, session_id)
    if not session:
        return None

    session.capacity = new_capacity
    session.updated_at = datetime.utcnow()

    try:
        await db.commit()
        await db.refresh(session)
        return session
    except SQLAlchemyError:
        await db.rollback()
        raise


async def update_session_status(
    db: AsyncSession,
    session_id: int,
    new_status: str
) -> Optional[ClassSession]:
    """Update session status (scheduled, canceled, completed)"""
    session = await get_class_session_by_id(db, session_id)
    if not session:
        return None

    session.status = new_status
    session.updated_at = datetime.utcnow()

    try:
        await db.commit()
        await db.refresh(session)
        return session
    except SQLAlchemyError:
        await db.rollback()
        raise


async def cancel_session(
    db: AsyncSession,
    session_id: int,
    cancel_reservations: bool = True
) -> Optional[ClassSession]:
    """Cancel a session and optionally cancel all reservations"""
    session = await update_session_status(db, session_id, "canceled")

    if session and cancel_reservations:
        # Cancel all reservations for this session
        from app.crud.reservationsCrud import cancel_reservations_for_session
        await cancel_reservations_for_session(db, session_id)

    return session


async def get_sessions_by_date_range(
    db: AsyncSession,
    start_date: date,
    end_date: date,
    venue_id: Optional[int] = None,
    instructor_id: Optional[int] = None,
    class_type_id: Optional[int] = None,
    status: Optional[str] = None
) -> List[ClassSession]:
    """Get sessions within date range with optional filters"""
    query = select(ClassSession).options(
        selectinload(ClassSession.class_type),
        selectinload(ClassSession.venue),
        selectinload(ClassSession.template),
        selectinload(ClassSession.instructor)
    ).where(
        and_(
            func.date(ClassSession.start_at) >= start_date,
            func.date(ClassSession.start_at) <= end_date
        )
    )

    if venue_id:
        query = query.where(ClassSession.venue_id == venue_id)
    if instructor_id:
        query = query.where(ClassSession.instructor_id == instructor_id)
    if class_type_id:
        query = query.where(ClassSession.class_type_id == class_type_id)
    if status:
        query = query.where(ClassSession.status == status)

    query = query.order_by(ClassSession.start_at)
    result = await db.execute(query)
    return result.scalars().all()


async def get_session_capacity_info(
    db: AsyncSession,
    session_id: int
) -> Optional[Dict[str, Any]]:
    """Get session capacity information including current reservations"""
    session = await get_class_session_by_id(db, session_id)
    if not session:
        return None

    # Count current reservations by status
    reservation_count_query = select(
        Reservation.status,
        func.count(Reservation.id).label("count")
    ).where(
        and_(
            Reservation.session_id == session_id,
            Reservation.status.in_(["reserved", "checked_in", "waitlisted"])
        )
    ).group_by(Reservation.status)

    result = await db.execute(reservation_count_query)
    reservation_counts = {row.status: row.count for row in result}

    reserved_count = reservation_counts.get("reserved", 0)
    checked_in_count = reservation_counts.get("checked_in", 0)
    waitlisted_count = reservation_counts.get("waitlisted", 0)

    total_reserved = reserved_count + checked_in_count
    available_spots = max(0, session.capacity - total_reserved)

    return {
        "session_id": session_id,
        "capacity": session.capacity,
        "reserved": reserved_count,
        "checked_in": checked_in_count,
        "waitlisted": waitlisted_count,
        "total_reserved": total_reserved,
        "available_spots": available_spots,
        "is_full": total_reserved >= session.capacity
    }


async def maintain_session_window(
    db: AsyncSession,
    weeks_ahead: int = 8
) -> Dict[str, Any]:
    """Maintain a rolling window of future sessions for all active templates"""
    end_date = date.today() + timedelta(weeks=weeks_ahead)
    start_date = date.today()

    # Get all active templates
    templates_query = select(ClassTemplate).where(ClassTemplate.is_active == True)
    result = await db.execute(templates_query)
    templates = result.scalars().all()

    stats = {
        "templates_processed": 0,
        "sessions_created": 0,
        "templates_with_sessions": []
    }

    for template in templates:
        sessions_created = await generate_sessions_from_template(
            db, template.id, start_date, end_date
        )

        stats["templates_processed"] += 1
        stats["sessions_created"] += len(sessions_created)

        if sessions_created:
            stats["templates_with_sessions"].append({
                "template_id": template.id,
                "template_name": template.name,
                "sessions_created": len(sessions_created),
                "date_range": f"{start_date} to {end_date}"
            })

    return stats
