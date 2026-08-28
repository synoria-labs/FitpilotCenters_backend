"""Single-row configuration for the motivational fitness estimates used in campaigns.

The win-back message tells a lapsed member how many calories they did not burn while
they were away. That number is an *estimate built from assumptions* — body mass, how
often the member used to come, how much of the gross expenditure actually counts as
"extra" — and every one of those assumptions differs per gym. Hardcoding them (which is
what ``AVG_KCAL_PER_SESSION = 900`` did) produced a figure roughly 36x too large for the
median lapsed member, because a constant cannot know that this gym opens five days a week
and that this member used to come 2.7 times in each of them.

What is *derivable* is deliberately not stored here: opening days and class duration come
from ``class_templates`` and the per-activity intensity from ``class_types.met_value``,
so a gym that opens on Saturdays gets the right answer the moment it creates the schedule.
This table holds only the knobs no query can infer.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, Integer, BigInteger, Numeric, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.postgresql import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FitnessEstimationConfig(Base):
    """Single-row estimation policy, editable from the desktop frontend."""

    __tablename__ = "fitness_estimation_config"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # Body mass the estimate is quoted for. Calorie burn scales linearly with it and the
    # system does not track member weight, so one honest reference beats a fake per-member
    # figure. 70 kg sits between the Mexican adult averages for women and men.
    reference_weight_kg: Mapped[float] = mapped_column(
        Numeric(5, 2), nullable=False, default=70
    )

    # A rail on the *calorie* figure only. Calories are linear and cumulative, so they stay
    # true however long the absence, but a six-year lapse would quote a six-digit number that
    # reads as a bug. Set deliberately past the oldest member in a real audience: a rail that
    # bites is the old bug at a new offset — everyone beyond it gets the same figure, so a
    # two-year absence stops saying more than a one-year one. ``kcal_window_label`` names the
    # window covered. Kilograms need no ceiling at all — see ``metabolic_adaptation``.
    horizon_weeks: Mapped[int] = mapped_column(Integer, nullable=False, default=104)

    # Sessions per week assumed for a member whose own history is too thin to measure
    # (about 30% of the lapsed audience never booked a single class).
    default_sessions_per_week: Mapped[float] = mapped_column(
        Numeric(4, 2), nullable=False, default=2.5
    )

    # Bookings required before the member's own cadence is trusted over the default above.
    min_bookings_for_history: Mapped[int] = mapped_column(Integer, nullable=False, default=4)

    # How far back cadence is measured.
    cadence_lookback_days: Mapped[int] = mapped_column(Integer, nullable=False, default=180)

    # Fallbacks for a gym with no schedule yet (no active class_templates to derive from).
    default_met: Mapped[float] = mapped_column(Numeric(4, 2), nullable=False, default=6.0)
    default_duration_min: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    default_open_days_per_week: Mapped[int] = mapped_column(Integer, nullable=False, default=5)

    # Subtract the 1 MET the member would have spent resting anyway. The claim is "calories
    # you did NOT burn", and sitting at home is not zero — leaving this on is the honest read.
    net_of_resting: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Wishnofsky's 7700 kcal/kg. Only used when ``metabolic_adaptation`` is off: it is a
    # 1958 simplification that overpredicts long-run weight change by roughly a factor of two.
    kcal_per_kg_fat: Mapped[int] = mapped_column(Integer, nullable=False, default=7700)

    # Convert calories to kilograms with a saturating model instead of dividing by 7700.
    # The body compensates — appetite and non-exercise activity adapt, and a heavier body
    # costs more to maintain — so a removed deficit approaches a new steady weight rather
    # than accumulating without limit. This is what keeps the figure from running away
    # without needing an arbitrary ceiling, and what lets a two-year absence still say more
    # than a three-month one. Off = the old linear ``kcal / kcal_per_kg_fat``.
    metabolic_adaptation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Hall et al. (Lancet, 2011): a sustained change of 10 kcal/day moves body weight about
    # 0.45 kg eventually, with half of it reached in roughly a year.
    kg_half_life_days: Mapped[int] = mapped_column(Integer, nullable=False, default=365)
    kg_per_100_kcal_per_day: Mapped[float] = mapped_column(
        Numeric(4, 2), nullable=False, default=4.5
    )

    # Optional discount for the fact that a missed workout is not banked one-for-one as fat
    # (appetite and non-exercise activity compensate). 1.0 = no discount.
    realization_factor: Mapped[float] = mapped_column(
        Numeric(4, 2), nullable=False, default=1.0
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
