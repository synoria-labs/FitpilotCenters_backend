"""Campaign dispatch heartbeat: rescue runs orphaned by a restart.

``run_campaign`` flips a campaign to ``sending`` and then loops for as long as the
audience takes. If the process dies mid-run nothing ever picks it back up:
``campaigns_due_for_send`` only looks at ``scheduled``, ``trigger_campaign`` refuses a
campaign already ``sending``, and ``resume_campaign`` requires ``paused``. The campaign is
stuck until someone cancels and re-triggers it by hand.

``heartbeat_at`` is stamped periodically while a run is alive, so the scheduler sweep can
tell "actively sending" from "abandoned" and reclaim the latter. Reclaiming is safe: every
recipient is claimed with an atomic compare-and-set, so a second dispatcher never
double-sends.

Revision ID: b1c4d7e2a9f3
Revises: a3c5d7e9f1b2
Create Date: 2026-08-22 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "b1c4d7e2a9f3"
down_revision: Union[str, None] = "a3c5d7e9f1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE {SCHEMA}.campaigns
        ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE {SCHEMA}.campaigns
        DROP COLUMN IF EXISTS heartbeat_at
        """
    )
