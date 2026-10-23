"""GraphQL types for the "Eventos especiales" screen.

Times travel two ways: ``start_at``/``end_at`` are instants (UTC), and the ``local_*`` strings
are the wall-clock date/time in the event's own zone, which is what the screen shows and what
the inputs take. The client never converts zones itself.
"""
from datetime import date, datetime
from typing import List, Optional

import strawberry


@strawberry.type
class SpecialEventSeatType:
    seat_id: int
    label: str
    status: str  # free | held | sold


@strawberry.type
class LinkedCampaignType:
    id: int
    name: str
    status: str


@strawberry.type
class SpecialEventType:
    id: int
    name: str
    description: Optional[str]
    includes: Optional[str]
    address: Optional[str]
    venue_id: int
    venue_name: Optional[str]
    venue_address: Optional[str]
    class_type_id: Optional[int]
    instructor_id: Optional[int]
    instructor_name: Optional[str]
    timezone: str
    start_at: datetime
    end_at: Optional[datetime]
    local_date: str
    local_start_time: str
    local_end_time: Optional[str]
    schedule_label: str
    price: float
    currency: str
    capacity: Optional[int]
    hold_minutes: int
    status: str
    sold: int
    held: int
    available: Optional[int]
    sales_open: bool
    sales_closed_reason: Optional[str]
    seats: List[SpecialEventSeatType]
    publish_blockers: List[str]
    payments_ready: bool
    payments_warning: Optional[str]
    active_takeovers: int
    pending_follow_ups: int
    linked_campaigns: List[LinkedCampaignType]
    cancel_reason: Optional[str]
    published_at: Optional[datetime]
    canceled_at: Optional[datetime]
    created_at: datetime


@strawberry.type
class SpecialEventOrderType:
    id: int
    participant_name: str
    participant_phone: Optional[str]
    person_id: Optional[int]
    seat_id: Optional[int]
    seat_label: Optional[str]
    status: str
    channel: str
    amount: float
    currency: str
    payment_method: Optional[str]
    paid_at: Optional[datetime]
    hold_expires_at: Optional[datetime]
    mp_status: Optional[str]
    mp_payment_id: Optional[str]
    review_reason: Optional[str]
    cancel_reason: Optional[str]
    refunded_at: Optional[datetime]
    notes: Optional[str]
    created_at: datetime


@strawberry.type
class SpecialEventDisplacementType:
    id: int
    event_id: int
    event_name: str
    session_id: int
    session_name: str
    session_start_at: datetime
    session_label: str
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


@strawberry.type
class SpecialEventTakeoverType:
    id: int
    session_id: int
    session_name: str
    session_label: str
    status: str
    previous_status: str
    reason: Optional[str]
    applied_at: datetime
    applied_by_name: Optional[str]
    restored_at: Optional[datetime]
    restored_by_name: Optional[str]


@strawberry.type
class SpecialEventDetailType:
    event: SpecialEventType
    orders: List[SpecialEventOrderType]
    displacements: List[SpecialEventDisplacementType]
    takeovers: List[SpecialEventTakeoverType]


@strawberry.type
class AffectedClientType:
    person_id: int
    person_name: str
    phone: Optional[str]
    reservation_id: Optional[int]
    reservation_status: Optional[str]
    source: Optional[str]
    seat_label: Optional[str]
    plan_name: Optional[str]
    membership_ends_at: Optional[datetime]
    without_reservation: bool


@strawberry.type
class AffectedSessionType:
    session_id: int
    name: str
    start_at: datetime
    end_at: datetime
    label: str
    status: str
    capacity: int
    taken_by_this_event: bool
    clients: List[AffectedClientType]


@strawberry.type
class TakeoverPreviewType:
    event_id: int
    sessions: List[AffectedSessionType]
    pending_session_ids: List[int]
    affected_clients: int
    blockers: List[str]
    summary: str


@strawberry.type
class RestoreDecisionType:
    displacement_id: int
    person_name: str
    session_id: int
    restore: bool
    reason: str


@strawberry.type
class RestorePreviewType:
    event_id: int
    session_labels: List[str]
    decisions: List[RestoreDecisionType]
    blockers: List[str]
    summary: str


@strawberry.type
class OptionType:
    id: int
    name: str


@strawberry.type
class VenueOptionType:
    id: int
    name: str
    address: Optional[str]
    seats: List[SpecialEventSeatType]


@strawberry.type
class SpecialEventFormOptionsType:
    venues: List[VenueOptionType]
    instructors: List[OptionType]
    class_types: List[OptionType]
    default_timezone: str
    payments_ready: bool
    payments_warning: Optional[str]


@strawberry.type
class SpecialEventResult:
    success: bool
    error: Optional[str] = None
    message: Optional[str] = None
    detail: Optional[SpecialEventDetailType] = None


@strawberry.type
class DisplacementResult:
    success: bool
    error: Optional[str] = None
    displacement: Optional[SpecialEventDisplacementType] = None


@strawberry.input
class SpecialEventInput:
    """Create: name, venue, date, start time and price are required. Update: None = no change."""

    name: Optional[str] = None
    description: Optional[str] = None
    includes: Optional[str] = None
    address: Optional[str] = None
    venue_id: Optional[int] = None
    class_type_id: Optional[int] = None
    instructor_id: Optional[int] = None
    timezone: Optional[str] = None
    local_date: Optional[date] = None
    start_time: Optional[str] = None  # "HH:MM", local to the event's zone
    end_time: Optional[str] = None  # "HH:MM", local to the event's zone
    price: Optional[float] = None
    capacity: Optional[int] = None
    seat_ids: Optional[List[int]] = None
    hold_minutes: Optional[int] = None


@strawberry.input
class DisplacementFollowUpInput:
    displacement_id: int
    notified: Optional[bool] = None
    notification_channel: Optional[str] = None
    notification_notes: Optional[str] = None
    compensation_type: Optional[str] = None
    compensation_notes: Optional[str] = None
    resolved: Optional[bool] = None


@strawberry.input
class AddParticipantInput:
    event_id: int
    participant_name: str
    participant_phone: str
    payment_method: str  # cash | card | transfer | courtesy
    amount: Optional[float] = None
    seat_id: Optional[int] = None
    notes: Optional[str] = None
