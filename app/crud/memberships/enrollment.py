from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Tuple

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MembershipPlan, MembershipSubscription, Payment, People
from app.models.classModel import StandingBooking

from .payments import create_payment
from .standing_bookings import (
    _assert_materialization_success,
    _handle_fixed_timeslot_effects,
    _init_materialization_stats,
)
from .subscriptions import create_membership_subscription, get_member_active_subscription
from .utils import _normalize_to_utc, _resolve_payment_amount


async def create_member_enrollment(
    db: AsyncSession,
    *,
    full_name: str,
    email: Optional[str] = None,
    phone_number: Optional[str] = None,
    plan_id: int,
    start_at: Optional[datetime] = None,
    payment_method: str = "cash",
    payment_amount: Optional[Decimal | float] = None,
    payment_status: str = "COMPLETED",
    payment_comment: Optional[str] = None,
    payment_provider: Optional[str] = None,
    provider_payment_id: Optional[str] = None,
    external_reference: Optional[str] = None,
    recorded_by: Optional[int] = None,
) -> Tuple[People, MembershipSubscription, Payment, MembershipPlan]:
    """Create member, subscription and payment in a single transaction."""
    plan_result = await db.execute(select(MembershipPlan).where(MembershipPlan.id == plan_id))
    plan = plan_result.scalar_one()

    normalized_start = _normalize_to_utc(start_at or datetime.now().astimezone())

    from app.crud.membersCrud import create_member as create_member_record

    # Don't start a new transaction - use the existing one from GraphQL
    # Step 1: Create member first (required for payment and subscription)
    person = await create_member_record(
        db=db,
        full_name=full_name,
        email=email,
        phone_number=phone_number,  # WhatsApp stored in phone_number
        commit=False,
    )

    # Step 2: Process payment BEFORE creating subscription
    amount_value = _resolve_payment_amount(plan, payment_amount)

    # Create payment first with subscription_id as None (will be updated later)
    payment = await create_payment(
        db=db,
        person_id=person.id,
        subscription_id=None,  # Will be updated after subscription creation
        amount=amount_value,
        method=payment_method,
        status=payment_status,
        comment=payment_comment,
        provider=payment_provider,
        provider_payment_id=provider_payment_id,
        external_reference=external_reference,
        recorded_by=recorded_by,
        commit=False,
    )

    # Step 3: Create subscription after payment is processed
    subscription = await create_membership_subscription(
        db=db,
        person_id=person.id,
        plan_id=plan_id,
        start_at=normalized_start,
        created_by=recorded_by,
        status="active",
        plan=plan,
        commit=False,
    )

    # Step 4: Update payment with subscription_id
    payment.subscription_id = subscription.id

    # Flush to ensure IDs are available, but don't commit yet
    await db.flush()

    # Refresh objects to get the latest state
    await db.refresh(person)
    await db.refresh(subscription)
    await db.refresh(payment)

    subscription.plan = plan

    return person, subscription, payment, plan


