"""Stop treating "calories not burned" and "kilograms of fat" as the same number over 7700.

e4f7a0b5d3c6 fixed the magnitude but kept a conceptual error, and papered over it with a
12-week horizon. Calories and kilograms do not behave alike:

* **Calories are linear and cumulative.** Not spending 187 kcal/day for 349 days really is
  65,438 kcal. That is arithmetic, and it needs no ceiling to stay true.
* **Kilograms are not.** Wishnofsky's 7700 kcal/kg (1958) overpredicts long-run weight change
  by roughly a factor of two because the body compensates: appetite and non-exercise activity
  adapt, and a heavier body costs more to maintain, so the deficit self-corrects toward a new
  steady state. Hall et al. (Lancet, 2011) quantify it: a permanent change of 10 kcal/day
  moves body weight about 0.45 kg *eventually*, with half of that reached in roughly a year.

So the horizon was doing the wrong job. It was hiding the linear rule's divergence instead of
correcting it, and it had a side effect that defeats the point of a win-back campaign: every
member past 12 weeks received an identical figure, so a two-year absence read exactly like a
three-month one.

The saturating model fixes both. It cannot run away — it asymptotes at the steady-state
weight by construction, not because of a ceiling someone picked — and it keeps growing with
the absence, so a longer lapse does say more. On this audience the median goes from 2.0 kg
(truncated) to 4.1 kg (real), and nothing exceeds the steady state.

``horizon_weeks`` survives as what it should have been all along: a rail on the *calorie*
figure, so a member who left six years ago does not get quoted a number with six digits. Its
default moves from 12 to 104 — deliberately past the oldest member in this audience (714
days), because a rail that bites is just the old bug at a different offset: everyone beyond
it receives the same figure again, and a two-year absence stops saying more than a one-year
one. It should protect against a pathological outlier, not shape the normal case.
``metabolic_adaptation`` turns the model off and returns to the linear 7700 rule, for a gym
that prefers it.

Revision ID: f5a8b1c6e4d7
Revises: e4f7a0b5d3c6
Create Date: 2026-08-27 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "f5a8b1c6e4d7"
down_revision: Union[str, None] = "e4f7a0b5d3c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"
TABLE = "fitness_estimation_config"

# NOT NULL with a default is safe here: the table holds exactly one row, seeded by the
# previous migration, so there is no backfill to worry about.
COLUMNS = (
    ("metabolic_adaptation", "BOOLEAN NOT NULL DEFAULT TRUE"),
    # Half-life of the approach to steady state. Hall's "half in about a year".
    ("kg_half_life_days", "INTEGER NOT NULL DEFAULT 365"),
    # Steady-state kilograms per 100 kcal/day of sustained change (Hall: 10 kcal/day ~ 0.45 kg).
    ("kg_per_100_kcal_per_day", "NUMERIC(4,2) NOT NULL DEFAULT 4.5"),
)


def upgrade() -> None:
    for column, definition in COLUMNS:
        op.execute(
            f"ALTER TABLE {SCHEMA}.{TABLE} ADD COLUMN IF NOT EXISTS {column} {definition}"
        )

    op.execute(f"ALTER TABLE {SCHEMA}.{TABLE} ALTER COLUMN horizon_weeks SET DEFAULT 104")

    # Move the seeded row to the new default, but only if it is still sitting on the old one.
    # A gym that deliberately chose 12 weeks keeps its choice; this is meant to correct a
    # default nobody picked, not to overwrite a decision somebody made.
    op.execute(f"UPDATE {SCHEMA}.{TABLE} SET horizon_weeks = 104 WHERE horizon_weeks = 12")


def downgrade() -> None:
    op.execute(f"UPDATE {SCHEMA}.{TABLE} SET horizon_weeks = 12 WHERE horizon_weeks = 104")
    op.execute(f"ALTER TABLE {SCHEMA}.{TABLE} ALTER COLUMN horizon_weeks SET DEFAULT 12")
    for column, _definition in reversed(COLUMNS):
        op.execute(f"ALTER TABLE {SCHEMA}.{TABLE} DROP COLUMN IF EXISTS {column}")
