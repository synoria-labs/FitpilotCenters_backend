"""manage_users capability + ensure standard roles exist.

Adds the ``manage_users`` capability (granted to admin for display in the
permissions matrix; admin is an implicit super-user in code) and, defensively,
seeds the standard roles so the user-management role picker is never empty in
environments where roles were not seeded manually.

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-06-22 16:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, None] = "d6e7f8a9b0c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"


def upgrade() -> None:
    # Defensive: ensure the standard roles exist (no-op where already present).
    # created_at is NOT NULL with no server default, so it MUST be supplied here:
    # on a fresh DB (new env / CI) the rows don't exist yet, so the INSERT actually
    # runs (rather than hitting ON CONFLICT), and omitting created_at would violate
    # the not-null constraint and abort `alembic upgrade head`.
    op.execute(
        f"""
        INSERT INTO {SCHEMA}.roles (code, description, created_at) VALUES
            ('admin', 'Administrador', now()),
            ('staff', 'Recepción / personal', now()),
            ('instructor', 'Instructor de clases', now()),
            ('member', 'Socio', now())
        ON CONFLICT (code) DO NOTHING
        """
    )

    # Seed the admin grant for display (admin is implicit super-user in code).
    op.execute(
        f"""
        INSERT INTO {SCHEMA}.role_capabilities (role_id, capability)
        SELECT id, 'manage_users' FROM {SCHEMA}.roles WHERE code = 'admin'
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        f"DELETE FROM {SCHEMA}.role_capabilities WHERE capability = 'manage_users'"
    )
