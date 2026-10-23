"""
GraphQL types for Class Sessions
"""
from datetime import datetime, date
from typing import Optional, List, Dict, Any
import strawberry

from app.models.classModel import ClassSession as ClassSessionModel


@strawberry.type
class ClassSession:
    """Class Session GraphQL type"""
    id: int
    class_type_id: int
    venue_id: int
    template_id: Optional[int]
    instructor_id: Optional[int]
    name: Optional[str]
    start_at: datetime
    end_at: datetime
    capacity: int
    status: str
    created_at: datetime
    updated_at: datetime

    # Related data
    class_type_name: Optional[str]
    venue_name: Optional[str]
    instructor_name: Optional[str]
    template_name: Optional[str]

    @classmethod
    def from_model(cls, session: ClassSessionModel) -> "ClassSession":
        return cls(
            id=session.id,
            class_type_id=session.class_type_id,
            venue_id=session.venue_id,
            template_id=session.template_id,
            instructor_id=session.instructor_id,
            name=session.name,
            start_at=session.start_at,
            end_at=session.end_at,
            capacity=session.capacity,
            status=session.status,
            created_at=session.created_at,
            updated_at=session.updated_at,
            class_type_name=session.class_type.name if session.class_type else None,
            venue_name=session.venue.name if session.venue else None,
            instructor_name=getattr(session.instructor, 'nombre', None) if session.instructor else None,
            template_name=session.template.name if session.template else None
        )


@strawberry.type
class SessionCapacityInfo:
    """Session capacity information"""
    session_id: int
    capacity: int
    reserved: int
    checked_in: int
    waitlisted: int
    total_reserved: int
    available_spots: int
    is_full: bool


@strawberry.type
class SessionGenerationStats:
    """Statistics from session generation"""
    templates_processed: int
    sessions_created: int
    date_range: str
    templates_with_sessions: List["TemplateSessionInfo"]


@strawberry.type
class TemplateSessionInfo:
    """Information about sessions created for a template"""
    template_id: int
    template_name: Optional[str]
    sessions_created: int
    date_range: str


@strawberry.type
class SessionCoverageReport:
    """Session coverage analysis report"""
    analysis_period: "AnalysisPeriod"
    templates: List["TemplateCoverage"]
    summary: "CoverageSummary"


@strawberry.type
class AnalysisPeriod:
    """Analysis period information"""
    start: date
    end: date
    weeks: int


@strawberry.type
class TemplateCoverage:
    """Coverage information for a template"""
    template_id: int
    template_name: Optional[str]
    weekday: int
    start_time: str
    expected_sessions: int
    existing_sessions: int
    coverage_percentage: float
    has_gaps: bool
    next_missing_dates: List[str]


@strawberry.type
class CoverageSummary:
    """Overall coverage summary"""
    total_templates: int
    templates_with_gaps: int
    total_expected_sessions: int
    total_existing_sessions: int
    overall_coverage_percentage: float


# Input types for mutations and queries
@strawberry.input
class CreateClassSessionInput:
    """Input for creating a class session"""
    template_id: Optional[int] = None
    class_type_id: int
    venue_id: int
    start_at: datetime
    end_at: datetime
    capacity: int
    instructor_id: Optional[int] = None
    name: Optional[str] = None
    status: str = "scheduled"


@strawberry.input
class UpdateSessionCapacityInput:
    """Input for updating session capacity"""
    session_id: int
    new_capacity: int


@strawberry.input
class UpdateSessionStatusInput:
    """Input for updating session status"""
    session_id: int
    new_status: str


@strawberry.input
class GenerateSessionsInput:
    """Input for generating sessions from template"""
    template_id: int
    start_date: date
    end_date: date


@strawberry.input
class GenerateFutureSessionsInput:
    """Input for generating future sessions"""
    template_id: Optional[int] = None
    weeks_ahead: int = 8
    start_from_date: Optional[date] = None


@strawberry.input
class GenerateAndMaterializeInput:
    """Input for generating sessions and materializing standing bookings"""
    template_id: Optional[int] = None
    weeks_ahead: int = 8
    auto_materialize: bool = True


@strawberry.input
class GetClassSessionsInput:
    """Input for filtering class sessions"""
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    venue_id: Optional[int] = None
    instructor_id: Optional[int] = None
    class_type_id: Optional[int] = None
    template_id: Optional[int] = None
    status: Optional[str] = None


@strawberry.input
class EmergencySessionGenerationInput:
    """Input for emergency session generation"""
    template_id: int
    specific_dates: List[date]


# Response types
@strawberry.type
class ClassSessionResponse:
    """Response for class session operations"""
    success: bool
    session: Optional[ClassSession]
    message: str


@strawberry.type
class ClassSessionsResponse:
    """Response for class sessions query"""
    sessions: List[ClassSession]
    total_count: int


