"""GraphQL mutations for the fitness estimation settings.

Both mutations invalidate the service's profile cache on the way out, so an edit is visible
on the next campaign send instead of up to a minute later — the operator who just changed
the reference weight because a message looked wrong should not have to wonder whether it
took.
"""
import logging

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud import fitnessEstimationCrud as crud
from app.crud.fitnessEstimationCrud import EstimationConfigError
from app.graphql.auth.permissions import IsAuthenticated
from app.graphql.fitness_estimation.queries import build_settings
from app.graphql.fitness_estimation.types import (
    FitnessEstimationResult,
    SaveFitnessEstimationConfigInput,
    SetClassTypeMetInput,
)
from app.services import fitness_estimation_service as estimation

logger = logging.getLogger(__name__)


@strawberry.type
class FitnessEstimationMutation:
    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def save_fitness_estimation_config(
        self, info: Info, input: SaveFitnessEstimationConfigInput
    ) -> FitnessEstimationResult:
        """Partial save of the estimation policy: ``None`` fields are left untouched."""
        db: AsyncSession = info.context.db
        try:
            await crud.upsert_config(
                db,
                reference_weight_kg=input.reference_weight_kg,
                horizon_weeks=input.horizon_weeks,
                default_sessions_per_week=input.default_sessions_per_week,
                min_bookings_for_history=input.min_bookings_for_history,
                cadence_lookback_days=input.cadence_lookback_days,
                default_met=input.default_met,
                default_duration_min=input.default_duration_min,
                default_open_days_per_week=input.default_open_days_per_week,
                net_of_resting=input.net_of_resting,
                kcal_per_kg_fat=input.kcal_per_kg_fat,
                metabolic_adaptation=input.metabolic_adaptation,
                kg_half_life_days=input.kg_half_life_days,
                kg_per_100_kcal_per_day=input.kg_per_100_kcal_per_day,
                realization_factor=input.realization_factor,
            )
        except EstimationConfigError as exc:
            await db.rollback()
            return FitnessEstimationResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            logger.exception("Error saving fitness estimation config")
            return FitnessEstimationResult(success=False, error=str(exc))

        estimation.invalidate_profile_cache()
        return FitnessEstimationResult(success=True, settings=await build_settings(db))

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def set_class_type_met(
        self, info: Info, input: SetClassTypeMetInput
    ) -> FitnessEstimationResult:
        """Set or clear one activity's intensity.

        A null ``met_value`` clears the override rather than storing a zero, returning the
        activity to the default for its code.
        """
        db: AsyncSession = info.context.db
        try:
            updated = await crud.set_class_type_met(db, input.class_type_id, input.met_value)
        except EstimationConfigError as exc:
            await db.rollback()
            return FitnessEstimationResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            logger.exception("Error setting class type MET")
            return FitnessEstimationResult(success=False, error=str(exc))

        if updated is None:
            return FitnessEstimationResult(
                success=False, error="No se encontró la actividad."
            )

        estimation.invalidate_profile_cache()
        return FitnessEstimationResult(success=True, settings=await build_settings(db))
