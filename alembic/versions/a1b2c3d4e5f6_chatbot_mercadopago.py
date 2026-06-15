"""Chatbot MercadoPago: config toggle + pending-action payment columns.

Adds:
* ``chatbot_config.require_mp_payment`` (bool) — toggle to require a MercadoPago payment before
  executing a purchase (editable from the frontend).
* ``chatbot_pending_action.external_reference`` / ``mp_preference_id`` / ``mp_init_point`` — to
  link a proposed purchase to its MercadoPago Checkout Pro preference and match the webhook.

Revision ID: a1b2c3d4e5f6
Revises: f1a2b3c4d5e6
Create Date: 2026-06-15 18:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"


def upgrade() -> None:
    op.add_column(
        "chatbot_config",
        sa.Column(
            "require_mp_payment", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "chatbot_pending_action",
        sa.Column("external_reference", sa.String(length=120), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "chatbot_pending_action",
        sa.Column("mp_preference_id", sa.String(length=120), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "chatbot_pending_action",
        sa.Column("mp_init_point", sa.Text(), nullable=True),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_chatbot_pending_external_reference",
        "chatbot_pending_action",
        ["external_reference"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("external_reference IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_chatbot_pending_external_reference",
        table_name="chatbot_pending_action",
        schema=SCHEMA,
    )
    op.drop_column("chatbot_pending_action", "mp_init_point", schema=SCHEMA)
    op.drop_column("chatbot_pending_action", "mp_preference_id", schema=SCHEMA)
    op.drop_column("chatbot_pending_action", "external_reference", schema=SCHEMA)
    op.drop_column("chatbot_config", "require_mp_payment", schema=SCHEMA)