@strawberry.type
class SessionCapacityResponse:
    """Response for session capacity query"""
    success: bool
    capacity_info: Optional[SessionCapacityInfo]
    message: str


@strawberry.type
class SessionGenerationResponse:
    """Response for session generation operations"""
    success: bool
    stats: Optional[SessionGenerationStats]
    message: str


@strawberry.type
class GenerateAndMaterializeResponse:
    """Response for generate and materialize operation"""
    success: bool
    generation_stats: Optional[SessionGenerationStats]
    materialization_stats_json: Optional[str]  # JSON string instead of Dict
    message: str


@strawberry.type
class SessionCoverageResponse:
    """Response for session coverage analysis"""
    success: bool
    report: Optional[SessionCoverageReport]
    message: str


@strawberry.type
class MaintenanceResponse:
    """Response for maintenance operations"""
    success: bool
    maintenance_stats_json: Optional[str]  # JSON string instead of Dict
    message: str


# Helper functions for converting data
def convert_generation_stats(stats_dict: Dict[str, Any]) -> SessionGenerationStats:
    """Convert stats dictionary to GraphQL type"""
    templates_info = []
    if "templates_with_sessions" in stats_dict:
        for template_info in stats_dict["templates_with_sessions"]:
            templates_info.append(TemplateSessionInfo(
                template_id=template_info["template_id"],
                template_name=template_info.get("template_name"),
                sessions_created=template_info["sessions_created"],
                date_range=template_info["date_range"]
            ))

    return SessionGenerationStats(
        templates_processed=stats_dict.get("templates_processed", 0),
        sessions_created=stats_dict.get("sessions_created", 0),
        date_range=f"{stats_dict.get('date_range', {}).get('start', '')} to {stats_dict.get('date_range', {}).get('end', '')}",
        templates_with_sessions=templates_info
    )


def convert_capacity_info(capacity_dict: Dict[str, Any]) -> SessionCapacityInfo:
    """Convert capacity dictionary to GraphQL type"""
    return SessionCapacityInfo(
        session_id=capacity_dict["session_id"],
        capacity=capacity_dict["capacity"],
        reserved=capacity_dict["reserved"],
        checked_in=capacity_dict["checked_in"],
        waitlisted=capacity_dict["waitlisted"],
        total_reserved=capacity_dict["total_reserved"],
        available_spots=capacity_dict["available_spots"],
        is_full=capacity_dict["is_full"]
    )


def convert_coverage_report(report_dict: Dict[str, Any]) -> SessionCoverageReport:
    """Convert coverage report dictionary to GraphQL type"""
    analysis_period = AnalysisPeriod(
        start=datetime.fromisoformat(report_dict["analysis_period"]["start"]).date(),
        end=datetime.fromisoformat(report_dict["analysis_period"]["end"]).date(),
        weeks=report_dict["analysis_period"]["weeks"]
    )

    templates = []
    for template_info in report_dict["templates"]:
        templates.append(TemplateCoverage(
            template_id=template_info["template_id"],
            template_name=template_info.get("template_name"),
            weekday=template_info["weekday"],
            start_time=template_info["start_time"],
            expected_sessions=template_info["expected_sessions"],
            existing_sessions=template_info["existing_sessions"],
            coverage_percentage=template_info["coverage_percentage"],
            has_gaps=template_info["has_gaps"],
            next_missing_dates=template_info["next_missing_dates"]
        ))

    summary = CoverageSummary(
        total_templates=report_dict["summary"]["total_templates"],
        templates_with_gaps=report_dict["summary"]["templates_with_gaps"],
        total_expected_sessions=report_dict["summary"]["total_expected_sessions"],
        total_existing_sessions=report_dict["summary"]["total_existing_sessions"],
        overall_coverage_percentage=report_dict["summary"]["overall_coverage_percentage"]
    )

    return SessionCoverageReport(
        analysis_period=analysis_period,
        templates=templates,
        summary=summary
    )


# ------------------------------
# Aggregated seat view types
# ------------------------------
@strawberry.type
class SeatOccupant:
    person_id: int
    full_name: Optional[str]


@strawberry.type
class SeatInfo:
    seat_id: int
    label: str
    status: str  # 'free' | 'occupied' | 'displaced' (booked in a class a special event took)
    occupant: Optional[SeatOccupant]
    will_expire_soon: bool


@strawberry.type
class SessionWithSeats:
    id: int
    name: Optional[str]
    start_at: datetime
    end_at: datetime
    capacity: int
    venue_id: int
    template_id: Optional[int]
    class_type_name: Optional[str]
    seats: List[SeatInfo]
    # 'scheduled' | 'canceled' | 'completed' | 'displaced'
    status: str = "scheduled"
    # Name of the special event that took this class (status 'displaced').
    displaced_by_event: Optional[str] = None
