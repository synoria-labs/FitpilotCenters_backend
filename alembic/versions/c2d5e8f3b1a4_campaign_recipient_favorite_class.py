"""Freeze each recipient's favourite class onto the campaign audience snapshot.

Segmenting by "the class this member books most" is only half the value; the message has to
be able to *say* it ("te extrañamos en Spinning de los lunes"). Resolving that at send time
would mean a per-recipient aggregate over reservations — an N+1 across the whole audience.

Instead the favourite class is resolved once, in a single query, when the audience is built,
and frozen here alongside the rest of the snapshot. Typed columns rather than a JSON blob so
the result stays queryable: "how many of the Yoga crowd converted?" is then a GROUP BY over
a ledger that already exists.

Both are nullable: a member with no booking history simply has no favourite class.

Revision ID: c2d5e8f3b1a4
Revises: b1c4d7e2a9f3
Create Date: 2026-08-22 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "c2d5e8f3b1a4"
down_revision: Union[str, None] = "b1c4d7e2a9f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"

COLUMNS = (
    ("favorite_class_type_id", "class_types"),
    ("favorite_class_template_id", "class_templates"),
)


def upgrade() -> None:
    for column, referenced in COLUMNS:
        op.execute(
            f"""
            ALTER TABLE {SCHEMA}.campaign_recipients
            ADD COLUMN IF NOT EXISTS {column} BIGINT
            """
        )
        # ON DELETE SET NULL: deleting a class must never cascade into the send ledger.
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'fk_campaign_recipient_{column}'
                ) THEN
                    ALTER TABLE {SCHEMA}.campaign_recipients
                    ADD CONSTRAINT fk_campaign_recipient_{column}
                    FOREIGN KEY ({column}) REFERENCES {SCHEMA}.{referenced}(id)
                    ON DELETE SET NULL;
                END IF;
            END $$;
            """
        )

    # Backs the "results by class" breakdown on the campaign dashboard.
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_campaign_recipient_favorite_class
        ON {SCHEMA}.campaign_recipients (campaign_id, favorite_class_type_id)
        """
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.idx_campaign_recipient_favorite_class")
    for column, _referenced in COLUMNS:
        op.execute(
            f"""
            ALTER TABLE {SCHEMA}.campaign_recipients
            DROP CONSTRAINT IF EXISTS fk_campaign_recipient_{column}
            """
        )
        op.execute(
            f"""
            ALTER TABLE {SCHEMA}.campaign_recipients
            DROP COLUMN IF EXISTS {column}
            """
        )
