"""GraphQL types for the fitness estimation configuration.

Powers the desktop "Estimaciones" admin tab: the policy behind the motivational calorie
figure in win-back campaigns, plus the per-activity intensity that no query can derive.
Read-only alongside them: what the gym's own schedule already says (opening days, class
duration), so the operator can see which half of the calculation they do not need to touch.
"""
from typing import List, Optional

import strawberry

from app.crud.fitnessEstimationCrud import (
    ClassTypeIntensityData,
    FitnessEstimationConfigData,
)


@strawberry.type
class FitnessEstimationConfigType:
    id: Optional[int]
    reference_weight_kg: float
    horizon_weeks: int
    default_sessions_per_week: float
    min_bookings_for_history: int
    cadence_lookback_days: int
    default_met: float
    default_duration_min: int
    default_open_days_per_week: int
    net_of_resting: bool
    kcal_per_kg_fat: int
    metabolic_adaptation: bool
    kg_half_life_days: int
    kg_per_100_kcal_per_day: float
    realization_factor: float

    @classmethod
    def from_data(cls, data: FitnessEstimationConfigData) -> "FitnessEstimationConfigType":
        return cls(
            id=data.id,
            reference_weight_kg=float(data.reference_weight_kg),
            horizon_weeks=int(data.horizon_weeks),
            default_sessions_per_week=float(data.default_sessions_per_week),
            min_bookings_for_history=int(data.min_bookings_for_history),
            cadence_lookback_days=int(data.cadence_lookback_days),
            default_met=float(data.default_met),
            default_duration_min=int(data.default_duration_min),
            default_open_days_per_week=int(data.default_open_days_per_week),
            net_of_resting=bool(data.net_of_resting),
            kcal_per_kg_fat=int(data.kcal_per_kg_fat),
            metabolic_adaptation=bool(data.metabolic_adaptation),
            kg_half_life_days=int(data.kg_half_life_days),
            kg_per_100_kcal_per_day=float(data.kg_per_100_kcal_per_day),
            realization_factor=float(data.realization_factor),
        )


@strawberry.type
class ClassTypeIntensityType:
    id: int
    code: str
    name: str
    # None means "inherited". The UI shows ``effective_met`` either way and marks the
    # difference, so an untouched catalog does not look unconfigured.
    met_value: Optional[float]
    effective_met: float
    is_default: bool

    @classmethod
    def from_data(cls, data: ClassTypeIntensityData) -> "ClassTypeIntensityType":
        return cls(
            id=data.id,
            code=data.code,
            name=data.name,
            met_value=data.met_value,
            effective_met=data.effective_met,
            is_default=data.is_default,
        )


@strawberry.type
class DerivedScheduleType:
    """What the estimate reads from ``class_templates`` — shown, never edited here.

    The point of surfacing it is that an operator who thinks the number is wrong can see
    whether the cause is a knob on this screen or a missing weekly schedule.
    """

    open_days_per_week: int
    open_weekdays: List[int]
    mean_duration_min: Optional[int]
    active_templates: int


@strawberry.type
class EstimationPreviewType:
    """One worked example, so the screen shows what the knobs actually do.

    Every intermediate value, because "36 sesiones x 525 kcal" is checkable and "18,900" is
    an assertion.
    """

    days_inactive: int
    weeks_counted: float
    horizon_reached: bool
    sessions_per_week: float
    sessions_missed: float
    met: float
    duration_min: int
    kcal_per_session: float
    kcal_per_day: float
    kcal: int
    kg_steady_state: float
    kg_fat: float
    kcal_text: str
    kg_fat_text: str
    window_label: str


@strawberry.type
class FitnessEstimationSettings:
    config: FitnessEstimationConfigType
    class_types: List[ClassTypeIntensityType]
    schedule: DerivedScheduleType
    preview: Optional[EstimationPreviewType]


@strawberry.input
class SaveFitnessEstimationConfigInput:
    reference_weight_kg: Optional[float] = None
    horizon_weeks: Optional[int] = None
    default_sessions_per_week: Optional[float] = None
    min_bookings_for_history: Optional[int] = None
    cadence_lookback_days: Optional[int] = None
    default_met: Optional[float] = None
    default_duration_min: Optional[int] = None
    default_open_days_per_week: Optional[int] = None
    net_of_resting: Optional[bool] = None
    kcal_per_kg_fat: Optional[int] = None
    metabolic_adaptation: Optional[bool] = None
    kg_half_life_days: Optional[int] = None
    kg_per_100_kcal_per_day: Optional[float] = None
    realization_factor: Optional[float] = None


@strawberry.input
class SetClassTypeMetInput:
    class_type_id: int
    # Null clears the override and returns the activity to its code default.
    met_value: Optional[float] = None


@strawberry.type
class FitnessEstimationResult:
    success: bool = False
    settings: Optional[FitnessEstimationSettings] = None
    error: Optional[str] = None