async def renew_subscription_with_standing_booking(
    db: AsyncSession,
    member_id: int,
    plan_id: int,
    template_id: Optional[int] = None,
    seat_id: Optional[int] = None,
    start_at: Optional[datetime] = None,
    payment_method: str = "cash",
    payment_amount: Optional[Decimal | float] = None,
    payment_status: str = "COMPLETED",
    payment_comment: Optional[str] = None,
    payment_provider: Optional[str] = None,
    provider_payment_id: Optional[str] = None,
    external_reference: Optional[str] = None,
    recorded_by: Optional[int] = None,
    auto_materialize: bool = True,
) -> tuple[MembershipSubscription, Payment, MembershipPlan, Optional[int], dict]:
    """
    Renew a subscription and handle standing booking creation for fixed time slot plans.

    Returns:
        - MembershipSubscription: The renewed subscription
        - Payment: The payment record
        - MembershipPlan: The plan details
        - Optional[int]: Standing booking ID if created
        - dict: Materialization stats
    """
    import logging

    logger = logging.getLogger(__name__)

    logger.info("Starting subscription renewal with standing booking for member %s, plan %s", member_id, plan_id)

    try:
        # Get the plan
        plan_result = await db.execute(select(MembershipPlan).where(MembershipPlan.id == plan_id))
        plan = plan_result.scalar_one()
        logger.info("Found plan: %s ($%s)", plan.name, plan.price)
    except Exception as exc:
        logger.error("Failed to get plan %s: %s", plan_id, exc)
        raise ValueError(f"Plan {plan_id} not found") from exc

    # Get current active subscription to calculate renewal start date
    current_subscription = await get_member_active_subscription(db, member_id)

    needs_standing_booking = plan.fixed_time_slot or template_id is not None

    # If template_id/seat_id not provided, try to preserve them from the previous subscription's standing bookings.
    if not template_id and current_subscription:
        if plan.fixed_time_slot:
            logger.info(
                "Plan requires fixed_time_slot but no template_id provided. Looking for previous standing booking..."
            )
        else:
            logger.info("No template_id provided. Checking for previous standing booking to preserve schedule...")

        # Get the most recent standing booking from the current subscription
        previous_sb_result = await db.execute(
            select(StandingBooking)
            .where(StandingBooking.subscription_id == current_subscription.id)
            .order_by(StandingBooking.created_at.desc())
            .limit(1)
        )
        previous_sb = previous_sb_result.scalars().first()

        if previous_sb:
            template_id = previous_sb.template_id
            seat_id = previous_sb.seat_id if not seat_id else seat_id
            needs_standing_booking = True
            logger.info(
                "Preserving template_id=%s, seat_id=%s from previous standing booking %s",
                template_id,
                seat_id,
                previous_sb.id,
            )
        else:
            logger.warning("No previous standing booking found for subscription %s", current_subscription.id)

    if start_at is None:
        if current_subscription and current_subscription.end_at:
            # Start renewal from current subscription end date
            normalized_start = _normalize_to_utc(current_subscription.end_at)
        else:
            # No active subscription, start from now
            normalized_start = _normalize_to_utc(datetime.now().astimezone())
    else:
        normalized_start = _normalize_to_utc(start_at)

    # Expire all existing active subscriptions before creating the new one
    # This prevents having multiple active subscriptions for the same member
    logger.info("Expiring existing active subscriptions for member %s", member_id)

    # First, get IDs of subscriptions to expire
    expired_subs_result = await db.execute(
        select(MembershipSubscription.id).where(
            and_(
                MembershipSubscription.person_id == member_id,
                MembershipSubscription.status == "active",
            )
        )
    )
    expired_subscription_ids = [row[0] for row in expired_subs_result.fetchall()]

    # Expire the subscriptions
    await db.execute(
        update(MembershipSubscription)
        .where(
            and_(
                MembershipSubscription.person_id == member_id,
                MembershipSubscription.status == "active",
            )
        )
        .values(status="expired", updated_at=datetime.now(timezone.utc))
    )

    # Also expire standing bookings associated with the expired subscriptions
    if expired_subscription_ids:
        logger.info("Expiring standing bookings for %s expired subscriptions", len(expired_subscription_ids))
        await db.execute(
            update(StandingBooking)
            .where(
                and_(
                    StandingBooking.subscription_id.in_(expired_subscription_ids),
                    StandingBooking.status == "active",
                )
            )
            .values(status="canceled")  # Note: "canceled" not "cancelled"
        )

    await db.flush()  # Ensure the updates are applied before creating the new subscription

    # Create new subscription
    subscription = await create_membership_subscription(
        db=db,
        person_id=member_id,
        plan_id=plan_id,
        start_at=normalized_start,
        created_by=recorded_by,
        commit=False,
    )

    # Calculate payment amount
    amount_value = _resolve_payment_amount(plan, payment_amount)

    # Create payment
    payment = await create_payment(
        db=db,
        person_id=member_id,
        subscription_id=subscription.id,
        amount=amount_value,
        method=payment_method,
        status=payment_status,
        comment=payment_comment,
        provider=payment_provider,
        provider_payment_id=provider_payment_id,
        external_reference=external_reference,
        recorded_by=recorded_by,
        commit=False,
    )

    standing_booking_id = None
    materialization_stats = _init_materialization_stats()

    # Handle standing booking for fixed time slot plans or explicit template selection
    if needs_standing_booking:
        if not template_id:
            raise ValueError("Debe seleccionar un horario para renovar este plan.")
        if not auto_materialize:
            raise ValueError("La renovacion requiere materializar reservas automaticamente.")
        logger.info("Handling fixed time-slot effects for template %s", template_id)
        standing_booking_id, materialization_stats = await _handle_fixed_timeslot_effects(
            db=db,
            subscription=subscription,
            plan=plan,
            template_id=template_id,
            seat_id=seat_id,
            auto_materialize=auto_materialize,
            commit_sessions=False,  # defer to the single outer commit -> atomic enrollment
        )
        _assert_materialization_success(materialization_stats)

    # Commit transaction
    logger.info("Committing renewal transaction for subscription %s", subscription.id)
    await db.commit()

    # Refresh objects
    await db.refresh(subscription)
    await db.refresh(payment)
    await db.refresh(plan)

    return subscription, payment, plan, standing_booking_id, materialization_stats


