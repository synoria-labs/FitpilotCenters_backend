"""
Modern GraphQL mutations for reservations.
"""
import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
from strawberry.types import Info

from app.crud.reservationsCrud import (
    create_reservation,
    cancel_reservation,
    check_in_reservation,
    checkout_reservation,
    get_reservation_by_id
)
from app.graphql.reservations.types import (
    CreateReservationInput,
    ReservationResponse,
    CheckInResponse,
    Reservation
)
from app.graphql.auth.permissions import IsAuthenticated
from app.graphql.context import Context


@strawberry.type
class ReservationMutation:
    """Reservation mutations"""

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def create_reservation(
        self,
        info: Info[Context],
        input: CreateReservationInput
    ) -> ReservationResponse:
        """Create a new reservation"""
        db: AsyncSession = info.context.db

        try:
            # Create the reservation
            reservation_model = await create_reservation(
                db=db,
                session_id=input.session_id,
                person_id=input.person_id,
                seat_id=input.seat_id,
                source=input.source
            )

            # Get the full reservation data
            reservation_data = await get_reservation_by_id(db, reservation_model.id)

            if not reservation_data:
                await db.rollback()
                return ReservationResponse(
                    success=False,
                    reservation=None,
                    message="Error retrieving created reservation"
                )

            # Ensure transaction is committed
            await db.commit()

            return ReservationResponse(
                success=True,
                reservation=Reservation.from_data(reservation_data),
                message="Reservation created successfully"
            )

        except ValueError as e:
            await db.rollback()
            return ReservationResponse(
                success=False,
                reservation=None,
                message=str(e)
            )
        except Exception as e:
            await db.rollback()
            return ReservationResponse(
                success=False,
                reservation=None,
                message=f"Unexpected error: {str(e)}"
            )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def cancel_reservation(
        self,
        info: Info[Context],
        reservation_id: int
    ) -> ReservationResponse:
        """Cancel a reservation"""
        db: AsyncSession = info.context.db

        try:
            # Get the reservation before canceling
            reservation_data = await get_reservation_by_id(db, reservation_id)
            if not reservation_data:
                return ReservationResponse(
                    success=False,
                    reservation=None,
                    message="Reservation not found"
                )

            # Cancel the reservation
            await cancel_reservation(db, reservation_id)

            # Get updated data
            updated_reservation_data = await get_reservation_by_id(db, reservation_id)

            # Ensure transaction is committed
            await db.commit()

            return ReservationResponse(
                success=True,
                reservation=Reservation.from_data(updated_reservation_data) if updated_reservation_data else None,
                message="Reservation canceled successfully"
            )

        except ValueError as e:
            await db.rollback()
            return ReservationResponse(
                success=False,
                reservation=None,
                message=str(e)
            )
        except Exception as e:
            await db.rollback()
            return ReservationResponse(
                success=False,
                reservation=None,
                message=f"Unexpected error: {str(e)}"
            )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def check_in_reservation(
        self,
        info: Info[Context],
        reservation_id: int
    ) -> CheckInResponse:
        """Check in a member for their reservation"""
        db: AsyncSession = info.context.db

        try:
            # Check in the reservation
            checkin_time = await check_in_reservation(db, reservation_id)

            # Ensure transaction is committed
            await db.commit()

            return CheckInResponse(
                success=True,
                checkin_time=checkin_time,
                message="Member checked in successfully"
            )

        except ValueError as e:
            await db.rollback()
            return CheckInResponse(
                success=False,
                checkin_time=None,
                message=str(e)
            )
        except Exception as e:
            await db.rollback()
            return CheckInResponse(
                success=False,
                checkin_time=None,
                message=f"Unexpected error: {str(e)}"
            )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def checkout_reservation(
        self,
        info: Info[Context],
        reservation_id: int
    ) -> CheckInResponse:
        """Check out a member from their reservation"""
        db: AsyncSession = info.context.db

        try:
            # Check out the reservation
            checkout_time = await checkout_reservation(db, reservation_id)

            # Ensure transaction is committed
            await db.commit()

            return CheckInResponse(
                success=True,
                checkin_time=checkout_time,  # Reusing the same response type
                message="Member checked out successfully"
            )

        except ValueError as e:
            await db.rollback()
            return CheckInResponse(
                success=False,
                checkin_time=None,
                message=str(e)
            )
        except Exception as e:
            await db.rollback()
            return CheckInResponse(
                success=False,
                checkin_time=None,
                message=f"Unexpected error: {str(e)}"
            )
