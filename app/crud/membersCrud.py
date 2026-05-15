from dataclasses import dataclass
from datetime import datetime, timezone, date
from typing import Optional, List, Tuple
import time

import logging

from sqlalchemy import func, or_, select, and_, case, delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import (
    People, PersonRole, Role, MembershipSubscription, Reservation,
    Payment, StandingBooking, StandingBookingException, Account, ClassSession, ClassTemplate,
    Lead, LeadEvent, LeadAttribution, CommunicationOptIn, WhatsAppThread, FormSubmission
)
from app.core.conversions import coerce_int


logger = logging.getLogger(__name__)

@dataclass
class StandingBookingInfo:
    template_id: int
    template_name: Optional[str]
    class_type_name: Optional[str]
    weekday: int
    start_time_local: str
    venue_name: Optional[str]
    instructor_name: Optional[str]


@dataclass
class MembershipSummary:
    subscription_id: Optional[int]
    plan_name: Optional[str]
    start_date: Optional[datetime]
    end_date: Optional[datetime]
    status: str
    remaining_days: Optional[int]
    payment_amount: Optional[float] = None


@dataclass
class MemberData:
    id: int
    full_name: str
    email: Optional[str]
    phone_number: Optional[str]
    wa_id: Optional[str]
    registration_date: datetime
    profile_picture_path: Optional[str]
    profile_picture_uploaded_at: Optional[datetime]
    active_membership: Optional[MembershipSummary]
    active_standing_booking: Optional[StandingBookingInfo]
    total_payments: float
    last_activity: Optional[datetime]


def _member_sort_key(member: 'MemberData') -> Tuple[int, float, str]:
    """Sort key placing active memberships first, then by end date descending."""
    membership = member.active_membership
    status = (membership.status or '').lower() if membership and membership.status else ''

    if 'active' in status or 'activo' in status:
        priority = 0
    elif membership:
        priority = 1
    else:
        priority = 2

    end_timestamp = 0.0
    if membership and membership.end_date:
        try:
            end_timestamp = membership.end_date.timestamp()
        except Exception:  # noqa: BLE001
            end_timestamp = 0.0

    full_name = (member.full_name or '').lower()
    return (priority, -end_timestamp, full_name)


def _build_member_data(person: People) -> MemberData:
    """Build MemberData from a People instance with preloaded relationships."""
    active_subscription = None
    latest_subscription = None
    true_end_date = None
    true_start_date = None

    active_standing_booking_info = None
    if person.standing_bookings:
        current_date = date.today()

        for sb in person.standing_bookings:
            if sb.status == 'active' and sb.end_date >= current_date:
                if true_end_date is None or sb.end_date > true_end_date:
                    true_end_date = sb.end_date
                    true_start_date = sb.start_date

                    if hasattr(sb, 'template') and sb.template:
                        template = sb.template
                        class_type = getattr(template, 'class_type', None)
                        venue = getattr(template, 'venue', None)
                        instructor = getattr(template, 'instructor', None)

                        active_standing_booking_info = StandingBookingInfo(
                            template_id=template.id,
                            template_name=getattr(template, 'name', None),
                            class_type_name=getattr(class_type, 'name', None) if class_type else None,
                            weekday=getattr(template, 'weekday', 0),
                            start_time_local=str(getattr(template, 'start_time_local', '')),
                            venue_name=getattr(venue, 'name', None) if venue else None,
                            instructor_name=getattr(instructor, 'full_name', None) if instructor else None
                        )

    if person.subscriptions:
        current_time = datetime.now(timezone.utc)
        ordered_subs = sorted(
            person.subscriptions,
            key=lambda s: s.end_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True
        )
        if ordered_subs:
            latest_subscription = ordered_subs[0]
        for sub in ordered_subs:
            if sub.status == 'active' and sub.end_at and sub.end_at > current_time:
                active_subscription = sub
                break

    membership_summary = None
    reference_subscription = active_subscription or latest_subscription

    if reference_subscription or true_end_date:
        today_local = date.today()

        # Priority: subscription end_at over standing booking end_date
        if reference_subscription and reference_subscription.end_at:
            end_datetime = reference_subscription.end_at
            start_datetime = reference_subscription.start_at
            # Calculate remaining days using local calendar dates
            end_date_local = end_datetime.astimezone().date() if end_datetime.tzinfo else end_datetime.date()
            remaining_days = (end_date_local - today_local).days
            status = 'active' if remaining_days >= 0 else 'expired'
        elif true_end_date:
            end_datetime = datetime.combine(true_end_date, datetime.max.time()).replace(tzinfo=timezone.utc)
            start_datetime = datetime.combine(true_start_date, datetime.min.time()).replace(tzinfo=timezone.utc) if true_start_date else None
            remaining_days = (true_end_date - today_local).days
            status = 'active' if remaining_days >= 0 else 'expired'
        else:
            end_datetime = None
            start_datetime = None
            status = 'inactive'
            remaining_days = None

        plan_name = None
        subscription_id = None
        if reference_subscription:
            plan_name = reference_subscription.plan.name if reference_subscription.plan else 'Standing Booking'
            subscription_id = reference_subscription.id

        membership_summary = MembershipSummary(
            subscription_id=subscription_id,
            plan_name=plan_name,
            start_date=start_datetime,
            end_date=end_datetime,
            status=status,
            remaining_days=remaining_days
        )

    total_payments = sum(p.amount for p in person.payments if p.status == 'COMPLETED')

    last_activity = None
    if person.reservations:
        last_reservation = max(person.reservations, key=lambda r: r.reserved_at)
        last_activity = last_reservation.reserved_at

    registration_date = datetime.utcnow().replace(tzinfo=timezone.utc)
    if person.roles:
        registration_date = min(role.created_at for role in person.roles)

    return MemberData(
        id=person.id,
        full_name=person.full_name or "Sin nombre",
        email=person.email,
        phone_number=person.phone_number,
        wa_id=person.wa_id,
        registration_date=registration_date,
        profile_picture_path=person.profile_picture_path,
        profile_picture_uploaded_at=person.profile_picture_uploaded_at,
        active_membership=membership_summary,
        active_standing_booking=active_standing_booking_info,
        total_payments=float(total_payments) if total_payments else 0.0,
        last_activity=last_activity
    )


