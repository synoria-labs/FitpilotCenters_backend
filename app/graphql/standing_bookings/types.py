"""
GraphQL types for Standing Bookings (Reservativos)
"""
from datetime import datetime, date
from typing import Optional, List, Any
import strawberry

from app.crud.standingBookingsCrud import (
    StandingBookingData, ClassTypeData, ClassTemplateData, SeatData
)


@strawberry.type
class StandingBooking:
    """Standing Booking GraphQL type"""
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
    person_name: Optional[str]
    template_name: Optional[str]
    class_type_name: Optional[str]
    venue_name: Optional[str]
    seat_label: Optional[str]
    weekday: Optional[int]
    start_time_local: Optional[str]

    @classmethod
    def from_data(cls, data: StandingBookingData) -> "StandingBooking":
        return cls(
            id=data.id,
            person_id=data.person_id,
            subscription_id=data.subscription_id,
            template_id=data.template_id,
            seat_id=data.seat_id,
            start_date=data.start_date,
            end_date=data.end_date,
            status=data.status,
            created_at=data.created_at,
            person_name=data.person_name,
            template_name=data.template_name,
            class_type_name=data.class_type_name,
            venue_name=data.venue_name,
            seat_label=data.seat_label,
            weekday=data.weekday,
            start_time_local=data.start_time_local
        )


@strawberry.type
class ClassType:
    """Class Type GraphQL type"""
    id: int
    code: str
    name: str
    description: Optional[str]

    @classmethod
    def from_data(cls, data: ClassTypeData) -> "ClassType":
        return cls(
            id=data.id,
            code=data.code,
            name=data.name,
            description=data.description
        )


@strawberry.type
class ClassTemplate:
    """Class Template GraphQL type"""
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
    class_type_name: Optional[str]
    venue_name: Optional[str]
    instructor_name: Optional[str]

    @classmethod
    def from_data(cls, data: ClassTemplateData) -> "ClassTemplate":
        return cls(
            id=data.id,
            class_type_id=data.class_type_id,
            venue_id=data.venue_id,
            default_capacity=data.default_capacity,
            default_duration_min=data.default_duration_min,
            weekday=data.weekday,
            start_time_local=data.start_time_local,
            instructor_id=data.instructor_id,
            name=data.name,
            is_active=data.is_active,
            class_type_name=data.class_type_name,
            venue_name=data.venue_name,
            instructor_name=data.instructor_name
        )


@strawberry.type
class AvailableSeat:
    """Available Seat GraphQL type"""
    id: int
    label: str
    venue_id: int
    is_active: bool
    seat_type_name: Optional[str]
    is_available: bool

    @classmethod
    def from_data(cls, data: SeatData) -> "AvailableSeat":
        return cls(
            id=data.id,
            label=data.label,
            venue_id=data.venue_id,
            is_active=data.is_active,
            seat_type_name=data.seat_type_name,
            is_available=data.is_available
        )


@strawberry.type
class MaterializationPreview:
    """Preview of what reservations would be created"""
    date: date
    session_id: int
    session_name: Optional[str]
    start_time: datetime
    status: str
    reason: str


@strawberry.type
class MaterializationStats:
    """Statistics from materialization process"""
    processed_bookings: int
    created_reservations: int
    skipped_no_capacity: int
    skipped_seat_taken: int
    skipped_existing: int
    skipped_exceptions: int
    errors: List[str]


# Input types for mutations
@strawberry.input
class CreateStandingBookingInput:
    """Input for creating a standing booking"""
    person_id: int
    subscription_id: int
    template_id: int
    start_date: date
    end_date: date
    seat_id: Optional[int] = None


@strawberry.input
class UpdateStandingBookingInput:
    """Input for updating a standing booking"""
    standing_booking_id: int
    template_id: Optional[int] = None
    seat_id: Optional[int] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    status: Optional[str] = None


@strawberry.input
class CreateStandingBookingExceptionInput:
    """Input for creating a standing booking exception"""
    standing_booking_id: int
    session_date: date
    action: str  # 'skip' or 'reschedule'
    new_session_id: Optional[int] = None
    new_seat_id: Optional[int] = None
    notes: Optional[str] = None


@strawberry.input
class GetClassTemplatesInput:
    """Input for filtering class templates"""
    class_type_id: Optional[int] = None
    venue_id: Optional[int] = None
    active_only: bool = True


