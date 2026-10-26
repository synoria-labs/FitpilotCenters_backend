"""
Modern GraphQL types for reservations.
"""
from datetime import datetime
from typing import Optional, List
import strawberry

from app.crud.reservationsCrud import ReservationData, SessionData, SeatData


@strawberry.type
class Reservation:
    """Reservation GraphQL type"""
    id: int
    session_id: int
    person_id: int
    seat_id: Optional[int]
    status: str
    reserved_at: datetime
    checkin_at: Optional[datetime]
    checkout_at: Optional[datetime]
    source: str

    # Related data
    person_name: Optional[str]
    seat_label: Optional[str]
    session_name: Optional[str]
    session_start: Optional[datetime]
    session_end: Optional[datetime]

    @classmethod
    def from_data(cls, data: ReservationData) -> "Reservation":
        return cls(
            id=data.id,
            session_id=data.session_id,
            person_id=data.person_id,
            seat_id=data.seat_id,
            status=data.status,
            reserved_at=data.reserved_at,
            checkin_at=data.checkin_at,
            checkout_at=data.checkout_at,
            source=data.source,
            person_name=data.person_name,
            seat_label=data.seat_label,
            session_name=data.session_name,
            session_start=data.session_start,
            session_end=data.session_end
        )


@strawberry.type
class Session:
    """Session GraphQL type with availability info"""
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
    has_reservation: bool = False

    @classmethod
    def from_data(cls, data: SessionData) -> "Session":
        return cls(
            id=data.id,
            name=data.name,
            start_at=data.start_at,
            end_at=data.end_at,
            capacity=data.capacity,
            available_spots=data.available_spots,
            reserved_count=data.reserved_count,
            class_type_name=data.class_type_name,
            venue_name=data.venue_name,
            instructor_name=data.instructor_name,
            has_reservation=data.has_reservation,
        )


@strawberry.type
class Seat:
    """Seat GraphQL type"""
    id: int
    label: str
    venue_id: int
    is_active: bool
    seat_type_name: Optional[str]
    is_available: bool

    @classmethod
    def from_data(cls, data: SeatData) -> "Seat":
        return cls(
            id=data.id,
            label=data.label,
            venue_id=data.venue_id,
            is_active=data.is_active,
            seat_type_name=data.seat_type_name,
            is_available=data.is_available
        )


# Input types for mutations
@strawberry.input
class CreateReservationInput:
    """Input for creating a reservation"""
    session_id: int
    person_id: int
    seat_id: Optional[int] = None
    source: str = "manual"


@strawberry.input
class GetSessionsInput:
    """Input for filtering sessions"""
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    class_type_id: Optional[int] = None
    venue_id: Optional[int] = None
    include_completed: bool = False
    person_id: Optional[int] = None


@strawberry.input
class GetReservationsInput:
    """Input for filtering reservations"""
    person_id: Optional[int] = None
    session_id: Optional[int] = None
    include_past: bool = False
    include_canceled: bool = False
    limit: int = 100


# Response types
@strawberry.type
class ReservationResponse:
    """Response for reservation operations"""
    success: bool
    reservation: Optional[Reservation]
    message: str


@strawberry.type
class CheckInResponse:
    """Response for check-in operations"""
    success: bool
    checkin_time: Optional[datetime]
    message: str


@strawberry.type
class SessionsResponse:
    """Response for sessions query"""
    sessions: List[Session]
    total_count: int


@strawberry.type
class ReservationsResponse:
    """Response for reservations query"""
    reservations: List[Reservation]
    total_count: int


@strawberry.type
class SeatsResponse:
    """Response for available seats query"""
    seats: List[Seat]
    available_count: int
    total_count: int
