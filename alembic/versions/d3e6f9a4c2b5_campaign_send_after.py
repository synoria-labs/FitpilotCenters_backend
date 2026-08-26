"""Per-recipient send eligibility, so a deferral is a wait rather than a failure.

Before this, a recipient deferred by quiet hours was written as ``failed`` and the campaign
was rescheduled for *now*. The 5-minute sweep then re-attempted the entire audience, failed
it again, and repeated until 09:00 — burning a night of sweeps, monopolising the single
scheduler slot, and reporting a wall of "failures" nobody had.

``send_after`` states the honest thing instead: this recipient is fine, just not yet. It is
also what lets the dispatcher claim work in bounded batches ("everyone due now") rather than
walking one long in-process loop that cannot survive a restart.

Revision ID: d3e6f9a4c2b5
Revises: c2d5e8f3b1a4
Create Date: 2026-08-22 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "d3e6f9a4c2b5"
down_revision: Union[str, None] = "c2d5e8f3b1a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"

INDEXES = (
    # The dispatch claim: "recipients of this campaign, still sendable, already due".
    (
        "idx_campaign_recipient_dispatch",
        "campaign_recipients",
        "(campaign_id, status, send_after)",
    ),
    # The conversion sweep, which scans unconverted sent recipients inside their window.
    (
        "idx_campaign_recipient_conversion",
        "campaign_recipients",
        "(campaign_id, converted, sent_at)",
    ),
)


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE {SCHEMA}.campaign_recipients
        ADD COLUMN IF NOT EXISTS send_after TIMESTAMPTZ
        """
    )
    for name, table, columns in INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {SCHEMA}.{table} {columns}")


def downgrade() -> None:
    for name, _table, _columns in INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.{name}")
    op.execute(
        f"""
        ALTER TABLE {SCHEMA}.campaign_recipients
        DROP COLUMN IF EXISTS send_after
        """
    )
