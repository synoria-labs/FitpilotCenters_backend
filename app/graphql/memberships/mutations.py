
import asyncio
from datetime import datetime, timezone
import json
import logging

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.models.notificationModel import (
    EVENT_NEW_REGISTRATION,
    EVENT_RENEWAL_CONFIRMATION,
)
from app.services.notification_service import dispatch_event_in_background

from app.crud.membershipsCrud import (
    create_membership_plan,
    update_membership_plan,
    set_membership_plan_active,
    create_membership_subscription,
    get_membership_plan_by_id,
    create_member_enrollment,
    create_member_enrollment_with_standing_booking,
    renew_subscription_with_standing_booking,
    SubscriptionData,
    update_payment,
    delete_payment
)
from app.crud.membersCrud import get_member_by_id
from app.crud.permissions import MANAGE_MEMBERSHIP_PLANS
from app.graphql.memberships.types import (
    CreateMembershipPlanInput, UpdateMembershipPlanInput, CreateSubscriptionInput,
    CreateMemberEnrollmentInput, RenewSubscriptionInput,
    MembershipPlanResponse, MembershipPlanMutationResponse, SubscriptionResponse,
    MemberEnrollmentResponse, SubscriptionRenewalResponse,
    MembershipPlan, Subscription, PaymentRecord,
    UpdatePaymentInput, PaymentMutationResponse
)
from app.graphql.members.types import Member
from app.graphql.auth.permissions import IsAuthenticated, require_capability


def _classify_renewal_error(error_text: str) -> tuple[str, str]:
    """Map low-level renewal errors to stable codes and user-facing causes."""
    normalized = (error_text or "").lower()

    availability_markers = (
        "already reserved by another person",
        "no se pudieron crear los standing bookings",
        "no se pudieron materializar las reservas",
        "sin cupo",
        "asientos ocupados",
        "seat",
    )
    if any(marker in normalized for marker in availability_markers):
        return "NO_AVAILABILITY", "Falta de disponibilidad"

    if "debe seleccionar un horario" in normalized:
        return "MISSING_TEMPLATE", "Falta seleccionar horario"

    if "no active subscription found" in normalized:
        return "NO_ACTIVE_SUBSCRIPTION", "No se encontro suscripcion activa"

    return "RENEWAL_FAILED", "Error al renovar"


