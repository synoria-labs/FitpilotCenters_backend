"""CRUD for the fitness estimation policy (single editable row) and per-activity intensity.

Reads/writes ``app.fitness_estimation_config`` plus the ``met_value`` column on
``app.class_types`` — together, everything behind the "calories you didn't burn" figure that
a query cannot derive from the schedule. Mirrors ``chatbotConfigCrud``: one row, a dataclass,
an ``_EDITABLE_FIELDS`` allow-list and a partial-save upsert that never clears what it was
not asked to change.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ClassType, FitnessEstimationConfig


@dataclass
class FitnessEstimationConfigData:
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
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    @classmethod
    def from_model(cls, m: FitnessEstimationConfig) -> "FitnessEstimationConfigData":
        return cls(
            id=m.id,
            reference_weight_kg=float(m.reference_weight_kg),
            horizon_weeks=int(m.horizon_weeks),
            default_sessions_per_week=float(m.default_sessions_per_week),
            min_bookings_for_history=int(m.min_bookings_for_history),
            cadence_lookback_days=int(m.cadence_lookback_days),
            default_met=float(m.default_met),
            default_duration_min=int(m.default_duration_min),
            default_open_days_per_week=int(m.default_open_days_per_week),
            net_of_resting=bool(m.net_of_resting),
            kcal_per_kg_fat=int(m.kcal_per_kg_fat),
            metabolic_adaptation=bool(m.metabolic_adaptation),
            kg_half_life_days=int(m.kg_half_life_days),
            kg_per_100_kcal_per_day=float(m.kg_per_100_kcal_per_day),
            realization_factor=float(m.realization_factor),
            created_at=m.created_at,
            updated_at=m.updated_at,
        )


@dataclass
class ClassTypeIntensityData:
    """One activity's intensity, plus whether that number is set or merely inherited.

    ``met_value is None`` is the normal state for an existing catalog, so the UI has to be
    able to say "8.5, inherited from the default for spinning" rather than showing a blank
    box that implies nothing is configured.
    """

    id: int
    code: str
    name: str
    met_value: Optional[float]
    effective_met: float
    is_default: bool


async def get_config_model(db: AsyncSession) -> Optional[FitnessEstimationConfig]:
    """Return the single config row (lowest id), or None if not seeded yet."""
    stmt = (
        select(FitnessEstimationConfig)
        .order_by(FitnessEstimationConfig.id.asc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


async def get_config(db: AsyncSession) -> Optional[FitnessEstimationConfigData]:
    model = await get_config_model(db)
    return FitnessEstimationConfigData.from_model(model) if model else None


# Fields the save mutation may set. Mirrors the editable columns.
_EDITABLE_FIELDS = (
    "reference_weight_kg",
    "horizon_weeks",
    "default_sessions_per_week",
    "min_bookings_for_history",
    "cadence_lookback_days",
    "default_met",
    "default_duration_min",
    "default_open_days_per_week",
    "net_of_resting",
    "kcal_per_kg_fat",
    "metabolic_adaptation",
    "kg_half_life_days",
    "kg_per_100_kcal_per_day",
    "realization_factor",
)

# Sanity rails on the values that reach a member's phone. A misplaced decimal in the config
# screen is not a validation error to the database — 700 kg is a perfectly good NUMERIC(5,2)
# — but it is a message the gym cannot take back.
_RANGES = {
    "reference_weight_kg": (30.0, 200.0),
    "horizon_weeks": (1, 260),
    "default_sessions_per_week": (0.5, 7.0),
    "min_bookings_for_history": (1, 100),
    "cadence_lookback_days": (7, 1095),
    "default_met": (1.0, 25.0),
    "default_duration_min": (15, 240),
    "default_open_days_per_week": (1, 7),
    "kcal_per_kg_fat": (5000, 12000),
    # Hall's half-life is about a year; the rails allow a gym to be more or less conservative
    # without letting a typo turn the curve into a step function or a flat line.
    "kg_half_life_days": (30, 1825),
    "kg_per_100_kcal_per_day": (1.0, 10.0),
    "realization_factor": (0.1, 1.0),
}


class EstimationConfigError(ValueError):
    """A knob was set outside the range that produces a sendable message."""


def _validate(applied: Dict) -> None:
    for key, (low, high) in _RANGES.items():
        value = applied.get(key)
        if value is None:
            continue
        if not (low <= float(value) <= high):
            raise EstimationConfigError(
                f"{key} debe estar entre {low} y {high} (recibido: {value})."
            )


async def upsert_config(
    db: AsyncSession, *, commit: bool = True, **fields
) -> FitnessEstimationConfig:
    """Create-or-update the single config row with the provided fields.

    Only keys in ``_EDITABLE_FIELDS`` are applied; ``None`` values are skipped so a partial
    save never clears unrelated columns.
    """
    applied = {k: v for k, v in fields.items() if k in _EDITABLE_FIELDS and v is not None}
    _validate(applied)

    config = await get_config_model(db)
    now = datetime.now(timezone.utc)

    if config is None:
        config = FitnessEstimationConfig(created_at=now, updated_at=now, **applied)
        db.add(config)
    else:
        for key, value in applied.items():
            setattr(config, key, value)
        config.updated_at = now

    await db.flush()
    if commit:
        await db.commit()
        await db.refresh(config)
    return config


async def list_class_type_intensities(db: AsyncSession) -> List[ClassTypeIntensityData]:
    """Every activity with its explicit MET and the value actually in force."""
    # Imported here rather than at module scope: the service imports the models this CRUD
    # also imports, and the default map belongs next to the formula that consumes it.
    from app.services.fitness_estimation_service import DEFAULT_METS, EstimationConfig

    config = await get_config(db)
    global_default = float(config.default_met) if config else EstimationConfig().default_met

    rows = (
        await db.execute(
            select(ClassType.id, ClassType.code, ClassType.name, ClassType.met_value)
            .order_by(ClassType.name.asc())
        )
    ).all()

    result: List[ClassTypeIntensityData] = []
    for type_id, code, name, met_value in rows:
        if met_value is not None:
            result.append(
                ClassTypeIntensityData(
                    id=int(type_id),
                    code=code,
                    name=name,
                    met_value=float(met_value),
                    effective_met=float(met_value),
                    is_default=False,
                )
            )
            continue
        fallback = DEFAULT_METS.get((code or "").strip().lower(), global_default)
        result.append(
            ClassTypeIntensityData(
                id=int(type_id),
                code=code,
                name=name,
                met_value=None,
                effective_met=float(fallback),
                is_default=True,
            )
        )
    return result


async def set_class_type_met(
    db: AsyncSession, class_type_id: int, met_value: Optional[float], *, commit: bool = True
) -> Optional[ClassType]:
    """Set (or clear) one activity's intensity.

    Passing ``None`` clears the override and returns the activity to its code default — the
    reason the column is nullable rather than backfilled in the first place.
    """
    if met_value is not None and not (1.0 <= float(met_value) <= 25.0):
        raise EstimationConfigError(
            f"El MET debe estar entre 1 y 25 (recibido: {met_value})."
        )

    class_type = await db.get(ClassType, class_type_id)
    if class_type is None:
        return None
    class_type.met_value = met_value
    await db.flush()
    if commit:
        await db.commit()
        await db.refresh(class_type)
    return class_type
