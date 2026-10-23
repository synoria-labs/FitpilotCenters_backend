"""
Session Generator Service for FitPilot
Automates the creation and maintenance of class sessions from templates
"""
from datetime import datetime, date, timedelta
from typing import List, Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from app.crud.classSessionCrud import (
    generate_sessions_from_template,
    maintain_session_window,
    get_sessions_by_template
)
from app.crud.standingBookingsCrud import materialize_standing_bookings
from app.core.timezones import local_date, to_local
from app.models.classModel import ClassTemplate
from sqlalchemy import select

logger = logging.getLogger(__name__)


class SessionGeneratorService:
    """Service to manage automatic session generation and maintenance"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def generate_future_sessions(
        self,
        template_id: Optional[int] = None,
        weeks_ahead: int = 8,
        start_from_date: Optional[date] = None
    ) -> Dict[str, Any]:
        """
        Generate future sessions for templates

        Args:
            template_id: Specific template ID, or None for all active templates
            weeks_ahead: How many weeks into the future to generate sessions
            start_from_date: Start date for generation (defaults to today)

        Returns:
            Statistics about session generation
        """
        start_date = start_from_date or date.today()
        end_date = start_date + timedelta(weeks=weeks_ahead)

        logger.info(f"Generating sessions from {start_date} to {end_date}")

        if template_id:
            # Generate for specific template
            sessions_created = await generate_sessions_from_template(
                self.db, template_id, start_date, end_date
            )

            return {
                "template_id": template_id,
                "sessions_created": len(sessions_created),
                "date_range": {
                    "start": start_date.isoformat(),
                    "end": end_date.isoformat()
                },
                "sessions": [
                    {
                        "id": session.id,
                        "start_at": session.start_at.isoformat(),
                        "capacity": session.capacity
                    }
                    for session in sessions_created
                ]
            }
        else:
            # Generate for all active templates
            stats = await maintain_session_window(self.db, weeks_ahead)
            stats["date_range"] = {
                "start": start_date.isoformat(),
                "end": end_date.isoformat()
            }
            return stats

    async def generate_and_materialize(
        self,
        template_id: Optional[int] = None,
        weeks_ahead: int = 8,
        auto_materialize: bool = True
    ) -> Dict[str, Any]:
        """
        Generate sessions and automatically materialize standing bookings

        Args:
            template_id: Specific template or None for all
            weeks_ahead: Weeks to generate ahead
            auto_materialize: Whether to automatically create reservations

        Returns:
            Combined statistics from generation and materialization
        """
        logger.info("Starting session generation and materialization process")

        # Step 1: Generate sessions
        generation_stats = await self.generate_future_sessions(
            template_id=template_id,
            weeks_ahead=weeks_ahead
        )

        result = {
            "generation": generation_stats,
            "materialization": None
        }

        # Step 2: Materialize standing bookings if requested
        if auto_materialize and generation_stats.get("sessions_created", 0) > 0:
            logger.info("Materializing standing bookings for new sessions")

            try:
                materialization_stats = await materialize_standing_bookings(
                    self.db,
                    window_weeks=weeks_ahead,
                    template_id=template_id
                )
                result["materialization"] = materialization_stats
                logger.info(f"Materialization completed: {materialization_stats}")

            except Exception as e:
                logger.error(f"Materialization failed: {str(e)}")
                result["materialization"] = {
                    "error": str(e),
                    "created_reservations": 0,
                    "materialized_count": 0
                }

        return result

    async def maintain_weekly_schedule(
        self,
        weeks_ahead: int = 8,
        cleanup_old_sessions: bool = False
    ) -> Dict[str, Any]:
        """
        Weekly maintenance job to ensure consistent session availability

        Args:
            weeks_ahead: How many weeks to maintain ahead
            cleanup_old_sessions: Whether to cleanup old completed sessions

        Returns:
            Maintenance statistics
        """
        logger.info(f"Starting weekly schedule maintenance for {weeks_ahead} weeks ahead")

        # Generate missing sessions
        generation_stats = await self.generate_future_sessions(weeks_ahead=weeks_ahead)

        # Materialize standing bookings
        materialization_stats = await materialize_standing_bookings(
            self.db,
            window_weeks=weeks_ahead
        )

        result = {
            "maintenance_date": datetime.utcnow().isoformat(),
            "generation": generation_stats,
            "materialization": materialization_stats,
            "cleanup": None
        }

        # Optional cleanup of old sessions
        if cleanup_old_sessions:
            cleanup_stats = await self._cleanup_old_sessions()
            result["cleanup"] = cleanup_stats

        logger.info("Weekly maintenance completed successfully")
        return result

    async def get_session_coverage_report(
        self,
        weeks_ahead: int = 8
    ) -> Dict[str, Any]:
        """
        Generate a report of session coverage for all templates

        Args:
            weeks_ahead: How many weeks to analyze

        Returns:
            Coverage report showing gaps and availability
        """
        start_date = date.today()
        end_date = start_date + timedelta(weeks=weeks_ahead)

        # Get all active templates
        templates_query = select(ClassTemplate).where(ClassTemplate.is_active == True)
        result = await self.db.execute(templates_query)
        templates = result.scalars().all()

        coverage_report = {
            "analysis_period": {
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
                "weeks": weeks_ahead
            },
            "templates": [],
            "summary": {
                "total_templates": len(templates),
                "templates_with_gaps": 0,
                "total_expected_sessions": 0,
                "total_existing_sessions": 0
            }
        }

        for template in templates:
            # Calculate expected sessions for this template
            expected_sessions = self._calculate_expected_sessions(
                template, start_date, end_date
            )

            # Get existing sessions
            existing_sessions = await get_sessions_by_template(
                self.db, template.id, start_date, end_date
            )

            has_gaps = len(existing_sessions) < expected_sessions
            if has_gaps:
                coverage_report["summary"]["templates_with_gaps"] += 1

            template_info = {
                "template_id": template.id,
                "template_name": template.name,
                "weekday": template.weekday,
                "start_time": template.start_time_local.isoformat(),
                "expected_sessions": expected_sessions,
                "existing_sessions": len(existing_sessions),
                "coverage_percentage": (len(existing_sessions) / expected_sessions * 100) if expected_sessions > 0 else 100,
                "has_gaps": has_gaps,
                "next_missing_dates": self._find_missing_dates(
                    template, start_date, end_date, existing_sessions
                )[:5]  # Show first 5 missing dates
            }

            coverage_report["templates"].append(template_info)
            coverage_report["summary"]["total_expected_sessions"] += expected_sessions
            coverage_report["summary"]["total_existing_sessions"] += len(existing_sessions)

        # Calculate overall coverage
        total_expected = coverage_report["summary"]["total_expected_sessions"]
        total_existing = coverage_report["summary"]["total_existing_sessions"]
        coverage_report["summary"]["overall_coverage_percentage"] = (
            (total_existing / total_expected * 100) if total_expected > 0 else 100
        )

        return coverage_report

    def _calculate_expected_sessions(
        self,
        template: ClassTemplate,
        start_date: date,
        end_date: date
    ) -> int:
        """Calculate how many sessions should exist for a template in the date range"""
        expected_count = 0
        current_date = start_date

        while current_date <= end_date:
            # Check if current date matches template weekday
            if current_date.weekday() == (template.weekday - 1) % 7:
                expected_count += 1
            current_date += timedelta(days=1)

        return expected_count

    def _find_missing_dates(
        self,
        template: ClassTemplate,
        start_date: date,
        end_date: date,
        existing_sessions: List
    ) -> List[str]:
        """Find dates where sessions should exist but don't"""
        existing_dates = {local_date(session.start_at) for session in existing_sessions}
        missing_dates = []

        current_date = start_date
        while current_date <= end_date:
            if current_date.weekday() == (template.weekday - 1) % 7:
                if current_date not in existing_dates:
                    missing_dates.append(current_date.isoformat())
            current_date += timedelta(days=1)

        return missing_dates

    async def _cleanup_old_sessions(self) -> Dict[str, Any]:
        """Clean up old completed sessions (older than 30 days)"""
        # This is a placeholder for cleanup logic
        # In production, you might want to archive rather than delete
        cutoff_date = date.today() - timedelta(days=30)

        # TODO: Implement actual cleanup logic
        # For now, just return placeholder stats
        return {
            "cutoff_date": cutoff_date.isoformat(),
            "sessions_cleaned": 0,
            "action": "placeholder - not implemented"
        }

    async def emergency_session_generation(
        self,
        template_id: int,
        specific_dates: List[date]
    ) -> Dict[str, Any]:
        """
        Emergency generation of sessions for specific dates
        Useful for manual intervention or special schedules
        """
        logger.info(f"Emergency session generation for template {template_id}")

        sessions_created = []

        for target_date in specific_dates:
            # Generate sessions for each specific date
            sessions = await generate_sessions_from_template(
                self.db, template_id, target_date, target_date
            )
            sessions_created.extend(sessions)

        # Materialize standing bookings for the new sessions
        if sessions_created:
            materialization_stats = await materialize_standing_bookings(
                self.db,
                template_id=template_id
            )
        else:
            materialization_stats = {
                "created_reservations": 0,
                "materialized_count": 0
            }

        return {
            "template_id": template_id,
            "target_dates": [d.isoformat() for d in specific_dates],
            "sessions_created": len(sessions_created),
            "materialization": materialization_stats,
            "sessions": [
                {
                    "id": session.id,
                    "date": local_date(session.start_at).isoformat(),
                    "start_time": to_local(session.start_at).time().isoformat()
                }
                for session in sessions_created
            ]
        }
