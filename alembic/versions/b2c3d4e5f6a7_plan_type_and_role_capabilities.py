"""Add membership plan_type/is_active and role_capabilities grant table.

Part A: membership_plans gains ``plan_type`` (fixed_schedule|flexible|credit_pack)
and ``is_active`` (soft-delete flag). Existing rows are backfilled from
``fixed_time_slot`` so current plans keep working.

Part C: a ``role_capabilities`` table enables capability-based authorization
(e.g. granting ``manage_membership_plans`` to non-admin roles). The ``admin``
role is an implicit super-user in code; we seed its grant only for UI display.

Revision ID: b2c3d4e5f6a7
Revises: f7a8b9c0d1e2
Create Date: 2026-06-19 12:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "f7a8b9c0d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"


def upgrade() -> None:
    # --- Part A: membership_plans columns -------------------------------
    op.execute(
        f"""
        ALTER TABLE {SCHEMA}.membership_plans
        ADD COLUMN IF NOT EXISTS plan_type VARCHAR(20) NOT NULL DEFAULT 'fixed_schedule',
        ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE
        """
    )
    op.execute(
        f"""
        UPDATE {SCHEMA}.membership_plans
        SET plan_type = CASE WHEN fixed_time_slot THEN 'fixed_schedule' ELSE 'flexible' END
        """
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_plan_type'
            ) THEN
                ALTER TABLE {SCHEMA}.membership_plans
                ADD CONSTRAINT ck_plan_type
                CHECK (plan_type IN ('fixed_schedule','flexible','credit_pack'));
            END IF;
        END $$;
        """
    )

    # --- Part C: role_capabilities table --------------------------------
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}.role_capabilities (
            role_id    BIGINT NOT NULL REFERENCES {SCHEMA}.roles(id) ON DELETE CASCADE,
            capability VARCHAR(60) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (role_id, capability)
        )
        """
    )
    # Seed admin grant for display (admin is an implicit super-user in code).
    op.execute(
        f"""
        INSERT INTO {SCHEMA}.role_capabilities (role_id, capability)
        SELECT id, 'manage_membership_plans' FROM {SCHEMA}.roles WHERE code = 'admin'
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {SCHEMA}.role_capabilities")
    op.execute(
        f"ALTER TABLE {SCHEMA}.membership_plans DROP CONSTRAINT IF EXISTS ck_plan_type"
    )
    op.execute(
        f"""
        ALTER TABLE {SCHEMA}.membership_plans
        DROP COLUMN IF EXISTS plan_type,
        DROP COLUMN IF EXISTS is_active
        """
    )
