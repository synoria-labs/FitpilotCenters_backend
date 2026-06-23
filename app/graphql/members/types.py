from datetime import datetime
from typing import Optional, List
from decimal import Decimal

import strawberry
from app.crud.membersCrud import MemberData, MembershipSummary, StandingBookingInfo


@strawberry.type
class ActiveStandingBooking:
    template_id: int
    template_name: Optional[str]
    class_type_name: Optional[str]
    weekday: int
    start_time_local: str
    venue_name: Optional[str]
    instructor_name: Optional[str]

    @classmethod
    def from_info(cls, info: Optional[StandingBookingInfo]) -> Optional["ActiveStandingBooking"]:
        if not info:
            return None

        return cls(
            template_id=info.template_id,
            template_name=info.template_name,
            class_type_name=info.class_type_name,
            weekday=info.weekday,
            start_time_local=info.start_time_local,
            venue_name=info.venue_name,
            instructor_name=info.instructor_name
        )


@strawberry.type
class MembershipInfo:
    subscription_id: Optional[int]
    plan_name: Optional[str]
    start_date: Optional[datetime]
    end_date: Optional[datetime]
    price: Optional[float] = None
    duration_value: Optional[int] = None
    duration_unit: Optional[str] = None
    payment_amount: Optional[float] = None

    # Store the DB status for reference but don't expose it directly
    _db_status: strawberry.Private[str]

    @strawberry.field
    def status(self) -> str:
        """Calculate real status based on end_date using local calendar dates."""
        if self.end_date:
            from datetime import date as date_type
            today = date_type.today()
            end_local = self.end_date.astimezone().date() if self.end_date.tzinfo else self.end_date.date()

            if end_local < today:
                return "expired"
            else:
                return "active"

        # If no end_date, fall back to the DB status
        return self._db_status

    @strawberry.field
    def remaining_days(self) -> Optional[int]:
        """Calculate remaining days using local calendar dates."""
        if self.end_date:
            from datetime import date as date_type
            today = date_type.today()
            end_local = self.end_date.astimezone().date() if self.end_date.tzinfo else self.end_date.date()
            return (end_local - today).days

        return None

    @classmethod
    def from_summary(cls, summary: Optional[MembershipSummary]) -> Optional["MembershipInfo"]:
        if not summary:
            return None

        return cls(
            subscription_id=summary.subscription_id,
            plan_name=summary.plan_name,
            start_date=summary.start_date,
            end_date=summary.end_date,
            _db_status=summary.status,
            payment_amount=summary.payment_amount
        )


@strawberry.type
class Member:
    id: int
    full_name: str
    email: Optional[str]
    phone_number: Optional[str]
    wa_id: Optional[str]
    registration_date: datetime
    profile_picture_url: Optional[str]
    profile_picture_uploaded_at: Optional[datetime]
    active_membership: Optional[MembershipInfo]
    active_standing_booking: Optional[ActiveStandingBooking]
    total_payments: float
    last_activity: Optional[datetime]

    @classmethod
    def from_data(cls, data: MemberData) -> "Member":
        # Convert profile picture path to full URL if present
        profile_picture_url = None
        if data.profile_picture_path:
            # This will be handled by FastAPI static files
            profile_picture_url = f"/uploads/{data.profile_picture_path}"

        return cls(
            id=data.id,
            full_name=data.full_name,
            email=data.email,
            phone_number=data.phone_number,
            wa_id=data.wa_id,
            registration_date=data.registration_date,
            profile_picture_url=profile_picture_url,
            profile_picture_uploaded_at=data.profile_picture_uploaded_at,
            active_membership=MembershipInfo.from_summary(data.active_membership),
            active_standing_booking=ActiveStandingBooking.from_info(data.active_standing_booking),
            total_payments=data.total_payments,
            last_activity=data.last_activity
        )


@strawberry.type
class PaginatedMembers:
    items: List[Member]
    total: int


@strawberry.input
class CreateMemberInput:
    full_name: str
    email: Optional[str] = None
    phone_number: Optional[str] = None
    wa_id: Optional[str] = None


@strawberry.input
class UpdateMemberInput:
    member_id: int
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone_number: Optional[str] = None
    wa_id: Optional[str] = None


@strawberry.type
class MemberResponse:
    success: bool = False
    member: Optional[Member] = None
    message: str = ""
    error_code: Optional[str] = None
    error_cause: Optional[str] = None


@strawberry.type
class DeleteMemberResponse:
    success: bool
    message: str

