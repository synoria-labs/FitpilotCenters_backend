import math
from datetime import date
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MembershipPlan, MembershipSubscription
from app.models.classModel import ClassTemplate

from app.crud.standing_bookings.materialization import _init_materialization_stats
from app.crud.standing_bookings.utils import _get_templates_in_same_group as _get_templates_in_same_group_core
from .utils import _align_date_to_weekday, _calculate_window_end_for_plan


try:
    from app.crud.classSessionCrud import generate_sessions_from_template
    from app.crud.standing_bookings.bookings import create_standing_booking
    from app.crud.standing_bookings.materialization import materialize_standing_bookings

    STANDING_BOOKINGS_AVAILABLE = True
except ImportError:
    STANDING_BOOKINGS_AVAILABLE = False
    create_standing_booking = None
    materialize_standing_bookings = None


async def _get_templates_in_same_group(
    db: AsyncSession,
    template_id: int,
) -> List[ClassTemplate]:
    """
    Get all templates that belong to the same TimeslotGroup.
    Group criteria: same class_type_id + venue_id + start_time_local + instructor_id

    Returns:
        List of ClassTemplate objects ordered by weekday
    """
    if not STANDING_BOOKINGS_AVAILABLE:
        return []

    return await _get_templates_in_same_group_core(db, template_id)


async def _create_standing_bookings_for_group(
    db: AsyncSession,
    subscription: MembershipSubscription,
    template_id: int,
    seat_id: Optional[int] = None,
) -> Tuple[List[int], List[int], Dict[int, date]]:
    """Create standing bookings for ALL templates in the same TimeslotGroup."""
    import logging

    logger = logging.getLogger(__name__)

    if not STANDING_BOOKINGS_AVAILABLE:
        logger.warning("Standing bookings not available, skipping group creation")
        return [], [], {}

    templates = await _get_templates_in_same_group(db, template_id)

    if not templates:
        logger.warning("No templates found for group with template_id %s", template_id)
        return [], [], {}

    logger.info(
        "Creating %s standing bookings for group (templates: %s)",
        len(templates),
        [t.id for t in templates],
    )

    membership_start = subscription.start_at.date()
    membership_end = subscription.end_at.date()

    standing_booking_ids: List[int] = []
    template_ids_used: List[int] = []
    template_start_dates: Dict[int, date] = {}

    for template in templates:
        try:
            aligned_start = _align_date_to_weekday(
                membership_start,
                getattr(template, "weekday", None),
            )

            if aligned_start > membership_end:
                logger.warning(
                    "Skipping template %s: aligned start %s beyond membership end %s",
                    template.id,
                    aligned_start,
                    membership_end,
                )
                continue

            standing_booking = await create_standing_booking(
                db=db,
                person_id=subscription.person_id,
                subscription_id=subscription.id,
                template_id=template.id,
                seat_id=seat_id,
                start_date=aligned_start,
                end_date=membership_end,
            )

            if standing_booking:
                standing_booking_ids.append(standing_booking.id)
                template_ids_used.append(template.id)
                template_start_dates[template.id] = aligned_start
                logger.info(
                    "Created standing booking %s for template %s (weekday %s) starting %s",
                    standing_booking.id,
                    template.id,
                    template.weekday,
                    aligned_start,
                )
            else:
                logger.warning("Failed to create standing booking for template %s", template.id)
        except Exception as exc:
            logger.error("Error creating standing booking for template %s: %s", template.id, exc)
            continue

    logger.info(
        "Successfully created %s standing bookings for templates %s",
        len(standing_booking_ids),
        template_ids_used,
    )
    return standing_booking_ids, template_ids_used, template_start_dates


