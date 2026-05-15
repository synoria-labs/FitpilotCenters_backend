from decimal import Decimal
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.conversions import coerce_int
from app.models import MembershipPlan

from .data import MembershipPlanData, _plan_to_data


async def get_membership_plans(db: AsyncSession) -> List[MembershipPlanData]:
    """Get all available membership plans."""
    result = await db.execute(select(MembershipPlan).order_by(MembershipPlan.price.asc()))
    plans = result.scalars().all()

    return [_plan_to_data(plan) for plan in plans]


async def get_membership_plan_by_id(db: AsyncSession, plan_id: int) -> Optional[MembershipPlanData]:
    """Get membership plan by ID."""
    plan_id_value = coerce_int(plan_id)
    if plan_id_value is None:
        return None

    result = await db.execute(select(MembershipPlan).where(MembershipPlan.id == plan_id_value))
    plan = result.scalar_one_or_none()

    if not plan:
        return None

    return _plan_to_data(plan)


async def create_membership_plan(
    db: AsyncSession,
    name: str,
    price: float,
    duration_value: int,
    duration_unit: str,
    description: Optional[str] = None,
    class_limit: Optional[int] = None,
    fixed_time_slot: bool = False,
    max_sessions_per_day: Optional[int] = None,
    max_sessions_per_week: Optional[int] = None,
) -> MembershipPlan:
    """Create a new membership plan."""
    plan = MembershipPlan(
        name=name,
        description=description,
        price=Decimal(str(price)),
        duration_value=duration_value,
        duration_unit=duration_unit,
        class_limit=class_limit,
        fixed_time_slot=fixed_time_slot,
        max_sessions_per_day=max_sessions_per_day,
        max_sessions_per_week=max_sessions_per_week,
    )

    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    return plan