async def create_member_enrollment_with_standing_booking(
    db: AsyncSession,
    full_name: str,
    plan_id: int,
    template_id: Optional[int] = None,
    seat_id: Optional[int] = None,
    email: Optional[str] = None,
    phone_number: Optional[str] = None,
    start_at: Optional[datetime] = None,
    payment_method: str = "cash",
    payment_amount: Optional[Decimal | float] = None,
    payment_status: str = "COMPLETED",
    payment_comment: Optional[str] = None,
    payment_provider: Optional[str] = None,
    provider_payment_id: Optional[str] = None,
    external_reference: Optional[str] = None,
    recorded_by: Optional[int] = None,
    auto_materialize: bool = True,
) -> tuple[People, MembershipSubscription, Payment, MembershipPlan, Optional[int], dict]:
    """
    Create member enrollment with automatic standing booking for fixed time slot plans.

    Returns:
        - People: The created member
        - MembershipSubscription: The subscription
        - Payment: The payment record
        - MembershipPlan: The plan details
        - Optional[int]: Standing booking ID if created
        - dict: Materialization stats
    """
    # Use the existing enrollment function
    person, subscription, payment, plan = await create_member_enrollment(
        db=db,
        full_name=full_name,
        email=email,
        phone_number=phone_number,  # WhatsApp number stored in phone_number
        plan_id=plan_id,
        start_at=start_at,
        payment_method=payment_method,
        payment_amount=payment_amount,
        payment_status=payment_status,
        payment_comment=payment_comment,
        payment_provider=payment_provider,
        provider_payment_id=provider_payment_id,
        external_reference=external_reference,
        recorded_by=recorded_by,
    )

    standing_booking_id = None
    materialization_stats = _init_materialization_stats()

    # Handle fixed time-slot effects via shared helper
    if template_id:
        if not auto_materialize:
            raise ValueError("El registro requiere materializar reservas automaticamente.")
        standing_booking_id, materialization_stats = await _handle_fixed_timeslot_effects(
            db=db,
            subscription=subscription,
            plan=plan,
            template_id=template_id,
            seat_id=seat_id,
            auto_materialize=auto_materialize,
            commit_sessions=False,  # defer to the single outer commit -> atomic enrollment
        )
        _assert_materialization_success(materialization_stats)
    elif plan.fixed_time_slot:
        raise ValueError("Debe seleccionar un horario para registrar este plan.")

    # Final commit
    await db.commit()

    return person, subscription, payment, plan, standing_booking_id, materialization_stats
