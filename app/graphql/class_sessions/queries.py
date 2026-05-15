"""
GraphQL queries for Class Sessions
"""
from datetime import date
from typing import List, Optional
import strawberry
from strawberry.types import Info

from app.crud.classSessionCrud import (
    get_class_session_by_id,
    get_sessions_by_template,
    get_sessions_by_date_range,
    get_session_capacity_info
)
from app.services.session_generator import SessionGeneratorService
from app.graphql.auth.permissions import IsAuthenticated
from .types import (
    ClassSession,
    ClassSessionsResponse,
    SessionCapacityResponse,
    SessionCoverageResponse,
    GetClassSessionsInput,
    convert_capacity_info,
    convert_coverage_report,
    SessionWithSeats,
    SeatInfo,
    SeatOccupant
)
from app.crud.reservationsCrud import (
    get_sessions_with_seats_by_date,
    get_week_sessions_with_seats
)

def _to_session_with_seats_items(data: List[dict]) -> List[SessionWithSeats]:
    gql_items: List[SessionWithSeats] = []
    for item in data:
        seats: List[SeatInfo] = []
        for s in item.get('seats', []):
            occ = s.get('occupant')
            seats.append(
                SeatInfo(
                    seat_id=s['seat_id'],
                    label=s['label'],
                    status=s['status'],
                    occupant=SeatOccupant(
                        person_id=occ['person_id'],
                        full_name=occ.get('full_name')
                    ) if occ else None,
                    will_expire_soon=bool(s.get('will_expire_soon', False))
                )
            )

        gql_items.append(
            SessionWithSeats(
                id=item['id'],
                name=item.get('name'),
                start_at=item['start_at'],
                end_at=item['end_at'],
                capacity=item['capacity'],
                venue_id=item['venue_id'],
                template_id=item.get('template_id'),
                class_type_name=item.get('class_type_name'),
                seats=seats
            )
        )

    return gql_items


@strawberry.type
class ClassSessionQueries:
    """Class Session queries"""

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def get_class_session(
        self,
        info: Info,
        session_id: int
    ) -> Optional[ClassSession]:
        """Get a single class session by ID"""
        db = info.context.db

        session = await get_class_session_by_id(db, session_id)
        if session:
            return ClassSession.from_model(session)
        return None

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def get_class_sessions(
        self,
        info: Info,
        filters: Optional[GetClassSessionsInput] = None
    ) -> ClassSessionsResponse:
        """Get class sessions with optional filters"""
        db = info.context.db

        try:
            if not filters:
                filters = GetClassSessionsInput()

            sessions = await get_sessions_by_date_range(
                db=db,
                start_date=filters.start_date or date.today(),
                end_date=filters.end_date,
                venue_id=filters.venue_id,
                instructor_id=filters.instructor_id,
                class_type_id=filters.class_type_id,
                status=filters.status
            )

            session_list = [ClassSession.from_model(session) for session in sessions]

            return ClassSessionsResponse(
                sessions=session_list,
                total_count=len(session_list)
            )

        except Exception as e:
            return ClassSessionsResponse(
                sessions=[],
                total_count=0
            )

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def get_sessions_by_template(
        self,
        info: Info,
        template_id: int,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        status: Optional[str] = None
    ) -> ClassSessionsResponse:
        """Get sessions for a specific template"""
        db = info.context.db

        try:
            sessions = await get_sessions_by_template(
                db=db,
                template_id=template_id,
                start_date=start_date,
                end_date=end_date,
                status=status
            )

            session_list = [ClassSession.from_model(session) for session in sessions]

            return ClassSessionsResponse(
                sessions=session_list,
                total_count=len(session_list)
            )

        except Exception as e:
            return ClassSessionsResponse(
                sessions=[],
                total_count=0
            )

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def get_session_capacity_info(
        self,
        info: Info,
        session_id: int
    ) -> SessionCapacityResponse:
        """Get capacity information for a session"""
        db = info.context.db

        try:
            capacity_info = await get_session_capacity_info(db, session_id)

            if capacity_info:
                return SessionCapacityResponse(
                    success=True,
                    capacity_info=convert_capacity_info(capacity_info),
                    message="Capacity information retrieved successfully"
                )
            else:
                return SessionCapacityResponse(
                    success=False,
                    capacity_info=None,
                    message="Session not found"
                )

        except Exception as e:
            return SessionCapacityResponse(
                success=False,
                capacity_info=None,
                message=f"Error retrieving capacity info: {str(e)}"
            )

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def get_session_coverage_report(
        self,
        info: Info,
        weeks_ahead: int = 8
    ) -> SessionCoverageResponse:
        """Get session coverage analysis report"""
        db = info.context.db

        try:
            service = SessionGeneratorService(db)
            report = await service.get_session_coverage_report(weeks_ahead)

            return SessionCoverageResponse(
                success=True,
                report=convert_coverage_report(report),
                message="Coverage report generated successfully"
            )

        except Exception as e:
            return SessionCoverageResponse(
                success=False,
                report=None,
                message=f"Error generating coverage report: {str(e)}"
            )

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def sessions_with_seats(
        self,
        info: Info,
        date: date,
        venue_id: Optional[int] = None
    ) -> List[SessionWithSeats]:
        """Get sessions for a date including per-seat occupancy and expiry flag."""
        db = info.context.db
        data = await get_sessions_with_seats_by_date(db, date, venue_id)
        return _to_session_with_seats_items(data)

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def week_sessions_with_seats(
        self,
        info: Info,
        start_date: date,
        end_date: date,
        class_type_id: Optional[int] = None,
        venue_id: Optional[int] = None
    ) -> List[SessionWithSeats]:
        """Get sessions for a date range (e.g., a week) including per-seat occupancy.

        Optimized to load all sessions in a single query with optional filtering by class type.
        """
        db = info.context.db
        data = await get_week_sessions_with_seats(db, start_date, end_date, class_type_id, venue_id)
        return _to_session_with_seats_items(data)
