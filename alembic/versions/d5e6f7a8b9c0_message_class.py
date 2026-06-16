"""Outbound coordination: add message_class to messages.

Adds a nullable ``message_class`` ('transactional'|'marketing') column to ``app.messages`` so the
unified WhatsApp outbound gateway can record each send's class. The marketing frequency cap counts
outbound rows where ``message_class='marketing'`` (a partial index supports that lookup).

Purely additive: nullable column + partial index. Does NOT touch the externally-created realtime
trigger ``trg_notify_whatsapp_message`` on ``app.messages``. No backfill (NULL = uncapped).

Revision ID: d5e6f7a8b9c0
Revises: c1d2e3f4a5b6
Create Date: 2026-06-16 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("message_class", sa.String(length=30), nullable=True),
        schema=SCHEMA,
    )
    op.create_index(
        "idx_messages_marketing_contact_ts",
        "messages",
        ["contact_id", "timestamp"],
        unique=False,
        schema=SCHEMA,
        postgresql_where=sa.text("message_class = 'marketing' AND direction = 'outbound'"),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_messages_marketing_contact_ts", table_name="messages", schema=SCHEMA
    )
    op.drop_column("messages", "message_class", schema=SCHEMA)