async def get_members_list(
    db: AsyncSession,
    limit: Optional[int] = None,
    offset: int = 0,
    search: Optional[str] = None
) -> List[MemberData]:
    """Get comprehensive list of members with optional filters."""

    current_time = datetime.now(timezone.utc)

    active_membership_rank = (
        select(
            func.max(
                case(
                    (
                        and_(
                            MembershipSubscription.status == 'active',
                            MembershipSubscription.end_at.isnot(None),
                            MembershipSubscription.end_at > current_time,
                        ),
                        1,
                    ),
                    else_=0,
                )
            )
        )
        .where(MembershipSubscription.person_id == People.id)
        .scalar_subquery()
    )

    latest_membership_end = (
        select(func.max(MembershipSubscription.end_at))
        .where(MembershipSubscription.person_id == People.id)
        .scalar_subquery()
    )

    # Base query for people with member role
    query = (
        select(People)
        .join(PersonRole)
        .join(Role)
        .options(
            selectinload(People.roles).selectinload(PersonRole.role),
            selectinload(People.subscriptions).selectinload(MembershipSubscription.plan),
            selectinload(People.payments),
            selectinload(People.reservations).selectinload(Reservation.session),
            selectinload(People.standing_bookings).selectinload(StandingBooking.template).selectinload(ClassTemplate.class_type),
            selectinload(People.standing_bookings).selectinload(StandingBooking.template).selectinload(ClassTemplate.venue),
            selectinload(People.standing_bookings).selectinload(StandingBooking.template).selectinload(ClassTemplate.instructor)
        )
        .where(Role.code == 'member')
        .where(People.deleted_at.is_(None))
        .where(
            select(MembershipSubscription.id)
            .where(MembershipSubscription.person_id == People.id)
            .exists()
        )
        .order_by(
            func.coalesce(active_membership_rank, 0).desc(),
            latest_membership_end.desc(),
            People.full_name
        )
    )

    if search:
        search_term = f"%{search.lower()}%"
        query = query.where(
            or_(
                func.lower(People.full_name).like(search_term),
                func.lower(People.email).like(search_term),
                func.lower(People.phone_number).like(search_term)
            )
        )

    if offset:
        query = query.offset(offset)

    if limit:
        query = query.limit(limit)

    result = await db.execute(query)
    people = result.scalars().all()

    members_data = [_build_member_data(person) for person in people]

    members_data.sort(key=_member_sort_key)
    return members_data


async def get_member_by_id(db: AsyncSession, member_id: int) -> Optional[MemberData]:
    """Get detailed member information by ID"""
    member_id = coerce_int(member_id)
    if member_id is None:
        return None

    result = await db.execute(
        select(People)
        .options(
            selectinload(People.roles).selectinload(PersonRole.role),
            selectinload(People.subscriptions).selectinload(MembershipSubscription.plan),
            selectinload(People.payments),
            selectinload(People.reservations).selectinload(Reservation.session),
            selectinload(People.standing_bookings).selectinload(StandingBooking.template).selectinload(ClassTemplate.class_type),
            selectinload(People.standing_bookings).selectinload(StandingBooking.template).selectinload(ClassTemplate.venue),
            selectinload(People.standing_bookings).selectinload(StandingBooking.template).selectinload(ClassTemplate.instructor)
        )
        .where(People.id == member_id)
        .where(People.deleted_at.is_(None))
    )

    person = result.scalar_one_or_none()
    if not person:
        return None

    # Check if person has member role
    is_member = any(role.role.code == 'member' for role in person.roles)
    if not is_member:
        return None

    return _build_member_data(person)


