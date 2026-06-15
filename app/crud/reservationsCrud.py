"""
Modern CRUD operations for reservations.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional, List, Dict, Any
from decimal import Decimal

from sqlalchemy import select, and_, func, case
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, joinedload

from app.models import (
    Reservation, People, Seat, ClassSession, ClassType, Venue,
    SeatType, MembershipSubscription
)


@dataclass
class ReservationData:
    """Clean reservation data structure"""
    id: int
    session_id: int
    person_id: int
    seat_id: Optional[int]
    status: str
    reserved_at: datetime
    checkin_at: Optional[datetime] = None
    checkout_at: Optional[datetime] = None
    source: str = "manual"

    # Related data
    person_name: Optional[str] = None
    seat_label: Optional[str] = None
    session_name: Optional[str] = None
    session_start: Optional[datetime] = None
    session_end: Optional[datetime] = None


@dataclass
class SessionData:
    """Class session data with availability info"""
    id: int
    name: Optional[str]
    start_at: datetime
    end_at: datetime
    capacity: int
    available_spots: int
    reserved_count: int
    class_type_name: Optional[str]
    venue_name: Optional[str]
    instructor_name: Optional[str]


@dataclass
class SeatData:
    """Seat information"""
    id: int
    label: str
    venue_id: int
    is_active: bool
    seat_type_name: Optional[str]
    is_available: bool = True


async def create_reservation(
    db: AsyncSession,
    *,
    session_id: int,
    person_id: int,
    seat_id: Optional[int] = None,
    source: str = "manual",
    commit: bool = True
) -> Reservation:
    """Create a new reservation"""

    # Validate session exists and is not full
    session_result = await db.execute(
        select(ClassSession).where(ClassSession.id == session_id)
    )
    session = session_result.scalar_one_or_none()
    if not session:
        raise ValueError(f"Session {session_id} not found")

    if session.status != 'scheduled':
        raise ValueError(f"Cannot reserve session with status {session.status}")

    # Check if session is in the future
    if session.start_at <= datetime.now(timezone.utc):
        raise ValueError("Cannot reserve past sessions")

    # Validate person exists
    person_result = await db.execute(
        select(People).where(People.id == person_id)
    )
    person = person_result.scalar_one_or_none()
    if not person:
        raise ValueError(f"Person {person_id} not found")

    # Validate seat if provided
    if seat_id:
        seat_result = await db.execute(
            select(Seat).where(
                and_(
                    Seat.id == seat_id,
                    Seat.is_active == True
                )
            )
        )
        seat = seat_result.scalar_one_or_none()
        if not seat:
            raise ValueError(f"Seat {seat_id} not found or inactive")

        # Check if seat is already reserved for this session
        existing_seat_reservation = await db.execute(
            select(Reservation).where(
                and_(
                    Reservation.session_id == session_id,
                    Reservation.seat_id == seat_id,
                    Reservation.status.in_(['reserved', 'checked_in'])
                )
            )
        )
        if existing_seat_reservation.scalar_one_or_none():
            raise ValueError("Seat is already reserved for this session")

    # Check if person already has a reservation for this session
    existing_person_reservation = await db.execute(
        select(Reservation).where(
            and_(
                Reservation.session_id == session_id,
                Reservation.person_id == person_id,
                Reservation.status.in_(['reserved', 'checked_in'])
            )
        )
    )
    if existing_person_reservation.scalar_one_or_none():
        raise ValueError("Person already has an active reservation for this session")

    # Check capacity
    reserved_count_result = await db.execute(
        select(func.count(Reservation.id)).where(
            and_(
                Reservation.session_id == session_id,
                Reservation.status.in_(['reserved', 'checked_in'])
            )
        )
    )
    reserved_count = reserved_count_result.scalar() or 0

    if reserved_count >= session.capacity:
        raise ValueError("Session is at full capacity")

    # Create the reservation
    reservation = Reservation(
        session_id=session_id,
        person_id=person_id,
        seat_id=seat_id,
        status='reserved',
        reserved_at=datetime.now(timezone.utc),
        source=source
    )

    db.add(reservation)

    if commit:
        await db.commit()
        await db.refresh(reservation)
    else:
        await db.flush()

    return reservation


async def cancel_reservation(
    db: AsyncSession,
    reservation_id: int,
    commit: bool = True
) -> bool:
    """Cancel a reservation"""
    result = await db.execute(
        select(Reservation).where(Reservation.id == reservation_id)
    )
    reservation = result.scalar_one_or_none()

    if not reservation:
        raise ValueError(f"Reservation {reservation_id} not found")

    if reservation.status == 'canceled':
        return True  # Already canceled

    if reservation.status not in ['reserved', 'waitlisted']:
        raise ValueError(f"Cannot cancel reservation with status {reservation.status}")

    reservation.status = 'canceled'

    if commit:
        await db.commit()

    return True


async def check_in_reservation(
    db: AsyncSession,
    reservation_id: int,
    commit: bool = True
) -> datetime:
    """Check in a member for their reservation"""
    result = await db.execute(
        select(Reservation).where(Reservation.id == reservation_id)
    )
    reservation = result.scalar_one_or_none()

    if not reservation:
        raise ValueError(f"Reservation {reservation_id} not found")

    if reservation.status != 'reserved':
        raise ValueError(f"Cannot check in reservation with status {reservation.status}")

    checkin_time = datetime.now(timezone.utc)
    reservation.status = 'checked_in'
    reservation.checkin_at = checkin_time

    if commit:
        await db.commit()

    return checkin_time


async def checkout_reservation(
    db: AsyncSession,
    reservation_id: int,
    commit: bool = True
) -> datetime:
    """Check out a member from their reservation"""
    result = await db.execute(
        select(Reservation).where(Reservation.id == reservation_id)
    )
    reservation = result.scalar_one_or_none()

    if not reservation:
        raise ValueError(f"Reservation {reservation_id} not found")

    if reservation.status != 'checked_in':
        raise ValueError(f"Cannot check out reservation with status {reservation.status}")

    checkout_time = datetime.now(timezone.utc)
    reservation.checkout_at = checkout_time
    # Keep status as 'checked_in' to maintain history

    if commit:
        await db.commit()

    return checkout_time


async def get_reservation_by_id(
    db: AsyncSession,
    reservation_id: int
) -> Optional[ReservationData]:
    """Get a reservation by ID with related data"""
    result = await db.execute(
        select(Reservation).options(
            joinedload(Reservation.person),
            joinedload(Reservation.seat),
            joinedload(Reservation.session).joinedload(ClassSession.class_type)
        ).where(Reservation.id == reservation_id)
    )
    reservation = result.scalar_one_or_none()

    if not reservation:
        return None

    return ReservationData(
        id=reservation.id,
        session_id=reservation.session_id,
        person_id=reservation.person_id,
        seat_id=reservation.seat_id,
        status=reservation.status,
        reserved_at=reservation.reserved_at,
        checkin_at=reservation.checkin_at,
        checkout_at=reservation.checkout_at,
        source=reservation.source,
        person_name=reservation.person.full_name,
        seat_label=reservation.seat.label if reservation.seat else None,
        session_name=reservation.session.name,
        session_start=reservation.session.start_at,
        session_end=reservation.session.end_at
    )


async def get_person_reservations(
    db: AsyncSession,
    person_id: int,
    include_past: bool = False,
    include_canceled: bool = False,
    limit: int = 100
) -> List[ReservationData]:
    """Get reservations for a person"""
    query = (
        select(Reservation)
        .options(
            joinedload(Reservation.person),
            joinedload(Reservation.seat),
            joinedload(Reservation.session).joinedload(ClassSession.class_type),
        )
        .join(Reservation.session)
        .where(Reservation.person_id == person_id)
    )

    if not include_past:
        query = query.where(ClassSession.start_at >= datetime.now(timezone.utc))

    if not include_canceled:
        query = query.where(Reservation.status != 'canceled')

    query = query.order_by(ClassSession.start_at.desc()).limit(limit)

    result = await db.execute(query)
    reservations = result.scalars().all()

    return [
        ReservationData(
            id=r.id,
            session_id=r.session_id,
            person_id=r.person_id,
            seat_id=r.seat_id,
            status=r.status,
            reserved_at=r.reserved_at,
            checkin_at=r.checkin_at,
            checkout_at=r.checkout_at,
            source=r.source,
            person_name=r.person.full_name,
            seat_label=r.seat.label if r.seat else None,
            session_name=r.session.name,
            session_start=r.session.start_at,
            session_end=r.session.end_at
        )
        for r in reservations
    ]


async def get_session_reservations(
    db: AsyncSession,
    session_id: int,
    include_canceled: bool = False
) -> List[ReservationData]:
    """Get all reservations for a session"""
    query = select(Reservation).options(
        joinedload(Reservation.person),
        joinedload(Reservation.seat),
        joinedload(Reservation.session)
    ).where(Reservation.session_id == session_id)

    if not include_canceled:
        query = query.where(Reservation.status != 'canceled')

    query = query.order_by(Reservation.reserved_at)

    result = await db.execute(query)
    reservations = result.scalars().all()

    return [
        ReservationData(
            id=r.id,
            session_id=r.session_id,
            person_id=r.person_id,
            seat_id=r.seat_id,
            status=r.status,
            reserved_at=r.reserved_at,
            checkin_at=r.checkin_at,
            checkout_at=r.checkout_at,
            source=r.source,
            person_name=r.person.full_name,
            seat_label=r.seat.label if r.seat else None,
            session_name=r.session.name,
            session_start=r.session.start_at,
            session_end=r.session.end_at
        )
        for r in reservations
    ]


async def get_available_sessions(
    db: AsyncSession,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    class_type_id: Optional[int] = None,
    venue_id: Optional[int] = None
) -> List[SessionData]:
    """Get available sessions with capacity information"""
    if not start_date:
        start_date = datetime.now(timezone.utc)
    if not end_date:
        end_date = start_date + timedelta(days=7)

    # Reservation count per session via a correlated scalar subquery. This avoids a
    # GROUP BY which, combined with joinedload of class_type/venue/instructor, fails on
    # PostgreSQL ("column class_types_1.id must appear in the GROUP BY clause"): once the
    # joined tables' columns are SELECTed, Postgres requires them in GROUP BY. The subquery
    # keeps the count out of the outer SELECT, so no GROUP BY is needed.
    reserved_count_sq = (
        select(func.count(Reservation.id))
        .where(
            Reservation.session_id == ClassSession.id,
            Reservation.status.in_(['reserved', 'checked_in']),
        )
        .correlate(ClassSession)
        .scalar_subquery()
    )

    query = select(
        ClassSession,
        reserved_count_sq.label('reserved_count')
    ).options(
        joinedload(ClassSession.class_type),
        joinedload(ClassSession.venue),
        joinedload(ClassSession.instructor)
    ).where(
        and_(
            ClassSession.start_at >= start_date,
            ClassSession.start_at <= end_date,
            ClassSession.status == 'scheduled'
        )
    )

    if class_type_id:
        query = query.where(ClassSession.class_type_id == class_type_id)

    if venue_id:
        query = query.where(ClassSession.venue_id == venue_id)

    query = query.order_by(ClassSession.start_at)

    result = await db.execute(query)
    sessions_data = result.all()

    return [
        SessionData(
            id=session.id,
            name=session.name,
            start_at=session.start_at,
            end_at=session.end_at,
            capacity=session.capacity,
            reserved_count=reserved_count,
            available_spots=session.capacity - reserved_count,
            class_type_name=session.class_type.name if session.class_type else None,
            venue_name=session.venue.name if session.venue else None,
            instructor_name=session.instructor.full_name if session.instructor else None
        )
        for session, reserved_count in sessions_data
    ]


async def get_available_seats(
    db: AsyncSession,
    session_id: int
) -> List[SeatData]:
    """Get available seats for a session"""
    # Get the session to find the venue
    session_result = await db.execute(
        select(ClassSession).where(ClassSession.id == session_id)
    )
    session = session_result.scalar_one_or_none()

    if not session:
        raise ValueError(f"Session {session_id} not found")

    # Get all seats for this venue
    seats_result = await db.execute(
        select(Seat).options(
            joinedload(Seat.seat_type)
        ).where(
            and_(
                Seat.venue_id == session.venue_id,
                Seat.is_active == True
            )
        ).order_by(func.length(Seat.label), Seat.label)
    )
    seats = seats_result.scalars().all()

    # Get reserved seat IDs for this session
    reserved_seats_result = await db.execute(
        select(Reservation.seat_id).where(
            and_(
                Reservation.session_id == session_id,
                Reservation.seat_id.isnot(None),
                Reservation.status.in_(['reserved', 'checked_in'])
            )
        )
    )
    reserved_seat_ids = {row[0] for row in reserved_seats_result.fetchall()}

    return [
        SeatData(
            id=seat.id,
            label=seat.label,
            venue_id=seat.venue_id,
            is_active=seat.is_active,
            seat_type_name=seat.seat_type.name if seat.seat_type else None,
            is_available=seat.id not in reserved_seat_ids
        )
        for seat in seats
    ]


# ------------------------------
# Aggregation: sessions with seats and expiry flag
# ------------------------------
async def _get_sessions_with_seats_range(
    db: AsyncSession,
    start_date,
    end_date,
    class_type_id: Optional[int] = None,
    venue_id: Optional[int] = None,
    include_class_type_id: bool = False,
) -> List[Dict[str, Any]]:
    """Shared loader for sessions with per-seat occupancy and expiry flags."""
    session_query = (
        select(ClassSession)
        .options(
            joinedload(ClassSession.class_type),
            joinedload(ClassSession.venue),
        )
        .where(
            and_(
                func.date(ClassSession.start_at) >= start_date,
                func.date(ClassSession.start_at) <= end_date,
            )
        )
    )

    if class_type_id:
        session_query = session_query.where(ClassSession.class_type_id == class_type_id)

    if venue_id:
        session_query = session_query.where(ClassSession.venue_id == venue_id)

    session_query = session_query.order_by(ClassSession.start_at)

    sessions_result = await db.execute(session_query)
    sessions = sessions_result.scalars().all()

    results: List[Dict[str, Any]] = []
    seats_cache: Dict[int, List] = {}
    expiry_cache: Dict[tuple[int, date], bool] = {}

    async def _will_expire_soon(person_id: int, session_start_at: datetime) -> bool:
        cache_key = (person_id, session_start_at.date())
        if cache_key in expiry_cache:
            return expiry_cache[cache_key]

        ms_q = (
            select(MembershipSubscription)
            .where(
                and_(
                    MembershipSubscription.person_id == person_id,
                    MembershipSubscription.status.in_(['active', 'grace']),
                    MembershipSubscription.start_at <= session_start_at,
                    MembershipSubscription.end_at >= session_start_at,
                )
            )
            .order_by(MembershipSubscription.end_at.desc())
            .limit(1)
        )
        ms_res = await db.execute(ms_q)
        ms = ms_res.scalar_one_or_none()
        if not ms or not ms.end_at:
            expiry_cache[cache_key] = False
            return False

        days_left = (ms.end_at.date() - session_start_at.date()).days
        will_expire = 0 <= days_left <= 2
        expiry_cache[cache_key] = will_expire
        return will_expire

    for session in sessions:
        # Seats for this venue (cached)
        if session.venue_id not in seats_cache:
            seats_result = await db.execute(
                select(Seat)
                .options(joinedload(Seat.seat_type))
                .where(and_(Seat.venue_id == session.venue_id, Seat.is_active == True))
                .order_by(func.length(Seat.label), Seat.label)
            )
            seats_cache[session.venue_id] = seats_result.scalars().all()

        venue_seats = seats_cache[session.venue_id]

        # Reservations for this session mapped by seat_id
        reservations_result = await db.execute(
            select(Reservation)
            .options(joinedload(Reservation.person))
            .where(
                and_(
                    Reservation.session_id == session.id,
                    Reservation.seat_id.isnot(None),
                    Reservation.status.in_(['reserved', 'checked_in']),
                )
            )
        )
        reservations = reservations_result.scalars().all()
        reserved_by_seat: Dict[int, People] = {}
        for r in reservations:
            if r.seat_id is not None and r.person is not None:
                reserved_by_seat[r.seat_id] = r.person

        # Build seats payload
        seats_payload: List[Dict[str, Any]] = []
        for seat in venue_seats:
            occupant = reserved_by_seat.get(seat.id)
            if occupant:
                will_exp = await _will_expire_soon(occupant.id, session.start_at)
                seats_payload.append({
                    'seat_id': seat.id,
                    'label': seat.label,
                    'status': 'occupied',
                    'occupant': {
                        'person_id': occupant.id,
                        'full_name': occupant.full_name or '',
                    },
                    'will_expire_soon': will_exp,
                })
            else:
                seats_payload.append({
                    'seat_id': seat.id,
                    'label': seat.label,
                    'status': 'free',
                    'occupant': None,
                    'will_expire_soon': False,
                })

        item: Dict[str, Any] = {
            'id': session.id,
            'name': session.name,
            'start_at': session.start_at,
            'end_at': session.end_at,
            'capacity': session.capacity,
            'venue_id': session.venue_id,
            'template_id': session.template_id,
            'class_type_name': session.class_type.name if session.class_type else None,
            'seats': seats_payload,
        }

        if include_class_type_id:
            item['class_type_id'] = session.class_type_id

        results.append(item)

    return results


async def get_sessions_with_seats_by_date(
    db: AsyncSession,
    target_date,
    venue_id: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Return sessions for a given date including per-seat occupancy and will_expire_soon flag."""
    return await _get_sessions_with_seats_range(
        db,
        start_date=target_date,
        end_date=target_date,
        venue_id=venue_id,
    )


async def get_week_sessions_with_seats(
    db: AsyncSession,
    start_date,
    end_date,
    class_type_id: Optional[int] = None,
    venue_id: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Return sessions for a date range (e.g., a week) including per-seat occupancy.

    Optimized version that loads all sessions in a date range at once,
    with optional filtering by class type.

    Args:
        db: Database session
        start_date: Start date of the range (inclusive)
        end_date: End date of the range (inclusive)
        class_type_id: Optional filter by class type ID
        venue_id: Optional filter by venue ID

    Returns:
        List of session dictionaries with same structure as get_sessions_with_seats_by_date
    """
    return await _get_sessions_with_seats_range(
        db,
        start_date=start_date,
        end_date=end_date,
        class_type_id=class_type_id,
        venue_id=venue_id,
        include_class_type_id=True,
    )