@strawberry.input
class GetAvailableSeatsInput:
    """Input for getting available seats"""
    template_id: int
    date_to_check: Optional[date] = None


@strawberry.input
class GetStandingBookingsInput:
    """Input for filtering standing bookings"""
    person_id: Optional[int] = None
    template_id: Optional[int] = None
    status: Optional[str] = None
    active_only: bool = False


@strawberry.input
class MaterializeBookingsInput:
    """Input for materializing standing bookings"""
    window_weeks: int = 8
    start_date: Optional[date] = None


@strawberry.input
class GetMaterializationPreviewInput:
    """Input for getting materialization preview"""
    standing_booking_id: int
    window_weeks: int = 4


# Response types
@strawberry.type
class StandingBookingResponse:
    """Response for standing booking operations"""
    success: bool
    standing_booking: Optional[StandingBooking]
    message: str


@strawberry.type
class ClassTypesResponse:
    """Response for class types query"""
    class_types: List[ClassType]
    total_count: int


@strawberry.type
class ClassTemplatesResponse:
    """Response for class templates query"""
    templates: List[ClassTemplate]
    total_count: int


@strawberry.type
class AvailableSeatsResponse:
    """Response for available seats query"""
    seats: List[AvailableSeat]
    available_count: int
    total_count: int


@strawberry.type
class StandingBookingsResponse:
    """Response for standing bookings query"""
    standing_bookings: List[StandingBooking]
    total_count: int


@strawberry.type
class MaterializationResponse:
    """Response for materialization operation"""
    success: bool
    stats: Optional[MaterializationStats]
    message: str


@strawberry.type
class MaterializationPreviewResponse:
    """Response for materialization preview"""
    preview: List[MaterializationPreview]
    total_sessions: int


@strawberry.input
class RescheduleStandingBookingInput:
    """Input for rescheduling standing booking dates to a new template."""
    standing_booking_id: int
    start_date: date
    end_date: date
    target_template_id: int
    target_seat_id: Optional[int] = None
    strict: bool = False


@strawberry.type
class RescheduleCount:
    status: str
    count: int


@strawberry.type
class RescheduleStandingBookingItem:
    session_date: date
    standing_booking_id: int
    source_session_id: Optional[int]
    target_session_id: Optional[int]
    seat_id: Optional[int]
    status: str
    reason: str


@strawberry.type
class RescheduleStandingBookingPreviewResponse:
    items: List[RescheduleStandingBookingItem]
    counts: List[RescheduleCount]


@strawberry.type
class RescheduleStandingBookingResponse:
    success: bool
    items: List[RescheduleStandingBookingItem]
    counts: List[RescheduleCount]
    message: str


# Helper functions for converting data
def convert_materialization_stats(stats_dict: dict) -> MaterializationStats:
    """Convert stats dictionary to GraphQL type"""
    return MaterializationStats(
        processed_bookings=stats_dict.get('processed_bookings', 0),
        created_reservations=stats_dict.get('created_reservations', 0),
        skipped_no_capacity=stats_dict.get('skipped_no_capacity', 0),
        skipped_seat_taken=stats_dict.get('skipped_seat_taken', 0),
        skipped_existing=stats_dict.get('skipped_existing', 0),
        skipped_exceptions=stats_dict.get('skipped_exceptions', 0),
        errors=stats_dict.get('errors', [])
    )


def convert_materialization_preview(preview_list: List[dict]) -> List[MaterializationPreview]:
    """Convert preview list to GraphQL types"""
    return [
        MaterializationPreview(
            date=item['date'],
            session_id=item['session_id'],
            session_name=item.get('session_name'),
            start_time=item['start_time'],
            status=item['status'],
            reason=item['reason']
        )
        for item in preview_list
    ]


def convert_reschedule_items(items: List[Any]) -> List[RescheduleStandingBookingItem]:
    return [
        RescheduleStandingBookingItem(
            session_date=item.session_date,
            standing_booking_id=item.standing_booking_id,
            source_session_id=item.source_session_id,
            target_session_id=item.target_session_id,
            seat_id=item.seat_id,
            status=item.status,
            reason=item.reason
        )
        for item in items
    ]


def convert_reschedule_counts(counts: dict) -> List[RescheduleCount]:
    return [
        RescheduleCount(status=status, count=count)
        for status, count in counts.items()
    ]
