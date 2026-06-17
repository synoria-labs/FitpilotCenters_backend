"""Message read state: is_read + read_at on app.messages.

Adds unread-message tracking for the Chats UI (WhatsApp-style unread badge + "No leídos"
filter):

* ``is_read`` (Boolean, default false) — whether staff/bot has read this inbound message.
* ``read_at`` (naive TIMESTAMP, like the other columns on this externally-created table).

The unread count is ``COUNT(*) WHERE direction='inbound' AND is_read=false`` per conversation,
backed by the partial index added here.

IMPORTANT: all pre-existing rows are backfilled to ``is_read = true`` so historical messages
do NOT show up as unread on first deploy (only messages received after this migration count).

Purely additive (add_column + index). Does not touch the externally-created realtime trigger.

Revision ID: f7a8b9c0d1e2
Revises: a7b8c9d0e1f2
Create Date: 2026-06-17 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        schema=SCHEMA,
    )
    op.add_column(
        "messages",
        sa.Column("read_at", sa.TIMESTAMP(timezone=False), nullable=True),
        schema=SCHEMA,
    )

    # Backfill: every existing message is considered read so we don't surface a huge
    # unread backlog on first deploy. Only messages received after this counts as unread.
    op.execute(sa.text("UPDATE app.messages SET is_read = true WHERE is_read = false"))

    # Tiny partial index covering only genuinely-unread inbound rows — backs both the
    # per-conversation COUNT and the mark-read UPDATE.
    op.create_index(
        "idx_messages_unread_inbound",
        "messages",
        ["conversation_id"],
        unique=False,
        schema=SCHEMA,
        postgresql_where=sa.text("direction = 'inbound' AND is_read = false"),
    )


def downgrade() -> None:
    op.drop_index("idx_messages_unread_inbound", table_name="messages", schema=SCHEMA)
    op.drop_column("messages", "read_at", schema=SCHEMA)
    op.drop_column("messages", "is_read", schema=SCHEMA)
