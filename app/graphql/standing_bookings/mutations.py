"""
GraphQL mutations for Standing Bookings
"""
import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
from strawberry.types import Info

from app.crud.standingBookingsCrud import (
    create_standing_booking,
    update_standing_booking_status,
    create_standing_booking_exception,
    get_standing_booking_by_id,
    materialize_standing_bookings,
    get_materialization_preview,
    preview_reschedule_standing_booking,
    reschedule_standing_booking
)
from app.graphql.standing_bookings.types import (
    CreateStandingBookingInput,
    UpdateStandingBookingInput,
    CreateStandingBookingExceptionInput,
    MaterializeBookingsInput,
    GetMaterializationPreviewInput,
    RescheduleStandingBookingInput,
    StandingBookingResponse,
    MaterializationResponse,
    MaterializationPreviewResponse,
    RescheduleStandingBookingPreviewResponse,
    RescheduleStandingBookingResponse,
    StandingBooking,
    convert_materialization_stats,
    convert_materialization_preview,
    convert_reschedule_items,
    convert_reschedule_counts
)
from app.graphql.auth.permissions import IsAuthenticated
from app.graphql.context import Context


@strawberry.type
class StandingBookingMutation:
    """Standing Booking mutations"""

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def create_standing_booking(
        self,
        info: Info[Context],
        input: CreateStandingBookingInput
    ) -> StandingBookingResponse:
        """Create a new standing booking (reservativo)"""
        db: AsyncSession = info.context.db

        try:
            # Create the standing booking
            standing_booking_model = await create_standing_booking(
                db=db,
                person_id=input.person_id,
                subscription_id=input.subscription_id,
                template_id=input.template_id,
                start_date=input.start_date,
                end_date=input.end_date,
                seat_id=input.seat_id
            )

            # Get the full standing booking data
            standing_booking_data = await get_standing_booking_by_id(db, standing_booking_model.id)

            if not standing_booking_data:
                await db.rollback()
                return StandingBookingResponse(
                    success=False,
                    standing_booking=None,
                    message="Error retrieving created standing booking"
                )

            # Ensure transaction is committed
            await db.commit()

            return StandingBookingResponse(
                success=True,
                standing_booking=StandingBooking.from_data(standing_booking_data),
                message="Standing booking created successfully"
            )

        except ValueError as e:
            await db.rollback()
            return StandingBookingResponse(
                success=False,
                standing_booking=None,
                message=str(e)
            )
        except Exception as e:
            await db.rollback()
            return StandingBookingResponse(
                success=False,
                standing_booking=None,
                message=f"Unexpected error: {str(e)}"
            )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def update_standing_booking(
        self,
        info: Info[Context],
        input: UpdateStandingBookingInput
    ) -> StandingBookingResponse:
        """Update a standing booking (change status, template, etc.)"""
        db: AsyncSession = info.context.db

        try:
            # For now, we only support status updates
            # In the future, this could be extended to handle template/seat changes
            if input.status:
                await update_standing_booking_status(
                    db=db,
                    standing_booking_id=input.standing_booking_id,
                    new_status=input.status
                )

            # Get updated data
            standing_booking_data = await get_standing_booking_by_id(db, input.standing_booking_id)

            if not standing_booking_data:
                await db.rollback()
                return StandingBookingResponse(
                    success=False,
                    standing_booking=None,
                    message="Standing booking not found"
                )

            # Ensure transaction is committed
            await db.commit()

            return StandingBookingResponse(
                success=True,
                standing_booking=StandingBooking.from_data(standing_booking_data),
                message="Standing booking updated successfully"
            )

        except ValueError as e:
            await db.rollback()
            return StandingBookingResponse(
                success=False,
                standing_booking=None,
                message=str(e)
            )
        except Exception as e:
            await db.rollback()
            return StandingBookingResponse(
                success=False,
                standing_booking=None,
                message=f"Unexpected error: {str(e)}"
            )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def cancel_standing_booking(
        self,
        info: Info[Context],
        standing_booking_id: int
    ) -> StandingBookingResponse:
        """Cancel a standing booking"""
        db: AsyncSession = info.context.db

        try:
            # Update status to canceled
            await update_standing_booking_status(
                db=db,
                standing_booking_id=standing_booking_id,
                new_status='canceled'
            )

            # Get updated data
            standing_booking_data = await get_standing_booking_by_id(db, standing_booking_id)

            if not standing_booking_data:
                await db.rollback()
                return StandingBookingResponse(
                    success=False,
                    standing_booking=None,
                    message="Standing booking not found"
                )

            # Ensure transaction is committed
            await db.commit()

            return StandingBookingResponse(
                success=True,
                standing_booking=StandingBooking.from_data(standing_booking_data),
                message="Standing booking canceled successfully"
            )

        except ValueError as e:
            await db.rollback()
            return StandingBookingResponse(
                success=False,
                standing_booking=None,
                message=str(e)
            )
        except Exception as e:
            await db.rollback()
            return StandingBookingResponse(
                success=False,
                standing_booking=None,
                message=f"Unexpected error: {str(e)}"
            )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def pause_standing_booking(
        self,
        info: Info[Context],
        standing_booking_id: int
    ) -> StandingBookingResponse:
        """Pause a standing booking"""
        db: AsyncSession = info.context.db

        try:
            # Update status to paused
            await update_standing_booking_status(
                db=db,
                standing_booking_id=standing_booking_id,
                new_status='paused'
            )

            # Get updated data
            standing_booking_data = await get_standing_booking_by_id(db, standing_booking_id)

            if not standing_booking_data:
                await db.rollback()
                return StandingBookingResponse(
                    success=False,
                    standing_booking=None,
                    message="Standing booking not found"
                )

            # Ensure transaction is committed
            await db.commit()

            return StandingBookingResponse(
                success=True,
                standing_booking=StandingBooking.from_data(standing_booking_data),
                message="Standing booking paused successfully"
            )

        except ValueError as e:
            await db.rollback()
            return StandingBookingResponse(
                success=False,
                standing_booking=None,
                message=str(e)
            )
        except Exception as e:
            await db.rollback()
            return StandingBookingResponse(
                success=False,
                standing_booking=None,
                message=f"Unexpected error: {str(e)}"
            )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def resume_standing_booking(
        self,
        info: Info[Context],
        standing_booking_id: int
    ) -> StandingBookingResponse:
        """Resume a paused standing booking"""
        db: AsyncSession = info.context.db

        try:
            # Update status to active
            await update_standing_booking_status(
                db=db,
                standing_booking_id=standing_booking_id,
                new_status='active'
            )

            # Get updated data
            standing_booking_data = await get_standing_booking_by_id(db, standing_booking_id)

            if not standing_booking_data:
                await db.rollback()
                return StandingBookingResponse(
                    success=False,
                    standing_booking=None,
                    message="Standing booking not found"
                )

            # Ensure transaction is committed
            await db.commit()

            return StandingBookingResponse(
                success=True,
                standing_booking=StandingBooking.from_data(standing_booking_data),
                message="Standing booking resumed successfully"
            )

        except ValueError as e:
            await db.rollback()
            return StandingBookingResponse(
                success=False,
                standing_booking=None,
                message=str(e)
            )
        except Exception as e:
            await db.rollback()
            return StandingBookingResponse(
                success=False,
                standing_booking=None,
                message=f"Unexpected error: {str(e)}"
            )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def create_standing_booking_exception(
        self,
        info: Info[Context],
        input: CreateStandingBookingExceptionInput
    ) -> StandingBookingResponse:
        """Create an exception for a standing booking (skip or reschedule a specific date)"""
        db: AsyncSession = info.context.db

        try:
            # Create the exception
            await create_standing_booking_exception(
                db=db,
                standing_booking_id=input.standing_booking_id,
                session_date=input.session_date,
                action=input.action,
                new_session_id=input.new_session_id,
                new_seat_id=input.new_seat_id,
                notes=input.notes
            )

            # Get the standing booking data
            standing_booking_data = await get_standing_booking_by_id(db, input.standing_booking_id)

            if not standing_booking_data:
                await db.rollback()
                return StandingBookingResponse(
                    success=False,
                    standing_booking=None,
                    message="Standing booking not found"
                )

            # Ensure transaction is committed
            await db.commit()

            return StandingBookingResponse(
                success=True,
                standing_booking=StandingBooking.from_data(standing_booking_data),
                message=f"Exception ({input.action}) created successfully for {input.session_date}"
            )

        except ValueError as e:
            await db.rollback()
            return StandingBookingResponse(
                success=False,
                standing_booking=None,
                message=str(e)
            )
        except Exception as e:
            await db.rollback()
            return StandingBookingResponse(
                success=False,
                standing_booking=None,
                message=f"Unexpected error: {str(e)}"
            )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def materialize_standing_bookings(
        self,
        info: Info[Context],
        input: MaterializeBookingsInput
    ) -> MaterializationResponse:
        """
        Materialize standing bookings into actual reservations.
        This can be triggered manually when needed.
        """
        db: AsyncSession = info.context.db

        try:
            # Run the materialization algorithm
            stats = await materialize_standing_bookings(
                db=db,
                window_weeks=input.window_weeks,
                start_date=input.start_date
            )

            # Ensure transaction is committed
            await db.commit()

            return MaterializationResponse(
                success=True,
                stats=convert_materialization_stats(stats),
                message=f"Materialization completed. Created {stats['created_reservations']} reservations from {stats['processed_bookings']} standing bookings."
            )

        except Exception as e:
            await db.rollback()
            return MaterializationResponse(
                success=False,
                stats=None,
                message=f"Materialization failed: {str(e)}"
            )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def get_materialization_preview(
        self,
        info: Info[Context],
        input: GetMaterializationPreviewInput
    ) -> MaterializationPreviewResponse:
        """
        Preview what reservations would be created for a standing booking.
        This is a read-only operation that doesn't create actual reservations.
        """
        db: AsyncSession = info.context.db

        try:
            # Get the preview
            preview_data = await get_materialization_preview(
                db=db,
                standing_booking_id=input.standing_booking_id,
                window_weeks=input.window_weeks
            )

            return MaterializationPreviewResponse(
                preview=convert_materialization_preview(preview_data),
                total_sessions=len(preview_data)
            )

        except Exception as e:
            return MaterializationPreviewResponse(
                preview=[],
                total_sessions=0
            )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def preview_reschedule_standing_booking(
        self,
        info: Info[Context],
        input: RescheduleStandingBookingInput
    ) -> RescheduleStandingBookingPreviewResponse:
        """Preview a batch reschedule for a standing booking."""
        db: AsyncSession = info.context.db

        try:
            result = await preview_reschedule_standing_booking(
                db=db,
                standing_booking_id=input.standing_booking_id,
                start_date=input.start_date,
                end_date=input.end_date,
                target_template_id=input.target_template_id,
                target_seat_id=input.target_seat_id
            )
            return RescheduleStandingBookingPreviewResponse(
                items=convert_reschedule_items(result.get("items") or []),
                counts=convert_reschedule_counts(result.get("counts") or {})
            )
        except Exception:
            return RescheduleStandingBookingPreviewResponse(items=[], counts=[])

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def reschedule_standing_booking(
        self,
        info: Info[Context],
        input: RescheduleStandingBookingInput
    ) -> RescheduleStandingBookingResponse:
        """Reschedule one or more dates for a standing booking."""
        db: AsyncSession = info.context.db

        try:
            result = await reschedule_standing_booking(
                db=db,
                standing_booking_id=input.standing_booking_id,
                start_date=input.start_date,
                end_date=input.end_date,
                target_template_id=input.target_template_id,
                target_seat_id=input.target_seat_id,
                strict=input.strict
            )
            return RescheduleStandingBookingResponse(
                success=bool(result.get("success")),
                items=convert_reschedule_items(result.get("items") or []),
                counts=convert_reschedule_counts(result.get("counts") or {}),
                message=result.get("message") or ""
            )
        except Exception as e:
            return RescheduleStandingBookingResponse(
                success=False,
                items=[],
                counts=[],
                message=str(e)
            )