async def _generate_sessions_for_templates(
    db: AsyncSession,
    template_ids: List[int],
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    weeks_ahead: Optional[int] = None,
) -> dict:
    """
    Generate class sessions for multiple templates.

    Args:
        db: Database session
        template_ids: List of template IDs to generate sessions for
        start_date: Start date for session generation (optional)
        end_date: End date for session generation (optional)
        weeks_ahead: Number of weeks ahead to generate sessions (used if end_date not provided)

    Returns:
        Statistics dict with sessions created per template
    """
    import logging
    from datetime import date as date_type, timedelta

    logger = logging.getLogger(__name__)

    if not STANDING_BOOKINGS_AVAILABLE:
        logger.warning("Session generation not available")
        return {"templates_processed": 0, "sessions_created": 0}

    # Determine date range
    if start_date is None:
        start_date = date_type.today()

    if end_date is None:
        if weeks_ahead is None:
            weeks_ahead = 8
        end_date = start_date + timedelta(weeks=weeks_ahead)

    stats = {
        "templates_processed": 0,
        "sessions_created": 0,
        "templates_with_sessions": [],
    }

    logger.info(
        "Generating sessions for %s templates from %s to %s",
        len(template_ids),
        start_date,
        end_date,
    )

    for template_id in template_ids:
        try:
            sessions_created = await generate_sessions_from_template(
                db=db,
                template_id=template_id,
                start_date=start_date,
                end_date=end_date,
            )

            stats["templates_processed"] += 1
            stats["sessions_created"] += len(sessions_created)

            if sessions_created:
                stats["templates_with_sessions"].append(
                    {
                        "template_id": template_id,
                        "sessions_created": len(sessions_created),
                        "date_range": f"{start_date} to {end_date}",
                    }
                )
                logger.info("Generated %s sessions for template %s", len(sessions_created), template_id)
            else:
                logger.info("No new sessions needed for template %s", template_id)

        except Exception as exc:
            logger.error("Error generating sessions for template %s: %s", template_id, exc)
            # Continue with other templates even if one fails
            continue

    logger.info(
        "Session generation complete: %s total sessions across %s templates",
        stats["sessions_created"],
        stats["templates_processed"],
    )
    return stats