async def create_member(
    db: AsyncSession,
    full_name: str,
    email: Optional[str] = None,
    phone_number: Optional[str] = None,  # WhatsApp stored here
    commit: bool = True
) -> People:
    """Create a new member (person with member role)."""

    # Create person
    person = People(
        full_name=full_name,
        email=email,
        phone_number=phone_number  # WhatsApp number
    )
    db.add(person)
    await db.flush()  # Get ID without committing

    # Assign member role
    member_role = await db.execute(select(Role).where(Role.code == 'member'))
    role = member_role.scalar_one()

    person_role = PersonRole(
        person_id=person.id,
        role_id=role.id
    )
    db.add(person_role)
    await db.flush()

    if commit:
        await db.commit()
        await db.refresh(person)

    return person


async def update_member(db: AsyncSession, member_id: int, **kwargs) -> Optional[People]:
    """Update member information"""
    started_at = time.perf_counter()
    requested_fields = sorted([key for key, value in kwargs.items() if value is not None])
    member_id = coerce_int(member_id)
    if member_id is None:
        logger.warning("crud.update_member invalid member_id=%s fields=%s", member_id, requested_fields)
        return None

    result = await db.execute(
        select(People)
        .where(People.id == member_id)
        .where(People.deleted_at.is_(None))
    )

    person = result.scalar_one_or_none()
    if not person:
        logger.warning("crud.update_member member_id=%s not found", member_id)
        return None

    # Update fields
    changed_fields: List[str] = []
    for key, value in kwargs.items():
        if hasattr(person, key) and value is not None:
            setattr(person, key, value)
            changed_fields.append(key)

    person.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(person)
    duration_ms = (time.perf_counter() - started_at) * 1000
    logger.info(
        "crud.update_member member_id=%s fields=%s requested_fields=%s duration_ms=%.2f",
        member_id,
        sorted(changed_fields),
        requested_fields,
        duration_ms,
    )
    return person

async def delete_member_and_related(db: AsyncSession, member_id: int) -> tuple[bool, str]:
    """Soft delete a member while cleaning related records."""

    member_id = coerce_int(member_id)
    if member_id is None:
        return False, "ID de socio invalido"

    result = await db.execute(
        select(People)
        .options(selectinload(People.roles).selectinload(PersonRole.role))
        .where(People.id == member_id)
    )
    person = result.scalar_one_or_none()

    if not person:
        return False, "Miembro no encontrado"

    if person.deleted_at:
        return False, "El socio ya fue eliminado previamente"

    is_member = any(role.role and role.role.code == 'member' for role in person.roles)
    if not is_member:
        return False, "La persona seleccionada no es un socio"

    standing_bookings_subquery = select(StandingBooking.id).where(StandingBooking.person_id == member_id).scalar_subquery()
    lead_ids_subquery = select(Lead.id).where(Lead.person_id == member_id).scalar_subquery()

    timestamp = datetime.now(timezone.utc)

    try:
        await db.execute(
            delete(StandingBookingException)
            .where(StandingBookingException.standing_booking_id.in_(standing_bookings_subquery))
        )

        await db.execute(
            delete(LeadEvent).where(LeadEvent.lead_id.in_(lead_ids_subquery))
        )
        await db.execute(
            delete(LeadAttribution).where(LeadAttribution.lead_id.in_(lead_ids_subquery))
        )
        await db.execute(delete(Lead).where(Lead.person_id == member_id))
        await db.execute(delete(FormSubmission).where(FormSubmission.person_id == member_id))
        await db.execute(delete(CommunicationOptIn).where(CommunicationOptIn.person_id == member_id))
        await db.execute(delete(WhatsAppThread).where(WhatsAppThread.person_id == member_id))

        await db.execute(delete(Reservation).where(Reservation.person_id == member_id))
        await db.execute(delete(StandingBooking).where(StandingBooking.person_id == member_id))
        await db.execute(delete(Payment).where(Payment.person_id == member_id))
        await db.execute(delete(MembershipSubscription).where(MembershipSubscription.person_id == member_id))
        await db.execute(
            update(ClassTemplate)
            .where(ClassTemplate.instructor_id == member_id)
            .values(instructor_id=None)
        )
        await db.execute(
            update(ClassSession)
            .where(ClassSession.instructor_id == member_id)
            .values(instructor_id=None)
        )
        await db.execute(delete(PersonRole).where(PersonRole.person_id == member_id))
        await db.execute(delete(Account).where(Account.person_id == member_id))
        await db.execute(
            update(People)
            .where(People.id == member_id)
            .values(
                deleted_at=timestamp,
                updated_at=timestamp
            )
        )

        await db.commit()
        return True, "Socio eliminado correctamente"
    except Exception as exc:
        await db.rollback()
        logger.exception("Error deleting member %s", member_id)
        return False, f"Error al eliminar socio: {exc}"
