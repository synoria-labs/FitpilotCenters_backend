"""Per-conversation bot state: bot_enabled + bot_paused_until.

Adds two nullable/defaulted columns to ``app.conversations`` for the WhatsApp bot coexistence:

* ``bot_enabled`` (default true) — manual master switch toggled by the robot button in Chats.
* ``bot_paused_until`` (naive TIMESTAMP, like ``expiration_timestamp``) — temporary auto-pause set
  when a human replies in Chats (human takeover); the bot resumes automatically when it elapses.

Purely additive. Does not touch the externally-created realtime trigger.

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-06-16 00:30:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("bot_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        schema=SCHEMA,
    )
    op.add_column(
        "conversations",
        sa.Column("bot_paused_until", sa.TIMESTAMP(timezone=False), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("conversations", "bot_paused_until", schema=SCHEMA)
    op.drop_column("conversations", "bot_enabled", schema=SCHEMA)
