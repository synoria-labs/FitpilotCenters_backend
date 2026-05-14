from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import MembershipPlan, MembershipSubscription, People

from .data import SubscriptionData, _subscription_to_data
from .utils import _calculate_subscription_end, _normalize_to_utc


def _subscription_query_with_relations():
    return select(MembershipSubscription).options(
        selectinload(MembershipSubscription.person),
        selectinload(MembershipSubscription.plan),
    )


async def create_membership_subscription(
    db: AsyncSession,
    person_id: int,
    plan_id: int,
    start_at: Optional[datetime] = None,
    created_by: Optional[int] = None,
    status: str = "active",
    plan: Optional[MembershipPlan] = None,
    commit: bool = True,
) -> MembershipSubscription:
    """Create a new membership subscription."""
    normalized_start = _normalize_to_utc(start_at or datetime.now().astimezone())

    if plan is None:
        plan_result = await db.execute(select(MembershipPlan).where(MembershipPlan.id == plan_id))
        plan = plan_result.scalar_one()

    end_at = _calculate_subscription_end(plan, normalized_start)

    subscription = MembershipSubscription(
        person_id=person_id,
        plan_id=plan_id,
        start_at=normalized_start,
        end_at=end_at,
        status=status,
        created_by=created_by,
    )

    db.add(subscription)
    await db.flush()

    # Attach plan reference for downstream logic without extra query
    if plan is not None:
        subscription.plan = plan

    if commit:
        await db.commit()
        await db.refresh(subscription)

    return subscription


async def get_member_active_subscription(
    db: AsyncSession,
    member_id: int,
) -> Optional[MembershipSubscription]:
    """
    Get the active subscription for a member.

    Note: Uses first() instead of scalar_one_or_none() as a defensive measure.
    While there should only be one active subscription per member (enforced at
    the business logic level), this prevents crashes if duplicates exist.
    Returns the most recent subscription (ordered by end_at desc).
    """
    result = await db.execute(
        select(MembershipSubscription)
        .options(selectinload(MembershipSubscription.plan))
        .where(
            and_(
                MembershipSubscription.person_id == member_id,
                MembershipSubscription.status == "active",
            )
        )
        .order_by(MembershipSubscription.end_at.desc())
    )
    return result.scalars().first()


async def get_active_subscriptions(db: AsyncSession, limit: int = 100) -> List[SubscriptionData]:
    """Get list of active subscriptions."""
    now = datetime.now().astimezone()

    query = _subscription_query_with_relations().where(
        and_(
            MembershipSubscription.status == "active",
            MembershipSubscription.end_at > now,
        )
    )
    query = query.order_by(MembershipSubscription.end_at.asc()).limit(limit)

    result = await db.execute(query)
    subscriptions = result.scalars().all()

    return [_subscription_to_data(sub, now) for sub in subscriptions]


async def get_expiring_subscriptions(db: AsyncSession, days_ahead: int = 7) -> List[SubscriptionData]:
    """Get subscriptions that will expire in the next N days."""
    now = datetime.now().astimezone()
    future_date = now + timedelta(days=days_ahead)

    query = _subscription_query_with_relations().where(
        and_(
            MembershipSubscription.status == "active",
            MembershipSubscription.end_at.between(now, future_date),
        )
    )
    query = query.order_by(MembershipSubscription.end_at.asc())

    result = await db.execute(query)
    subscriptions = result.scalars().all()

    return [_subscription_to_data(sub, now) for sub in subscriptions]


async def get_membership_subscriptions(
    db: AsyncSession,
    limit: int = 100,
    offset: int = 0,
    status: Optional[str] = None,
    search: Optional[str] = None,
) -> List[SubscriptionData]:
    """Get membership subscriptions with optional filters."""
    now = datetime.now().astimezone()

    # Base query
    query = _subscription_query_with_relations()

    # Apply filters
    conditions = []

    if status:
        conditions.append(MembershipSubscription.status == status)

    if search:
        # Search in person name, phone, or email
        search_term = f"%{search.lower()}%"
        conditions.append(
            MembershipSubscription.person.has(
                or_(
                    People.full_name.ilike(search_term),
                    People.phone_number.ilike(search_term),
                    People.email.ilike(search_term),
                )
            )
        )

    if conditions:
        query = query.where(and_(*conditions))

    # Order and pagination
    query = query.order_by(MembershipSubscription.created_at.desc()).offset(offset).limit(limit)

    result = await db.execute(query)
    subscriptions = result.scalars().all()

    return [_subscription_to_data(sub, now) for sub in subscriptions]
