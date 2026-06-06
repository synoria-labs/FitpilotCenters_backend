"""Notification settings + idempotency log for automated WhatsApp notifications.

Creates two FitPilot-native tables in the ``app`` schema:

* ``notification_settings`` — per-event config (template + variable mapping + offsets).
* ``notification_log`` — idempotency/audit ledger with a unique ``dedup_key``.

Revision ID: b3f1c9a2d7e4
Revises: 2c1a4e6f8b90
Create Date: 2026-06-05 21:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b3f1c9a2d7e4"
down_revision: Union[str, None] = "2c1a4e6f8b90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"


def upgrade() -> None:
    op.create_table(
        "notification_settings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("template_id", sa.BigInteger(), nullable=True),
        sa.Column("param_mapping", sa.JSON(), nullable=True),
        sa.Column("offsets_days", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["template_id"],
            [f"{SCHEMA}.whatsapp_templates.id"],
            ondelete="SET NULL",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_notification_settings_event",
        "notification_settings",
        ["event_type"],
        unique=True,
        schema=SCHEMA,
    )

    op.create_table(
        "notification_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("person_id", sa.BigInteger(), nullable=True),
        sa.Column("subscription_id", sa.BigInteger(), nullable=True),
        sa.Column("template_id", sa.BigInteger(), nullable=True),
        sa.Column("dedup_key", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("wa_message_id", sa.String(length=120), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["person_id"],
            [f"{SCHEMA}.people.id"],
            ondelete="CASCADE",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_notification_log_dedup",
        "notification_log",
        ["dedup_key"],
        unique=True,
        schema=SCHEMA,
    )
    op.create_index(
        "idx_notification_log_event_person",
        "notification_log",
        ["event_type", "person_id"],
        unique=False,
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("idx_notification_log_event_person", table_name="notification_log", schema=SCHEMA)
    op.drop_index("uq_notification_log_dedup", table_name="notification_log", schema=SCHEMA)
    op.drop_table("notification_log", schema=SCHEMA)
    op.drop_index("uq_notification_settings_event", table_name="notification_settings", schema=SCHEMA)
    op.drop_table("notification_settings", schema=SCHEMA)