async def _create_reservations_for_subscription(
    db: AsyncSession,
    subscription_id: int,
    start_date: Optional[date] = None,
    weeks_ahead: Optional[int] = None,
) -> dict:
    """
    Helper function to create reservations immediately for a specific subscription.

    Args:
        db: Database session
        subscription_id: The subscription ID to materialize bookings for
        start_date: Start date for materialization (defaults to subscription start)
        weeks_ahead: How many weeks ahead to materialize (optional, calculated from subscription)

    Returns:
        Statistics dictionary with materialization results
    """
    if not STANDING_BOOKINGS_AVAILABLE:
        return _init_materialization_stats()

    try:
        # Get the subscription to determine the materialization window
        subscription_stmt = select(MembershipSubscription).where(
            MembershipSubscription.id == subscription_id
        )
        subscription_result = await db.execute(subscription_stmt)
        subscription = subscription_result.scalar_one_or_none()

        if not subscription:
            return {
                "error": "Subscription not found",
                "created_reservations": 0,
                "materialized_count": 0,
            }

        # Calculate window_weeks based on subscription duration if not provided
        if weeks_ahead is None:
            # Calculate weeks from subscription start to end (add 1 week buffer)
            duration_days = (subscription.end_at.date() - subscription.start_at.date()).days
            weeks_ahead = max(1, (duration_days // 7) + 1)

        # Create reservations only for this specific subscription.
        # The materialize function will respect standing_booking.end_date.
        stats = await materialize_standing_bookings(
            db=db,
            window_weeks=weeks_ahead,
            start_date=start_date if start_date else subscription.start_at.date(),
            subscription_id=subscription_id,
        )
        return stats
    except Exception as exc:
        return {
            "error": str(exc),
            "created_reservations": 0,
            "materialized_count": 0,
        }


def _assert_materialization_success(materialization_stats: dict) -> None:
    errors = materialization_stats.get("errors") or []
    error_message = materialization_stats.get("error")
    created = int(materialization_stats.get("created_reservations") or 0)
    existing = int(materialization_stats.get("skipped_existing") or 0)
    seat_taken = int(materialization_stats.get("skipped_seat_taken") or 0)
    no_capacity = int(materialization_stats.get("skipped_no_capacity") or 0)
    standing_booking_ids = materialization_stats.get("standing_booking_ids") or []

    if not standing_booking_ids:
        raise ValueError("No se pudieron crear los standing bookings para el horario fijo.")

    materialized_total = created + existing
    reasons = []

    if seat_taken:
        reasons.append(f"asientos ocupados: {seat_taken}")
    if no_capacity:
        reasons.append(f"sin cupo: {no_capacity}")
    if error_message:
        reasons.append(f"error: {error_message}")
    if errors:
        reasons.append(f"errores: {len(errors)}")
    if materialized_total == 0:
        reasons.append("no se generaron reservas")

    if reasons:
        raise ValueError(f"No se pudieron materializar las reservas ({'; '.join(reasons)}).")


async def _handle_fixed_timeslot_effects(
    db: AsyncSession,
    subscription: MembershipSubscription,
    plan: MembershipPlan,
    template_id: int,
    seat_id: Optional[int] = None,
    *,
    auto_materialize: bool = True,
) -> tuple[Optional[int], dict]:
    """Common helper for fixed time-slot effects (group bookings + sessions + materialization).

    Para membresia semanal con horario fijo: crear exactamente UNA class_session por dia
    (una por template_id del grupo) dentro de la primera semana del periodo de la suscripcion
    y materializar UNA reserva por cada session resultante.
    """
    materialization_stats = _init_materialization_stats()
    generation_stats: dict = {"sessions_created": 0}

    # Create standing bookings for the whole TimeslotGroup
    standing_booking_ids, template_ids_used, template_start_dates = await _create_standing_bookings_for_group(
        db=db,
        subscription=subscription,
        template_id=template_id,
        seat_id=seat_id,
    )
    primary_id = standing_booking_ids[0] if standing_booking_ids else None

    template_start_dates = template_start_dates or {}
    earliest_template_start = min(template_start_dates.values(), default=subscription.start_at.date())
    window_start = min(subscription.start_at.date(), earliest_template_start)

    subscription_end = subscription.end_at.date()
    window_end = _calculate_window_end_for_plan(
        plan=plan,
        window_start=window_start,
        subscription_end=subscription_end,
    )

    coverage_days = max(1, (window_end - window_start).days + 1)
    weeks_ahead = max(1, math.ceil(coverage_days / 7))

    if standing_booking_ids and auto_materialize:
        created_total = 0
        for tid in template_ids_used:
            template_start = template_start_dates.get(tid, window_start)
            gen_stats = await _generate_sessions_for_templates(
                db=db,
                template_ids=[tid],
                start_date=template_start,
                end_date=window_end,
            )
            created_total += int(gen_stats.get("sessions_created", 0))
        generation_stats["sessions_created"] = created_total

        # Create reservations immediately for the same window
        materialization_stats = await _create_reservations_for_subscription(
            db=db,
            subscription_id=subscription.id,
            start_date=window_start,
            weeks_ahead=weeks_ahead,
        )

    template_start_dates_iso = {tid: dt.isoformat() for tid, dt in template_start_dates.items()}

    # Attach aggregation info
    materialization_stats["generation_stats"] = generation_stats
    materialization_stats["standing_booking_ids"] = standing_booking_ids
    materialization_stats["aligned_start_dates"] = template_start_dates_iso
    materialization_stats["window"] = {
        "start": window_start.isoformat(),
        "end": window_end.isoformat(),
        "weeks_ahead": weeks_ahead,
        "coverage_days": coverage_days,
    }
    return primary_id, materialization_stats
