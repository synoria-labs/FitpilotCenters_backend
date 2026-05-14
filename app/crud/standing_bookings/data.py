from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional

from app.models.classModel import StandingBooking


@dataclass
class StandingBookingData:
    """Data transfer object for Standing Booking with related data."""
    id: int
    person_id: int
    subscription_id: int
    template_id: int
    seat_id: Optional[int]
    start_date: date
    end_date: date
    status: str
    created_at: datetime

    # Related data
    person_name: Optional[str] = None
    template_name: Optional[str] = None
    class_type_name: Optional[str] = None
    venue_name: Optional[str] = None
    seat_label: Optional[str] = None
    weekday: Optional[int] = None
    start_time_local: Optional[str] = None


@dataclass
class ClassTypeData:
    """Data transfer object for Class Type."""
    id: int
    code: str
    name: str
    description: Optional[str] = None


@dataclass
class ClassTemplateData:
    """Data transfer object for Class Template with related data."""
    id: int
    class_type_id: int
    venue_id: int
    default_capacity: Optional[int]
    default_duration_min: int
    weekday: int
    start_time_local: str
    instructor_id: Optional[int]
    name: Optional[str]
    is_active: bool

    # Related data
    class_type_name: Optional[str] = None
    venue_name: Optional[str] = None
    instructor_name: Optional[str] = None


@dataclass
class SeatData:
    """Data transfer object for Seat with availability."""
    id: int
    label: str
    venue_id: int
    is_active: bool
    seat_type_name: Optional[str] = None
    is_available: bool = True


@dataclass
class RescheduleItem:
    session_date: date
    standing_booking_id: int
    source_session_id: Optional[int]
    target_session_id: Optional[int]
    seat_id: Optional[int]
    status: str
    reason: str


def _standing_booking_to_data(sb: StandingBooking) -> StandingBookingData:
    """Map StandingBooking model to StandingBookingData DTO."""
    return StandingBookingData(
        id=sb.id,
        person_id=sb.person_id,
        subscription_id=sb.subscription_id,
        template_id=sb.template_id,
        seat_id=sb.seat_id,
        start_date=sb.start_date,
        end_date=sb.end_date,
        status=sb.status,
        created_at=sb.created_at,
        person_name=sb.person.full_name if sb.person else None,
        template_name=sb.template.name if sb.template else None,
        class_type_name=sb.template.class_type.name if sb.template and sb.template.class_type else None,
        venue_name=sb.template.venue.name if sb.template and sb.template.venue else None,
        weekday=sb.template.weekday if sb.template else None,
        start_time_local=str(sb.template.start_time_local) if sb.template else None,
    )
