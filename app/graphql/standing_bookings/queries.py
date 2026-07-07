"""
GraphQL queries for Standing Bookings
"""
import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from strawberry.types import Info

from app.crud.standingBookingsCrud import (
    get_class_types,
    get_class_templates,
    get_available_seats_for_template,
    get_standing_bookings,
    get_standing_booking_by_id
)
from app.graphql.standing_bookings.types import (
    GetClassTemplatesInput,
    GetAvailableSeatsInput,
    GetStandingBookingsInput,
    ClassTypesResponse,
    ClassTemplatesResponse,
    AvailableSeatsResponse,
    StandingBookingsResponse,
    StandingBookingResponse,
    ClassType,
    ClassTemplate,
    AvailableSeat,
    StandingBooking
)
from app.graphql.auth.permissions import IsAuthenticated
from app.graphql.context import Context
from app.core.logging_config import get_logger

logger = get_logger("graphql.standing_bookings.queries")


@strawberry.type
class StandingBookingQuery:
    """Standing Booking queries"""

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def class_types(self, info: Info[Context]) -> ClassTypesResponse:
        """Get all class types"""
        db: AsyncSession = info.context.db

        try:
            class_types_data = await get_class_types(db)

            return ClassTypesResponse(
                class_types=[ClassType.from_data(ct) for ct in class_types_data],
                total_count=len(class_types_data)
            )

        except Exception:
            logger.exception("class_types query failed")
            return ClassTypesResponse(
                class_types=[],
                total_count=0
            )

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def class_templates(
        self,
        info: Info[Context],
        input: GetClassTemplatesInput
    ) -> ClassTemplatesResponse:
        """Get class templates with optional filtering"""
        db: AsyncSession = info.context.db

        try:
            templates_data = await get_class_templates(
                db=db,
                class_type_id=input.class_type_id,
                venue_id=input.venue_id,
                active_only=input.active_only
            )

            return ClassTemplatesResponse(
                templates=[ClassTemplate.from_data(tmpl) for tmpl in templates_data],
                total_count=len(templates_data)
            )

        except Exception:
            logger.exception("class_templates query failed")
            return ClassTemplatesResponse(
                templates=[],
                total_count=0
            )

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def all_class_templates(self, info: Info[Context]) -> ClassTemplatesResponse:
        """Get all active class templates without filtering"""
        db: AsyncSession = info.context.db

        try:
            templates_data = await get_class_templates(
                db=db,
                active_only=True
            )

            return ClassTemplatesResponse(
                templates=[ClassTemplate.from_data(tmpl) for tmpl in templates_data],
                total_count=len(templates_data)
            )

        except Exception:
            logger.exception("all_class_templates query failed")
            return ClassTemplatesResponse(
                templates=[],
                total_count=0
            )

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def template_available_seats(
        self,
        info: Info[Context],
        input: GetAvailableSeatsInput
    ) -> AvailableSeatsResponse:
        """Get available seats for a template on a specific date"""
        db: AsyncSession = info.context.db

        try:
            seats_data = await get_available_seats_for_template(
                db=db,
                template_id=input.template_id,
                date_to_check=input.date_to_check
            )

            available_seats = [seat for seat in seats_data if seat.is_available]

            return AvailableSeatsResponse(
                seats=[AvailableSeat.from_data(seat) for seat in seats_data],
                available_count=len(available_seats),
                total_count=len(seats_data)
            )

        except Exception:
            logger.exception("template_available_seats query failed")
            return AvailableSeatsResponse(
                seats=[],
                available_count=0,
                total_count=0
            )

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def standing_bookings(
        self,
        info: Info[Context],
        input: GetStandingBookingsInput
    ) -> StandingBookingsResponse:
        """Get standing bookings with optional filtering"""
        db: AsyncSession = info.context.db

        try:
            standing_bookings_data = await get_standing_bookings(
                db=db,
                person_id=input.person_id,
                template_id=input.template_id,
                status=input.status,
                active_only=input.active_only
            )

            return StandingBookingsResponse(
                standing_bookings=[StandingBooking.from_data(sb) for sb in standing_bookings_data],
                total_count=len(standing_bookings_data)
            )

        except Exception:
            logger.exception("standing_bookings query failed")
            return StandingBookingsResponse(
                standing_bookings=[],
                total_count=0
            )

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def standing_booking(
        self,
        info: Info[Context],
        id: int
    ) -> StandingBookingResponse:
        """Get a specific standing booking by ID"""
        db: AsyncSession = info.context.db

        try:
            standing_booking_data = await get_standing_booking_by_id(db, id)

            if not standing_booking_data:
                return StandingBookingResponse(
                    success=False,
                    standing_booking=None,
                    message="Standing booking not found"
                )

            return StandingBookingResponse(
                success=True,
                standing_booking=StandingBooking.from_data(standing_booking_data),
                message="Standing booking retrieved successfully"
            )

        except Exception:
            logger.exception("standing_booking query failed")
            return StandingBookingResponse(
                success=False,
                standing_booking=None,
                message="Error retrieving standing booking"
            )

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def standing_bookings_for_person(
        self,
        info: Info[Context],
        person_id: int,
        active_only: bool = True
    ) -> StandingBookingsResponse:
        """Get all standing bookings for a specific person"""
        db: AsyncSession = info.context.db

        try:
            standing_bookings_data = await get_standing_bookings(
                db=db,
                person_id=person_id,
                active_only=active_only
            )

            return StandingBookingsResponse(
                standing_bookings=[StandingBooking.from_data(sb) for sb in standing_bookings_data],
                total_count=len(standing_bookings_data)
            )

        except Exception:
            logger.exception("standing_bookings_for_person query failed")
            return StandingBookingsResponse(
                standing_bookings=[],
                total_count=0
            )

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def standing_bookings_for_template(
        self,
        info: Info[Context],
        template_id: int,
        active_only: bool = True
    ) -> StandingBookingsResponse:
        """Get all standing bookings for a specific template"""
        db: AsyncSession = info.context.db

        try:
            standing_bookings_data = await get_standing_bookings(
                db=db,
                template_id=template_id,
                active_only=active_only
            )

            return StandingBookingsResponse(
                standing_bookings=[StandingBooking.from_data(sb) for sb in standing_bookings_data],
                total_count=len(standing_bookings_data)
            )

        except Exception:
            logger.exception("standing_bookings_for_template query failed")
            return StandingBookingsResponse(
                standing_bookings=[],
                total_count=0
            )

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def class_templates_by_weekday(
        self,
        info: Info[Context],
        weekday: int,
        class_type_id: int = None
    ) -> ClassTemplatesResponse:
        """Get class templates for a specific weekday (0=Sunday, 6=Saturday)"""
        db: AsyncSession = info.context.db

        try:
            # Get all templates first, then filter by weekday
            templates_data = await get_class_templates(
                db=db,
                class_type_id=class_type_id,
                active_only=True
            )

            # Filter by weekday
            weekday_templates = [tmpl for tmpl in templates_data if tmpl.weekday == weekday]

            return ClassTemplatesResponse(
                templates=[ClassTemplate.from_data(tmpl) for tmpl in weekday_templates],
                total_count=len(weekday_templates)
            )

        except Exception:
            logger.exception("class_templates_by_weekday query failed")
            return ClassTemplatesResponse(
                templates=[],
                total_count=0
            )

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def templates_requiring_seats(self, info: Info[Context]) -> ClassTemplatesResponse:
        """Get templates that require seat selection (e.g., spinning classes)"""
        db: AsyncSession = info.context.db

        try:
            # This is a simplified approach - in a real implementation,
            # you might want to have a field in the class_types table
            # that indicates if seats are required

            # For now, we'll get all templates and let the frontend
            # determine which ones require seats based on class type
            templates_data = await get_class_templates(
                db=db,
                active_only=True
            )

            # Filter for class types that typically require seats
            # This could be made more sophisticated with database flags
            seat_requiring_types = ['spinning', 'spin', 'cycling']
            seat_templates = [
                tmpl for tmpl in templates_data
                if tmpl.class_type_name and any(
                    seat_type in tmpl.class_type_name.lower()
                    for seat_type in seat_requiring_types
                )
            ]

            return ClassTemplatesResponse(
                templates=[ClassTemplate.from_data(tmpl) for tmpl in seat_templates],
                total_count=len(seat_templates)
            )

        except Exception:
            logger.exception("templates_requiring_seats query failed")
            return ClassTemplatesResponse(
                templates=[],
                total_count=0
            )
