"""Performance indexes: auth sessions lookup + booking-path drift reconciliation.

- ``sessions.session`` is looked up on EVERY authenticated request
  (``verify_session``) and ``sessions.user_id`` on session listing/revocation;
  neither had an index.
- ``idx_reservations_session``, ``idx_sessions_instructor`` and
  ``idx_sessions_template`` are declared on the models (classModel.py) but were
  missing in the real database (schema drift, tables predate the models).
  ``idx_reservations_session (session_id, status)`` backs the per-session
  capacity count used by every booking flow.

All statements are idempotent (IF NOT EXISTS) so environments where any of
these already exist are unaffected.

Revision ID: a3c5d7e9f1b2
Revises: f8b1c2d3e4a5
Create Date: 2026-07-06 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "a3c5d7e9f1b2"
down_revision: Union[str, None] = "f8b1c2d3e4a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"

INDEXES = (
    # (name, table, columns)
    ("ix_sessions_session", "sessions", "(session)"),
    ("ix_sessions_user_id", "sessions", "(user_id)"),
    ("idx_reservations_session", "reservations", "(session_id, status)"),
    ("idx_sessions_instructor", "class_sessions", "(instructor_id, start_at)"),
    ("idx_sessions_template", "class_sessions", "(template_id, start_at)"),
)


def upgrade() -> None:
    for name, table, columns in INDEXES:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON {SCHEMA}.{table} {columns}"
        )


def downgrade() -> None:
    for name, _table, _columns in INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.{name}")
