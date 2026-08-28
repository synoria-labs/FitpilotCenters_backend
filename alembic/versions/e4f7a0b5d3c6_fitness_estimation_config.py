"""Make the campaign calorie estimate derivable and configurable instead of a constant.

``AVG_KCAL_PER_SESSION = 900`` multiplied by every calendar day since a membership lapsed.
Against production that is not an approximation, it is an impossible claim: the median
lapsed member is 349 days out, so the message would have said "314,100 kcal, about 40.8 kg
of fat" — and 83.5 kg for the oldest. Three independent errors compounded. Weekends were
counted as missed classes (this gym runs Monday to Friday). Every open day was counted as
attended (the real cadence is ~2.7 bookings per active week). And 900 kcal/hour is roughly
twice a one-hour spin class: the Compendium of Physical Activities puts a spin class at
8.5 MET, which for 70 kg is 525 kcal net of resting metabolism.

The fix is a ladder, and this migration only adds the rungs that cannot be derived.
Opening days and class duration already exist in ``class_templates`` (``weekday``,
``default_duration_min``, ``is_active``), so a gym that opens on Saturdays or runs 45-minute
classes gets the right number from its own schedule with nothing to configure. Intensity is
the one input no query can infer, so it becomes a nullable column on the activity catalog —
NULL keeps its meaning ("use the default for this code"), which is why no row is backfilled
and the existing catalog is left exactly as it is. The remaining assumptions (reference body
mass, how far back to count, what to do when a member has no history) go in a single-row
config table on the ``chatbot_config`` pattern, editable from the desktop app.

``campaign_recipients.sessions_per_week`` freezes the member's own cadence into the audience
snapshot, the same trick as ``favorite_class_type_id`` in c2d5e8f3b1a4: one aggregate per
audience instead of one GROUP BY per message.

Revision ID: e4f7a0b5d3c6
Revises: d3e6f9a4c2b5
Create Date: 2026-08-27 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "e4f7a0b5d3c6"
down_revision: Union[str, None] = "d3e6f9a4c2b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"

# (table, column, type). All nullable on purpose: NULL is the "fall back to the default"
# signal the service already understands, so nothing existing has to be rewritten.
COLUMNS = (
    ("class_types", "met_value", "NUMERIC(4,2)"),
    ("campaign_recipients", "sessions_per_week", "NUMERIC(5,2)"),
)


def upgrade() -> None:
    for table, column, coltype in COLUMNS:
        op.execute(
            f"ALTER TABLE {SCHEMA}.{table} ADD COLUMN IF NOT EXISTS {column} {coltype}"
        )

    # A plausibility rail, not a business rule: 1 MET is lying in bed and ~23 is a world-class
    # sprint. Anything outside that is a typo in the config screen, and a typo here ships to
    # members over WhatsApp.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_class_types_met_range'
            ) THEN
                ALTER TABLE {SCHEMA}.class_types
                ADD CONSTRAINT ck_class_types_met_range
                CHECK (met_value IS NULL OR (met_value >= 1 AND met_value <= 25));
            END IF;
        END $$;
        """
    )

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}.fitness_estimation_config (
            id                         BIGSERIAL PRIMARY KEY,
            reference_weight_kg        NUMERIC(5,2) NOT NULL DEFAULT 70,
            horizon_weeks              INTEGER      NOT NULL DEFAULT 12,
            default_sessions_per_week  NUMERIC(4,2) NOT NULL DEFAULT 2.5,
            min_bookings_for_history   INTEGER      NOT NULL DEFAULT 4,
            cadence_lookback_days      INTEGER      NOT NULL DEFAULT 180,
            default_met                NUMERIC(4,2) NOT NULL DEFAULT 6.0,
            default_duration_min       INTEGER      NOT NULL DEFAULT 60,
            default_open_days_per_week INTEGER      NOT NULL DEFAULT 5,
            net_of_resting             BOOLEAN      NOT NULL DEFAULT TRUE,
            kcal_per_kg_fat            INTEGER      NOT NULL DEFAULT 7700,
            realization_factor         NUMERIC(4,2) NOT NULL DEFAULT 1.0,
            created_at                 TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at                 TIMESTAMPTZ  NOT NULL DEFAULT now()
        )
        """
    )

    # Seed the single row so the config screen has something to edit and the service never
    # has to special-case "not configured yet". Every value is the column default, so this
    # is a no-op semantically — the service behaves identically with or without the row.
    op.execute(
        f"""
        INSERT INTO {SCHEMA}.fitness_estimation_config (id)
        SELECT 1
        WHERE NOT EXISTS (SELECT 1 FROM {SCHEMA}.fitness_estimation_config)
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {SCHEMA}.fitness_estimation_config")
    op.execute(
        f"ALTER TABLE {SCHEMA}.class_types DROP CONSTRAINT IF EXISTS ck_class_types_met_range"
    )
    for table, column, _coltype in reversed(COLUMNS):
        op.execute(f"ALTER TABLE {SCHEMA}.{table} DROP COLUMN IF EXISTS {column}")
