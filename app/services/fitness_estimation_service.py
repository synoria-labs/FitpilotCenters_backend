"""Defensible numbers for the motivational "calories you didn't burn" campaign variable.

The previous model was ``days_since_the_membership_lapsed * 900 kcal``. Measured against
production it did not merely drift — it produced claims no member could believe. The median
lapsed member is 349 days past their end date, so the message read "314,100 kcal, about
40.8 kg of fat"; the oldest reached 83.5 kg, more than half a body weight. Three separate
mistakes multiplied together:

* **A calendar day is not a missed class.** This gym runs Monday to Friday, so two of every
  seven days were counted as classes that never existed. And nobody attends every open day:
  the real cadence here is about 2.7 bookings per active week, not 7.
* **900 kcal for an hour is roughly double.** The Compendium of Physical Activities rates a
  spin class at 8.5 MET, which for a 70 kg member over 60 minutes is 595 kcal gross and 525
  net of the resting metabolism they would have spent anyway. 900 kcal/h would require about
  12.9 MET sustained — competitive-cyclist territory, not a group class.
* **No horizon.** A linear error with no ceiling, applied to a median of 349 days.

A fourth mistake outlived the first fix and is worth stating separately, because it is the
one that is easy to make again: **calories and kilograms are not the same number scaled by
7700.** Calories are linear and cumulative — not spending 187 kcal/day for a year really is
65,438 kcal, and no ceiling is needed to keep that true. Kilograms are not: the body
compensates, so a removed deficit approaches a new steady weight instead of accumulating.
Wishnofsky's 7700 kcal/kg (1958) therefore overpredicts long-run weight change by roughly a
factor of two. Capping the window hid that divergence rather than correcting it, and it had a
side effect that defeats a win-back campaign: everyone past the horizon received an identical
figure, so a two-year absence read exactly like a three-month one. ``_kg_from_kcal`` uses the
saturating model from Hall et al. (Lancet, 2011) instead; the horizon survives only as a rail
on the calorie figure.

So this module derives what the database already knows and configures only what it cannot.
Opening days and class duration come straight from ``class_templates``; a gym that opens on
Saturdays or runs 45-minute classes is correct the moment its schedule exists, with nothing
to set up. Intensity is the single input no query can infer, so it lives on ``class_types``
as a nullable ``met_value`` that falls back to a per-code default. Everything left over —
reference body mass, how far back to count, what to assume for a member with no history —
is one editable row in ``fitness_estimation_config``.

Nothing here is presented as a medical measurement, and the copy says "estimado". The point
is that the estimate should be defensible: a member who reads it should recognise their own
routine in the number, not a figure that makes the gym look careless.
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Mapping, Optional, Sequence

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ClassSession,
    ClassTemplate,
    ClassType,
    FitnessEstimationConfig,
    Reservation,
)

# A booking is the only attendance signal that actually exists in this deployment: the
# check-in columns are never written, so every historical row sits at "reserved". Mirrors
# ``segmentation_service._ATTENDANCE_STATUSES`` so cadence and affinity agree on what counts.
ATTENDANCE_STATUSES = ("checked_in", "reserved")

# Metabolic equivalents by ``class_types.code``, used when a type has no explicit
# ``met_value``. Values follow the 2011 Compendium of Physical Activities (Ainsworth et al.);
# the spin-class figure is its code 02017, "bicycling, stationary, RPM/Spin bike class".
# These are defaults, not doctrine — a gym whose spinning runs harder sets its own number.
DEFAULT_METS: Mapping[str, float] = {
    "spinning": 8.5,
    "spin": 8.5,
    "cycling": 8.5,
    "indoor_cycling": 8.5,
    "ciclismo": 8.5,
    "hiit": 8.0,
    "crossfit": 8.0,
    "box": 7.8,
    "boxing": 7.8,
    "boxeo": 7.8,
    "funcional": 7.0,
    "functional": 7.0,
    "step": 7.0,
    "zumba": 6.5,
    "baile": 6.5,
    "dance": 6.5,
    "aerobics": 6.5,
    "aerobicos": 6.5,
    "pesas": 5.0,
    "weights": 5.0,
    "fuerza": 5.0,
    "strength": 5.0,
    "gap": 5.0,
    "pilates": 3.8,
    "barre": 3.5,
    "yoga": 3.0,
    "estiramiento": 2.3,
    "stretching": 2.3,
    "movilidad": 2.3,
}

# The profile is gym-wide and changes only when someone edits the schedule or the config
# screen. Re-deriving it per dispatch batch would be three aggregates every sweep for an
# answer that is identical all day.
_PROFILE_TTL_SECONDS = 60.0
_profile_cache: Optional[tuple] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Resolved configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EstimationConfig:
    """The estimation policy, already resolved to plain floats/ints.

    Defaults here are the same values the table declares, so an installation that has never
    opened the config screen behaves exactly like one that saved the defaults.
    """

    reference_weight_kg: float = 70.0
    horizon_weeks: int = 104
    default_sessions_per_week: float = 2.5
    min_bookings_for_history: int = 4
    cadence_lookback_days: int = 180
    default_met: float = 6.0
    default_duration_min: int = 60
    default_open_days_per_week: int = 5
    net_of_resting: bool = True
    kcal_per_kg_fat: int = 7700
    metabolic_adaptation: bool = True
    kg_half_life_days: int = 365
    kg_per_100_kcal_per_day: float = 4.5
    realization_factor: float = 1.0

    @classmethod
    def from_model(cls, row: Optional[FitnessEstimationConfig]) -> "EstimationConfig":
        if row is None:
            return cls()
        return cls(
            reference_weight_kg=float(row.reference_weight_kg),
            horizon_weeks=int(row.horizon_weeks),
            default_sessions_per_week=float(row.default_sessions_per_week),
            min_bookings_for_history=int(row.min_bookings_for_history),
            cadence_lookback_days=int(row.cadence_lookback_days),
            default_met=float(row.default_met),
            default_duration_min=int(row.default_duration_min),
            default_open_days_per_week=int(row.default_open_days_per_week),
            net_of_resting=bool(row.net_of_resting),
            kcal_per_kg_fat=int(row.kcal_per_kg_fat),
            metabolic_adaptation=bool(row.metabolic_adaptation),
            kg_half_life_days=int(row.kg_half_life_days),
            kg_per_100_kcal_per_day=float(row.kg_per_100_kcal_per_day),
            realization_factor=float(row.realization_factor),
        )


@dataclass(frozen=True)
class GymSchedule:
    """What the gym's own ``class_templates`` say about when and how long it runs.

    Derived, never configured. This is the half of the problem the database already answers,
    and reading it is what makes a future gym with Saturday classes or 45-minute sessions
    correct without anyone editing a setting.
    """

    open_weekdays: frozenset = frozenset()
    duration_by_template: Optional[Mapping[int, int]] = None
    duration_by_class_type: Optional[Mapping[int, int]] = None
    met_by_class_type: Optional[Mapping[int, float]] = None
    mean_duration_min: Optional[int] = None

    def __post_init__(self) -> None:
        for field in ("duration_by_template", "duration_by_class_type", "met_by_class_type"):
            if getattr(self, field) is None:
                object.__setattr__(self, field, {})

    @property
    def open_days_per_week(self) -> int:
        return len(self.open_weekdays)


@dataclass(frozen=True)
class EstimationProfile:
    """Config + schedule, the whole input the estimate needs besides the member."""

    config: EstimationConfig
    schedule: GymSchedule

    def open_days_per_week(self) -> int:
        """How many days a week the member could plausibly have come.

        A gym with no active schedule yet falls back to the configured guess rather than to
        zero, which would silently erase every estimate.
        """
        derived = self.schedule.open_days_per_week
        if derived > 0:
            return derived
        return max(1, min(7, self.config.default_open_days_per_week))

    def met_for(self, class_type_id: Optional[int]) -> float:
        """Intensity for one activity: explicit column, then code default, then global."""
        if class_type_id is not None:
            explicit = self.schedule.met_by_class_type.get(class_type_id)
            if explicit:
                return float(explicit)
        return float(self.config.default_met)

    def duration_for(
        self, class_type_id: Optional[int] = None, class_template_id: Optional[int] = None
    ) -> int:
        """Minutes for one class: the member's own slot, then its activity, then the gym mean.

        Narrowest known truth first. A member whose favourite slot is a 90-minute Saturday
        class is estimated on 90 minutes even if everything else at that gym runs 45.
        """
        if class_template_id is not None:
            slot = self.schedule.duration_by_template.get(class_template_id)
            if slot:
                return int(slot)
        if class_type_id is not None:
            activity = self.schedule.duration_by_class_type.get(class_type_id)
            if activity:
                return int(activity)
        if self.schedule.mean_duration_min:
            return int(self.schedule.mean_duration_min)
        return int(self.config.default_duration_min)


def default_profile() -> EstimationProfile:
    """Profile for callers with no database session (tests, pure previews)."""
    return EstimationProfile(config=EstimationConfig(), schedule=GymSchedule())


# ---------------------------------------------------------------------------
# The estimate itself — pure, so it can be unit-tested and previewed without a session
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class InactivityEstimate:
    """Every intermediate value, not just the answer.

    The operator is about to send this to real people over WhatsApp, so the pieces need to
    be inspectable: "36 sessions x 525 kcal" is auditable in a way that "18,900" is not.
    """

    days_inactive: int
    weeks_counted: float
    horizon_reached: bool
    sessions_per_week: float
    cadence_from_history: bool
    sessions_missed: float
    met: float
    duration_min: int
    kcal_per_session: float
    kcal_per_day: float
    kcal: int
    # Where the member's weight would settle if they never came back. The saturating model
    # approaches it and never passes it, which is what makes a ceiling unnecessary.
    kg_steady_state: float
    kg_fat: float


def _kg_from_kcal(cfg: EstimationConfig, *, kcal: float, kcal_per_day: float, days: float):
    """Convert calories not burned into kilograms, and report the ceiling it approaches.

    Two models, because the naive one is wrong in a way that matters at the timescales this
    audience actually sits at.

    ``metabolic_adaptation`` (default) uses Hall et al. (Lancet, 2011): a sustained change of
    10 kcal/day moves body weight about 0.45 kg eventually, with half of that reached in
    roughly a year. Weight therefore approaches a steady state exponentially rather than
    piling up linearly, which is both what bodies do and what keeps the figure from running
    away on a member who left three years ago.

    Off, it falls back to dividing by ``kcal_per_kg_fat`` — Wishnofsky's 1958 rule, which
    overpredicts by roughly a factor of two but is what most people expect to see.

    Returns ``(kg, kg_steady_state)``; the steady state is 0 for the linear model, which has
    no ceiling to report.
    """
    if not cfg.metabolic_adaptation:
        return kcal / float(cfg.kcal_per_kg_fat or 7700), 0.0

    steady = (kcal_per_day / 100.0) * float(cfg.kg_per_100_kcal_per_day)
    half_life = max(1.0, float(cfg.kg_half_life_days))
    fraction = 1.0 - (0.5 ** (days / half_life))
    return steady * fraction, steady


def estimate_inactivity(
    profile: EstimationProfile,
    *,
    days_inactive: Optional[int],
    sessions_per_week: Optional[float] = None,
    class_type_id: Optional[int] = None,
    class_template_id: Optional[int] = None,
) -> Optional[InactivityEstimate]:
    """Translate an absence into calories not burned.

    Returns ``None`` for a member who is not actually lapsed — the caller renders empty
    strings rather than inventing a number, which is the same rule the campaign variables
    already follow for members with no booking history.
    """
    if days_inactive is None or days_inactive <= 0:
        return None

    cfg = profile.config

    # Rail the window. This bounds the *calorie* total, which is linear and would otherwise
    # quote six digits for a five-year absence. It is not what keeps the kilogram figure
    # sane — that is ``_kg_from_kcal``. ``kcal_window_label`` states the window covered.
    weeks_raw = days_inactive / 7.0
    horizon = max(1, int(cfg.horizon_weeks))
    weeks = min(weeks_raw, float(horizon))
    horizon_reached = weeks_raw > horizon

    # Cadence: the member's own if we measured enough of it, otherwise the configured
    # assumption. Either way it can never exceed the days the gym is actually open.
    from_history = sessions_per_week is not None and sessions_per_week > 0
    cadence = float(sessions_per_week) if from_history else float(cfg.default_sessions_per_week)
    cadence = max(0.0, min(cadence, float(profile.open_days_per_week())))
    if cadence <= 0:
        return None

    met = profile.met_for(class_type_id)
    duration_min = profile.duration_for(class_type_id, class_template_id)

    # Net of resting metabolism: the claim is "calories you did NOT burn", and the member was
    # never at zero. Subtracting the 1 MET they spent anyway is the difference between an
    # estimate and a flattering one.
    effective_met = max(met - 1.0, 0.0) if cfg.net_of_resting else met
    kcal_per_session = effective_met * cfg.reference_weight_kg * (duration_min / 60.0)

    sessions = weeks * cadence
    kcal = sessions * kcal_per_session * max(0.0, cfg.realization_factor)
    kcal_per_day = cadence * kcal_per_session / 7.0
    kg_fat, kg_steady = _kg_from_kcal(
        cfg,
        kcal=kcal,
        kcal_per_day=kcal_per_day * max(0.0, cfg.realization_factor),
        days=weeks * 7.0,
    )

    return InactivityEstimate(
        days_inactive=int(days_inactive),
        weeks_counted=round(weeks, 2),
        horizon_reached=horizon_reached,
        sessions_per_week=round(cadence, 2),
        cadence_from_history=bool(from_history),
        sessions_missed=round(sessions, 1),
        met=round(met, 2),
        duration_min=int(duration_min),
        kcal_per_session=round(kcal_per_session, 1),
        kcal_per_day=round(kcal_per_day, 1),
        kcal=int(round(kcal)),
        kg_steady_state=round(kg_steady, 2),
        kg_fat=round(kg_fat, 2),
    )


# ---------------------------------------------------------------------------
# Presentation — one place, so the wizard preview and the real send cannot diverge
# ---------------------------------------------------------------------------
def format_kcal(kcal: int) -> str:
    """Round to a scale that reads like an estimate rather than a measurement.

    "17,000" invites belief; "16,847" invites an argument about the 47.
    """
    if kcal <= 0:
        return ""
    step = 100 if kcal >= 2000 else 50
    return f"{int(round(kcal / step) * step):,}"


def format_kg_fat(kg: float) -> str:
    """One decimal, and never a rounded-to-zero that reads as 'nothing'."""
    if kg <= 0:
        return ""
    return f"{max(kg, 0.1):.1f}"


def format_window_label(weeks: float) -> str:
    """Spanish label for the window the estimate actually covers.

    Exists so the copy can say "en los últimos 3 meses" when the horizon truncates a
    two-year absence, instead of implying the total spans the whole lapse.
    """
    if weeks <= 0:
        return ""
    if weeks < 2:
        return "los últimos días"
    if weeks < 9:
        return f"las últimas {int(round(weeks))} semanas"
    months = int(round(weeks / 4.345))
    if months <= 1:
        return "el último mes"
    # Past a year and a half, months stop being readable ("los últimos 24 meses"). Whole
    # years only: the label is descriptive, and ``weeks_counted`` carries the precision.
    if months >= 18:
        years = max(2, int(round(weeks / 52.18)))
        return f"los últimos {years} años"
    return f"los últimos {months} meses"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------
async def load_schedule(db: AsyncSession) -> GymSchedule:
    """Derive opening days, class durations and per-activity intensity from the schedule.

    Two set-based queries. Only ``is_active`` templates count: a retired Saturday slot should
    not keep inflating how many days a week the gym is open.
    """
    rows = (
        await db.execute(
            select(
                ClassTemplate.id,
                ClassTemplate.weekday,
                ClassTemplate.class_type_id,
                ClassTemplate.default_duration_min,
            ).where(ClassTemplate.is_active.is_(True))
        )
    ).all()

    open_weekdays = set()
    duration_by_template: Dict[int, int] = {}
    durations_by_type: Dict[int, list] = {}
    for template_id, weekday, class_type_id, duration in rows:
        if weekday is not None:
            open_weekdays.add(int(weekday))
        if duration:
            duration_by_template[int(template_id)] = int(duration)
            durations_by_type.setdefault(int(class_type_id), []).append(int(duration))

    # A class type's duration is the mean of its own slots, so a mixed 45/60 catalog does not
    # get flattened to whichever row happened to come back first.
    duration_by_class_type = {
        type_id: int(round(sum(values) / len(values)))
        for type_id, values in durations_by_type.items()
        if values
    }
    all_durations = [d for values in durations_by_type.values() for d in values]
    mean_duration = int(round(sum(all_durations) / len(all_durations))) if all_durations else None

    type_rows = (
        await db.execute(select(ClassType.id, ClassType.code, ClassType.met_value))
    ).all()
    met_by_class_type: Dict[int, float] = {}
    for type_id, code, met_value in type_rows:
        if met_value is not None:
            met_by_class_type[int(type_id)] = float(met_value)
            continue
        # NULL is the normal state for an existing catalog. Fall back to the code default so
        # spinning is 8.5 without anyone having to run an UPDATE over live data.
        fallback = DEFAULT_METS.get((code or "").strip().lower())
        if fallback:
            met_by_class_type[int(type_id)] = float(fallback)

    return GymSchedule(
        open_weekdays=frozenset(open_weekdays),
        duration_by_template=duration_by_template,
        duration_by_class_type=duration_by_class_type,
        met_by_class_type=met_by_class_type,
        mean_duration_min=mean_duration,
    )


async def load_profile(db: AsyncSession, *, use_cache: bool = True) -> EstimationProfile:
    """Config row + derived schedule, memoised for a minute.

    A dispatch run claims batch after batch; the profile is identical across all of them, so
    without the cache every sweep pays for the same aggregates.
    """
    global _profile_cache
    if use_cache and _profile_cache is not None:
        cached_at, cached = _profile_cache
        if _time.monotonic() - cached_at < _PROFILE_TTL_SECONDS:
            return cached

    row = (
        await db.execute(
            select(FitnessEstimationConfig)
            .order_by(FitnessEstimationConfig.id.asc())
            .limit(1)
        )
    ).scalars().first()
    profile = EstimationProfile(
        config=EstimationConfig.from_model(row), schedule=await load_schedule(db)
    )
    _profile_cache = (_time.monotonic(), profile)
    return profile


def invalidate_profile_cache() -> None:
    """Called by the save mutation so an edit takes effect on the next send, not in a minute."""
    global _profile_cache
    _profile_cache = None


async def cadence_for(
    db: AsyncSession,
    person_ids: Sequence[int],
    *,
    lookback_days: int = 180,
    min_bookings: int = 4,
    now: Optional[datetime] = None,
) -> Dict[int, float]:
    """Bookings per *active* week for each member, in one set-based query.

    Active weeks, not calendar weeks: the question the message asks is "how often did you
    come when you were coming", and dividing by the whole window would fold the ramp-down
    before churn into the answer and understate everyone. Members below ``min_bookings`` are
    left out entirely — the caller then uses the configured default rather than trusting a
    cadence extrapolated from a single booking.
    """
    ids = [int(p) for p in person_ids if p is not None]
    if not ids:
        return {}

    now = now or _now()
    floor = now - timedelta(days=max(1, int(lookback_days)))
    week = func.date_trunc("week", ClassSession.start_at)
    stmt = (
        select(
            Reservation.person_id,
            func.count().label("bookings"),
            func.count(distinct(week)).label("weeks"),
        )
        .join(ClassSession, ClassSession.id == Reservation.session_id)
        .where(
            Reservation.person_id.in_(ids),
            Reservation.status.in_(ATTENDANCE_STATUSES),
            ClassSession.start_at >= floor,
            ClassSession.start_at < now,
        )
        .group_by(Reservation.person_id)
        .having(func.count() >= int(min_bookings))
    )

    cadence: Dict[int, float] = {}
    for person_id, bookings, weeks in (await db.execute(stmt)).all():
        weeks = int(weeks or 0)
        if weeks <= 0:
            continue
        cadence[int(person_id)] = round(float(bookings) / float(weeks), 2)
    return cadence


async def days_since_last_class_for(
    db: AsyncSession, person_ids: Sequence[int], *, now: Optional[datetime] = None
) -> Dict[int, int]:
    """Days since each member's last attended class.

    The honest anchor for "hace X días que no te vemos". It routinely disagrees with the
    membership end date by months — members stop showing up before they stop paying — and it
    is the number that sentence actually claims.
    """
    ids = [int(p) for p in person_ids if p is not None]
    if not ids:
        return {}

    now = now or _now()
    stmt = (
        select(Reservation.person_id, func.max(ClassSession.start_at))
        .join(ClassSession, ClassSession.id == Reservation.session_id)
        .where(
            Reservation.person_id.in_(ids),
            Reservation.status.in_(ATTENDANCE_STATUSES),
            ClassSession.start_at < now,
        )
        .group_by(Reservation.person_id)
    )

    result: Dict[int, int] = {}
    for person_id, last_at in (await db.execute(stmt)).all():
        if last_at is None:
            continue
        last_aware = last_at if last_at.tzinfo else last_at.replace(tzinfo=timezone.utc)
        days = (now - last_aware).days
        if days >= 0:
            result[int(person_id)] = days
    return result
