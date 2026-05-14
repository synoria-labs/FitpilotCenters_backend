from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.models import MembershipPlan, MembershipSubscription


@dataclass
class MembershipPlanData:
    id: int
    name: str
    description: Optional[str]
    price: float
    duration_value: int
    duration_unit: str
    class_limit: Optional[int]
    fixed_time_slot: bool
    max_sessions_per_day: Optional[int]
    max_sessions_per_week: Optional[int]
    created_at: datetime


@dataclass
class SubscriptionData:
    id: int
    person_id: int
    plan_id: int
    start_at: datetime
    end_at: datetime
    status: str
    plan_name: str
    person_name: str
    remaining_days: Optional[int]


def _plan_to_data(plan: MembershipPlan) -> MembershipPlanData:
    """Map MembershipPlan model to MembershipPlanData DTO."""
    return MembershipPlanData(
        id=plan.id,
        name=plan.name,
        description=plan.description,
        price=float(plan.price),
        duration_value=plan.duration_value,
        duration_unit=plan.duration_unit,
        class_limit=plan.class_limit,
        fixed_time_slot=plan.fixed_time_slot,
        max_sessions_per_day=plan.max_sessions_per_day,
        max_sessions_per_week=plan.max_sessions_per_week,
        created_at=plan.created_at,
    )


def _subscription_to_data(
    subscription: MembershipSubscription,
    now: datetime,
) -> SubscriptionData:
    """Map MembershipSubscription model to SubscriptionData DTO."""
    plan_name = getattr(subscription.plan, "name", None) if subscription.plan else None
    person_name = getattr(subscription.person, "full_name", None) if subscription.person else None
    return SubscriptionData(
        id=subscription.id,
        person_id=subscription.person_id,
        plan_id=subscription.plan_id,
        start_at=subscription.start_at,
        end_at=subscription.end_at,
        status=subscription.status,
        plan_name=plan_name or "Sin nombre",
        person_name=person_name or "Sin nombre",
        remaining_days=(subscription.end_at - now).days if subscription.end_at and subscription.end_at > now else 0,
    )
