"""
Modern GraphQL queries for reservations.
"""
import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, List
from datetime import datetime, timedelta, timezone
from strawberry.types import Info

from app.crud.reservationsCrud import (
    get_reservation_by_id,
    get_person_reservations,
    get_session_reservations,
    get_available_sessions,
    get_available_seats
)
from app.graphql.reservations.types import (
    Reservation,
    Session,
    Seat,
    GetSessionsInput,
    GetReservationsInput,
    SessionsResponse,
    ReservationsResponse,
    SeatsResponse
)
from app.graphql.auth.permissions import IsAuthenticated
from app.graphql.context import Context
from app.core.logging_config import get_logger

logger = get_logger("graphql.reservations.queries")


@strawberry.type
class ReservationQuery:
    """Reservation queries"""

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def reservation(
        self,
        info: Info[Context],
        id: int
    ) -> Optional[Reservation]:
        """Get a reservation by ID"""
        db: AsyncSession = info.context.db

        try:
            reservation_data = await get_reservation_by_id(db, id)
            return Reservation.from_data(reservation_data) if reservation_data else None

        except Exception:
            # A DB/query failure here must not look like "reservation not found".
            logger.exception("reservation query failed")
            return None

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def reservations(
        self,
        info: Info[Context],
        input: Optional[GetReservationsInput] = None
    ) -> ReservationsResponse:
        """Get reservations with filters"""
        db: AsyncSession = info.context.db

        try:
            if not input:
                input = GetReservationsInput()

            reservations_data = []

            if input.person_id:
                # Get reservations for a specific person
                reservations_data = await get_person_reservations(
                    db=db,
                    person_id=input.person_id,
                    include_past=input.include_past,
                    include_canceled=input.include_canceled,
                    limit=input.limit
                )
            elif input.session_id:
                # Get reservations for a specific session
                reservations_data = await get_session_reservations(
                    db=db,
                    session_id=input.session_id,
                    include_canceled=input.include_canceled
                )
            else:
                # No specific filter, return empty list for security
                reservations_data = []

            reservations = [Reservation.from_data(data) for data in reservations_data]

            return ReservationsResponse(
                reservations=reservations,
                total_count=len(reservations)
            )

        except Exception:
            logger.exception("reservations query failed")
            return ReservationsResponse(
                reservations=[],
                total_count=0
            )

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def available_sessions(
        self,
        info: Info[Context],
        input: Optional[GetSessionsInput] = None
    ) -> SessionsResponse:
        """Get available sessions with capacity information"""
        db: AsyncSession = info.context.db

        try:
            if not input:
                input = GetSessionsInput()

            # Default to next 7 days if no dates provided
            if not input.start_date:
                input.start_date = datetime.now(timezone.utc)
            if not input.end_date:
                input.end_date = input.start_date + timedelta(days=7)

            sessions_data = await get_available_sessions(
                db=db,
                start_date=input.start_date,
                end_date=input.end_date,
                class_type_id=input.class_type_id,
                venue_id=input.venue_id
            )

            sessions = [Session.from_data(data) for data in sessions_data]

            return SessionsResponse(
                sessions=sessions,
                total_count=len(sessions)
            )

        except Exception:
            logger.exception("available_sessions query failed")
            return SessionsResponse(
                sessions=[],
                total_count=0
            )

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def available_seats(
        self,
        info: Info[Context],
        session_id: int
    ) -> SeatsResponse:
        """Get available seats for a specific session"""
        db: AsyncSession = info.context.db

        try:
            seats_data = await get_available_seats(db, session_id)
            seats = [Seat.from_data(data) for data in seats_data]
            available_count = sum(1 for seat in seats if seat.is_available)

            return SeatsResponse(
                seats=seats,
                available_count=available_count,
                total_count=len(seats)
            )

        except Exception:
            logger.exception("available_seats query failed")
            return SeatsResponse(
                seats=[],
                available_count=0,
                total_count=0
            )

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def person_reservations(
        self,
        info: Info[Context],
        person_id: int,
        include_past: bool = False,
        include_canceled: bool = False,
        limit: int = 100
    ) -> List[Reservation]:
        """Get reservations for a specific person (simplified query)"""
        db: AsyncSession = info.context.db

        try:
            reservations_data = await get_person_reservations(
                db=db,
                person_id=person_id,
                include_past=include_past,
                include_canceled=include_canceled,
                limit=limit
            )

            return [Reservation.from_data(data) for data in reservations_data]

        except Exception:
            logger.exception("person_reservations query failed")
            return []

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def session_reservations(
        self,
        info: Info[Context],
        session_id: int,
        include_canceled: bool = False
    ) -> List[Reservation]:
        """Get reservations for a specific session (simplified query)"""
        db: AsyncSession = info.context.db

        try:
            reservations_data = await get_session_reservations(
                db=db,
                session_id=session_id,
                include_canceled=include_canceled
            )

            return [Reservation.from_data(data) for data in reservations_data]

        except Exception:
            logger.exception("session_reservations query failed")
            return []

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def upcoming_sessions(
        self,
        info: Info[Context],
        days_ahead: int = 7,
        class_type_id: Optional[int] = None,
        venue_id: Optional[int] = None
    ) -> List[Session]:
        """Get upcoming sessions (convenience query)"""
        db: AsyncSession = info.context.db

        try:
            start_date = datetime.now(timezone.utc)
            end_date = start_date + timedelta(days=days_ahead)

            sessions_data = await get_available_sessions(
                db=db,
                start_date=start_date,
                end_date=end_date,
                class_type_id=class_type_id,
                venue_id=venue_id
            )

            return [Session.from_data(data) for data in sessions_data]

        except Exception:
            logger.exception("upcoming_sessions query failed")
            return []
