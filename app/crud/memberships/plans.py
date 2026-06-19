from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.conversions import coerce_int
from app.models import MembershipPlan

from .data import MembershipPlanData, _plan_to_data

# Valid plan types and the rule that ties plan_type -> fixed_time_slot.
PLAN_TYPES = ("fixed_schedule", "flexible", "credit_pack")


def _derive_fixed_time_slot(plan_type: str) -> bool:
    """fixed_time_slot is True only for the recurring fixed-schedule model."""
    return plan_type == "fixed_schedule"


async def get_membership_plans(
    db: AsyncSession, include_inactive: bool = False
) -> List[MembershipPlanData]:
    """Get membership plans. By default only active plans are returned."""
    query = select(MembershipPlan)
    if not include_inactive:
        query = query.where(MembershipPlan.is_active.is_(True))
    query = query.order_by(MembershipPlan.price.asc())

    result = await db.execute(query)
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
    plan_type: str = "fixed_schedule",
    fixed_time_slot: Optional[bool] = None,
    is_active: bool = True,
    max_sessions_per_day: Optional[int] = None,
    max_sessions_per_week: Optional[int] = None,
) -> MembershipPlan:
    """Create a new membership plan.

    ``plan_type`` is the source of truth for the booking model; ``fixed_time_slot``
    is derived from it (unless explicitly provided for backward compatibility).
    """
    if plan_type not in PLAN_TYPES:
        raise ValueError(f"plan_type inválido: {plan_type}")

    resolved_fixed_slot = (
        fixed_time_slot if fixed_time_slot is not None else _derive_fixed_time_slot(plan_type)
    )

    plan = MembershipPlan(
        name=name,
        description=description,
        price=Decimal(str(price)),
        duration_value=duration_value,
        duration_unit=duration_unit,
        class_limit=class_limit,
        plan_type=plan_type,
        fixed_time_slot=resolved_fixed_slot,
        is_active=is_active,
        max_sessions_per_day=max_sessions_per_day,
        max_sessions_per_week=max_sessions_per_week,
    )

    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    return plan


# Sentinel so callers can distinguish "not provided" from "explicitly None".
_UNSET = object()


async def update_membership_plan(
    db: AsyncSession,
    plan_id: int,
    name=_UNSET,
    price=_UNSET,
    duration_value=_UNSET,
    duration_unit=_UNSET,
    description=_UNSET,
    class_limit=_UNSET,
    plan_type=_UNSET,
    is_active=_UNSET,
    max_sessions_per_day=_UNSET,
    max_sessions_per_week=_UNSET,
) -> Optional[MembershipPlan]:
    """Partial update of a membership plan. Only provided fields are changed."""
    plan_id_value = coerce_int(plan_id)
    if plan_id_value is None:
        return None

    result = await db.execute(select(MembershipPlan).where(MembershipPlan.id == plan_id_value))
    plan = result.scalar_one_or_none()
    if not plan:
        return None

    if name is not _UNSET:
        plan.name = name
    if description is not _UNSET:
        plan.description = description
    if price is not _UNSET and price is not None:
        plan.price = Decimal(str(price))
    if duration_value is not _UNSET and duration_value is not None:
        plan.duration_value = duration_value
    if duration_unit is not _UNSET and duration_unit is not None:
        plan.duration_unit = duration_unit
    if class_limit is not _UNSET:
        plan.class_limit = class_limit
    if plan_type is not _UNSET and plan_type is not None:
        if plan_type not in PLAN_TYPES:
            raise ValueError(f"plan_type inválido: {plan_type}")
        plan.plan_type = plan_type
        # Keep the derived flag consistent with the (possibly new) plan_type.
        plan.fixed_time_slot = _derive_fixed_time_slot(plan_type)
    if is_active is not _UNSET and is_active is not None:
        plan.is_active = bool(is_active)
    if max_sessions_per_day is not _UNSET:
        plan.max_sessions_per_day = max_sessions_per_day
    if max_sessions_per_week is not _UNSET:
        plan.max_sessions_per_week = max_sessions_per_week

    plan.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(plan)
    return plan


async def set_membership_plan_active(
    db: AsyncSession, plan_id: int, is_active: bool
) -> Optional[MembershipPlan]:
    """Soft-delete / restore a plan by toggling its is_active flag."""
    plan_id_value = coerce_int(plan_id)
    if plan_id_value is None:
        return None

    result = await db.execute(select(MembershipPlan).where(MembershipPlan.id == plan_id_value))
    plan = result.scalar_one_or_none()
    if not plan:
        return None

    plan.is_active = bool(is_active)
    plan.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(plan)
    return plan