@strawberry.type
class MembershipMutation:
    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def create_membership_plan(self, info: Info, input: CreateMembershipPlanInput) -> MembershipPlanMutationResponse:
        """Create a new membership plan (requires the manage_membership_plans capability)."""
        db: AsyncSession = info.context.db

        error = await require_capability(info, MANAGE_MEMBERSHIP_PLANS)
        if error:
            return MembershipPlanMutationResponse(success=False, plan=None, message=error)

        try:
            plan = await create_membership_plan(
                db=db,
                name=input.name,
                price=input.price,
                duration_value=input.duration_value,
                duration_unit=input.duration_unit,
                description=input.description,
                class_limit=input.class_limit,
                plan_type=input.plan_type,
                fixed_time_slot=input.fixed_time_slot,
                is_active=input.is_active,
                max_sessions_per_day=input.max_sessions_per_day,
                max_sessions_per_week=input.max_sessions_per_week
            )

            # Get plan data
            plan_data = await get_membership_plan_by_id(db=db, plan_id=plan.id)

            return MembershipPlanMutationResponse(
                success=True,
                plan=MembershipPlan.from_data(plan_data) if plan_data else None,
                message="Plan de membresía creado exitosamente"
            )

        except Exception as e:
            # Rollback in case of error
            await db.rollback()
            return MembershipPlanMutationResponse(
                success=False,
                plan=None,
                message=f"Error al crear plan de membresía: {str(e)}"
            )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def update_membership_plan(self, info: Info, input: UpdateMembershipPlanInput) -> MembershipPlanMutationResponse:
        """Update an existing membership plan (requires the manage_membership_plans capability)."""
        db: AsyncSession = info.context.db

        error = await require_capability(info, MANAGE_MEMBERSHIP_PLANS)
        if error:
            return MembershipPlanMutationResponse(success=False, plan=None, message=error)

        _UNSET = object()

        def _opt(value):
            # Treat None as "not provided" so partial updates don't wipe fields,
            # except for explicitly nullable fields handled below.
            return value if value is not None else _UNSET

        try:
            plan = await update_membership_plan(
                db=db,
                plan_id=input.plan_id,
                name=_opt(input.name),
                price=_opt(input.price),
                duration_value=_opt(input.duration_value),
                duration_unit=_opt(input.duration_unit),
                description=input.description if input.description is not None else _UNSET,
                class_limit=input.class_limit if input.class_limit is not None else _UNSET,
                plan_type=_opt(input.plan_type),
                is_active=_opt(input.is_active),
                max_sessions_per_day=input.max_sessions_per_day if input.max_sessions_per_day is not None else _UNSET,
                max_sessions_per_week=input.max_sessions_per_week if input.max_sessions_per_week is not None else _UNSET,
            )

            if plan is None:
                return MembershipPlanMutationResponse(
                    success=False, plan=None, message="Plan no encontrado"
                )

            plan_data = await get_membership_plan_by_id(db=db, plan_id=plan.id)
            return MembershipPlanMutationResponse(
                success=True,
                plan=MembershipPlan.from_data(plan_data) if plan_data else None,
                message="Plan de membresía actualizado exitosamente"
            )

        except Exception as e:
            await db.rollback()
            return MembershipPlanMutationResponse(
                success=False, plan=None,
                message=f"Error al actualizar plan de membresía: {str(e)}"
            )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def set_membership_plan_active(self, info: Info, plan_id: int, is_active: bool) -> MembershipPlanMutationResponse:
        """Soft-delete (deactivate) or restore a membership plan."""
        db: AsyncSession = info.context.db

        error = await require_capability(info, MANAGE_MEMBERSHIP_PLANS)
        if error:
            return MembershipPlanMutationResponse(success=False, plan=None, message=error)

        try:
            plan = await set_membership_plan_active(db=db, plan_id=plan_id, is_active=is_active)
            if plan is None:
                return MembershipPlanMutationResponse(
                    success=False, plan=None, message="Plan no encontrado"
                )

            plan_data = await get_membership_plan_by_id(db=db, plan_id=plan.id)
            action = "reactivado" if is_active else "desactivado"
            return MembershipPlanMutationResponse(
                success=True,
                plan=MembershipPlan.from_data(plan_data) if plan_data else None,
                message=f"Plan {action} exitosamente"
            )

        except Exception as e:
            await db.rollback()
            return MembershipPlanMutationResponse(
                success=False, plan=None,
                message=f"Error al cambiar el estado del plan: {str(e)}"
            )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def create_subscription(self, info: Info, input: CreateSubscriptionInput) -> SubscriptionResponse:
        """Create a new membership subscription"""
        db: AsyncSession = info.context.db

        try:
            created_by = getattr(info.context, 'account_id', None)

            subscription = await create_membership_subscription(
                db=db,
                person_id=input.person_id,
                plan_id=input.plan_id,
                start_at=input.start_at,
                created_by=created_by
            )

            from sqlalchemy import select
            from sqlalchemy.orm import selectinload

            result = await db.execute(
                select(subscription.__class__)
                .options(
                    selectinload(subscription.__class__.person),
                    selectinload(subscription.__class__.plan)
                )
                .where(subscription.__class__.id == subscription.id)
            )
            sub_with_relations = result.scalar_one()

            subscription_data = SubscriptionData(
                id=sub_with_relations.id,
                person_id=sub_with_relations.person_id,
                plan_id=sub_with_relations.plan_id,
                start_at=sub_with_relations.start_at,
                end_at=sub_with_relations.end_at,
                status=sub_with_relations.status,
                plan_name=sub_with_relations.plan.name,
                person_name=sub_with_relations.person.full_name or "Sin nombre",
                remaining_days=(sub_with_relations.end_at - sub_with_relations.start_at).days
            )

            # Ensure transaction is committed
            await db.commit()

            return SubscriptionResponse(
                subscription=Subscription.from_data(subscription_data),
                message="Suscripci\u00f3n creada exitosamente"
            )

        except Exception as e:
            # Rollback in case of error
            await db.rollback()
            return SubscriptionResponse(
                subscription=None,
                message=f"Error al crear suscripci\u00f3n: {str(e)}"
            )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def create_member_enrollment(self, info: Info, input: CreateMemberEnrollmentInput) -> MemberEnrollmentResponse:
        """Create member, subscription and payment; optionally create standing bookings + sessions like renewal."""
        db: AsyncSession = info.context.db

        try:
            created_by = getattr(info.context, 'account_id', None)

            if getattr(input, 'template_id', None):
                person, subscription, payment, plan, standing_booking_id, materialization_stats = (
                    await create_member_enrollment_with_standing_booking(
                        db=db,
                        full_name=input.full_name,
                        email=input.email,
                        phone_number=input.phone_number,  # WhatsApp number stored here
                        plan_id=input.plan_id,
                        start_at=input.start_at,
                        payment_method=input.payment_method,
                        payment_amount=input.payment_amount,
                        payment_status=input.payment_status,
                        payment_comment=input.payment_comment,
                        payment_provider=input.payment_provider,
                        provider_payment_id=input.provider_payment_id,
                        external_reference=input.external_reference,
                        recorded_by=created_by,
                        template_id=input.template_id,
                        seat_id=getattr(input, 'seat_id', None),
                        auto_materialize=True,
                    )
                )
            else:
                person, subscription, payment, plan = await create_member_enrollment(
                    db=db,
                    full_name=input.full_name,
                    email=input.email,
                    phone_number=input.phone_number,  # WhatsApp number stored here
                    plan_id=input.plan_id,
                    start_at=input.start_at,
                    payment_method=input.payment_method,
                    payment_amount=input.payment_amount,
                    payment_status=input.payment_status,
                    payment_comment=input.payment_comment,
                    payment_provider=input.payment_provider,
                    provider_payment_id=input.provider_payment_id,
                    external_reference=input.external_reference,
                    recorded_by=created_by
                )

            member_data = await get_member_by_id(db=db, member_id=person.id)

            now = datetime.now(timezone.utc)
            remaining_days = (subscription.end_at - now).days if subscription.end_at > now else 0

            subscription_data = SubscriptionData(
                id=subscription.id,
                person_id=subscription.person_id,
                plan_id=subscription.plan_id,
                start_at=subscription.start_at,
                end_at=subscription.end_at,
                status=subscription.status,
                plan_name=plan.name,
                person_name=person.full_name or "Sin nombre",
                remaining_days=remaining_days
            )

            # Commit the transaction after all operations
            await db.commit()

            # Fire-and-forget welcome notification (own session, never blocks/rolls back the alta).
            try:
                asyncio.create_task(
                    dispatch_event_in_background(
                        EVENT_NEW_REGISTRATION,
                        person_id=person.id,
                        subscription_id=subscription.id,
                    )
                )
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).warning(
                    "Could not schedule welcome notification", exc_info=True
                )

            # Build message and top-level fields like renewal
            try:
                import json
                message_payload = {"text": "Suscripci\u00f3n creada exitosamente"}
                if 'materialization_stats' in locals() and isinstance(materialization_stats, dict):
                    message_payload["standingBookingIds"] = materialization_stats.get("standing_booking_ids", [])
                    message_payload["materializationStats"] = materialization_stats
                if 'standing_booking_id' in locals():
                    message_payload["standingBookingId"] = standing_booking_id
                message_text = json.dumps(message_payload)
            except Exception:
                message_text = "Suscripci\u00f3n creada exitosamente"

            return MemberEnrollmentResponse(
                member=Member.from_data(member_data) if member_data else None,
                subscription=Subscription.from_data(subscription_data),
                payment=PaymentRecord.from_model(payment),
                message=message_text,
                standingBookingId=standing_booking_id if 'standing_booking_id' in locals() else None,
                materializationStats=(json.dumps(materialization_stats) if 'materialization_stats' in locals() and materialization_stats is not None else None),
            )

        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Error creating member enrollment: {str(e)}", exc_info=True)

            # Roll back the transaction explicitly
            await db.rollback()

            return MemberEnrollmentResponse(
                member=None,
                subscription=None,
                payment=None,
                message=f"Error al crear suscripci\u00f3n: {str(e)}"
            )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def renew_subscription(self, info: Info, input: RenewSubscriptionInput) -> SubscriptionRenewalResponse:
        """Renew a member's subscription."""
        db: AsyncSession = info.context.db

        try:
            created_by = getattr(info.context, 'account_id', None)

            # Log the input for debugging
            import logging
            logger = logging.getLogger(__name__)
            logger.info(f"Renewing subscription for member {input.member_id}, plan {input.plan_id}")

            subscription, payment, plan, standing_booking_id, materialization_stats = await renew_subscription_with_standing_booking(
                db=db,
                member_id=input.member_id,
                plan_id=input.plan_id,
                template_id=input.template_id,
                seat_id=input.seat_id,
                start_at=input.start_at,
                payment_method=input.payment_method,
                payment_amount=input.payment_amount,
                payment_status=input.payment_status,
                payment_comment=input.payment_comment,
                payment_provider=input.payment_provider,
                provider_payment_id=input.provider_payment_id,
                external_reference=input.external_reference,
                recorded_by=created_by
            )

            # Log results based on what was created
            if standing_booking_id:
                logger.info(f"Successfully created subscription {subscription.id}, payment {payment.id}, and standing booking {standing_booking_id}")
                logger.info(f"Materialization stats: {materialization_stats}")
            else:
                logger.info(f"Successfully created subscription {subscription.id} and payment {payment.id} (no standing booking required)")

            # Ensure the transaction is committed before returning
            await db.commit()

            # Fire-and-forget renewal confirmation (own session, never blocks/rolls back the renovación).
            try:
                asyncio.create_task(
                    dispatch_event_in_background(
                        EVENT_RENEWAL_CONFIRMATION,
                        person_id=subscription.person_id,
                        subscription_id=subscription.id,
                    )
                )
            except Exception:  # noqa: BLE001
                logger.warning("Could not schedule renewal confirmation", exc_info=True)

            # Calculate remaining days for response
            now = datetime.now(timezone.utc)
            remaining_days = (subscription.end_at - now).days if subscription.end_at > now else 0

            subscription_data = SubscriptionData(
                id=subscription.id,
                person_id=subscription.person_id,
                plan_id=subscription.plan_id,
                start_at=subscription.start_at,
                end_at=subscription.end_at,
                status=subscription.status,
                plan_name=plan.name,
                person_name="", # Will be populated from member data if needed
                remaining_days=remaining_days
            )

            # Prepare response message with standing booking info embedded as JSON
            response_data = {
                "success": True,
                "text": "Suscripción renovada exitosamente",
                "standingBookingId": standing_booking_id,  # Backward compatibility: first ID
                "standingBookingIds": materialization_stats.get("standing_booking_ids", []),  # NEW: all IDs
                "materializationStats": materialization_stats
            }
            message_with_data = json.dumps(response_data)

            return SubscriptionRenewalResponse(
                success=True,
                subscription=Subscription.from_data(subscription_data),
                payment=PaymentRecord.from_model(payment),
                message=message_with_data,
                standingBookingId=standing_booking_id,
                materializationStats=(
                    json.dumps(materialization_stats)
                    if materialization_stats is not None
                    else None
                ),
            )

        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Error renewing subscription: {str(e)}", exc_info=True)

            await db.rollback()

            error_text = str(e)
            error_code, cause = _classify_renewal_error(error_text)
            error_payload = {
                "success": False,
                "text": f"{cause}.",
                "errorCode": error_code,
                "cause": cause,
                "details": error_text,
            }

            return SubscriptionRenewalResponse(
                success=False,
                subscription=None,
                payment=None,
                message=json.dumps(error_payload),
                standingBookingId=None,
                materializationStats=None,
            )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def update_payment(self, info: Info, input: UpdatePaymentInput) -> PaymentMutationResponse:
        """Update an existing payment."""
        db: AsyncSession = info.context.db
        
        try:
            from sqlalchemy.orm import selectinload
            payment = await update_payment(
                db=db,
                payment_id=input.payment_id,
                amount=input.amount,
                method=input.method,
                status=input.status,
                comment=input.comment,
                commit=True
            )
            
            if not payment:
                return PaymentMutationResponse(
                    success=False,
                    payment=None,
                    message="Pago no encontrado."
                )
                
            # Eager load the person
            from app.models import Payment as PaymentModel
            from sqlalchemy import select
            stmt = select(PaymentModel).options(selectinload(PaymentModel.person)).where(PaymentModel.id == payment.id)
            result = await db.execute(stmt)
            payment_with_person = result.scalar_one()

            return PaymentMutationResponse(
                success=True,
                payment=PaymentRecord.from_model(payment_with_person),
                message="Pago actualizado exitosamente."
            )
        except Exception as e:
            await db.rollback()
            return PaymentMutationResponse(
                success=False,
                payment=None,
                message=f"Error al actualizar pago: {str(e)}"
            )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def delete_payment(self, info: Info, payment_id: int) -> PaymentMutationResponse:
        """Delete an existing payment."""
        db: AsyncSession = info.context.db
        
        try:
            success = await delete_payment(db=db, payment_id=payment_id, commit=True)
            if success:
                return PaymentMutationResponse(
                    success=True,
                    payment=None,
                    message="Pago eliminado exitosamente."
                )
            else:
                return PaymentMutationResponse(
                    success=False,
                    payment=None,
                    message="Pago no encontrado."
                )
        except Exception as e:
            await db.rollback()
            return PaymentMutationResponse(
                success=False,
                payment=None,
                message=f"Error al eliminar pago: {str(e)}"
            )
