"""GraphQL query for the fitness estimation settings."""
import logging
from typing import Optional

import strawberry
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud import fitnessEstimationCrud as crud
from app.graphql.auth.permissions import IsAuthenticated
from app.graphql.fitness_estimation.types import (
    ClassTypeIntensityType,
    DerivedScheduleType,
    EstimationPreviewType,
    FitnessEstimationConfigType,
    FitnessEstimationSettings,
)
from app.models import ClassTemplate
from app.services import fitness_estimation_service as estimation

logger = logging.getLogger(__name__)


async def build_settings(db: AsyncSession) -> FitnessEstimationSettings:
    """Assemble the whole screen: policy, per-activity intensity, derived schedule, example.

    Shared with the save mutation so an edit returns the recomputed screen — including the
    worked example — rather than making the client re-query to see what it just changed.
    """
    data = await crud.get_config(db)
    if data is None:
        # Never seeded (a database restored from before the migration's INSERT). Materialise
        # the defaults so the screen always has something concrete to edit.
        await crud.upsert_config(db, reference_weight_kg=estimation.EstimationConfig().reference_weight_kg)
        data = await crud.get_config(db)

    profile = await estimation.load_profile(db, use_cache=False)
    intensities = await crud.list_class_type_intensities(db)

    active_templates = int(
        (
            await db.execute(
                select(func.count())
                .select_from(ClassTemplate)
                .where(ClassTemplate.is_active.is_(True))
            )
        ).scalar()
        or 0
    )

    schedule = DerivedScheduleType(
        open_days_per_week=profile.open_days_per_week(),
        open_weekdays=sorted(profile.schedule.open_weekdays),
        mean_duration_min=profile.schedule.mean_duration_min,
        active_templates=active_templates,
    )

    # The example is the busiest activity, since that is the one most members will be quoted.
    busiest = max(intensities, key=lambda i: i.effective_met, default=None)
    result = estimation.estimate_inactivity(
        profile,
        days_inactive=max(1, int(profile.config.horizon_weeks)) * 7,
        class_type_id=busiest.id if busiest else None,
    )
    preview = (
        EstimationPreviewType(
            days_inactive=result.days_inactive,
            weeks_counted=result.weeks_counted,
            horizon_reached=result.horizon_reached,
            sessions_per_week=result.sessions_per_week,
            sessions_missed=result.sessions_missed,
            met=result.met,
            duration_min=result.duration_min,
            kcal_per_session=result.kcal_per_session,
            kcal_per_day=result.kcal_per_day,
            kcal=result.kcal,
            kg_steady_state=result.kg_steady_state,
            kg_fat=result.kg_fat,
            kcal_text=estimation.format_kcal(result.kcal),
            kg_fat_text=estimation.format_kg_fat(result.kg_fat),
            window_label=estimation.format_window_label(result.weeks_counted),
        )
        if result
        else None
    )

    return FitnessEstimationSettings(
        config=FitnessEstimationConfigType.from_data(data),
        class_types=[ClassTypeIntensityType.from_data(i) for i in intensities],
        schedule=schedule,
        preview=preview,
    )


@strawberry.type
class FitnessEstimationQuery:
    @strawberry.field(permission_classes=[IsAuthenticated])
    async def fitness_estimation_settings(self, info: Info) -> Optional[FitnessEstimationSettings]:
        """Estimation policy, per-activity intensity and the schedule it derives from."""
        db: AsyncSession = info.context.db
        return await build_settings(db)
