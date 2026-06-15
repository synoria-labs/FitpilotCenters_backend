"""Chatbot config + propose/confirm pending-action tables.

Creates two FitPilot-native tables in the ``app`` schema for the customer-facing
WhatsApp chatbot agent:

* ``chatbot_config`` — a single editable row (system prompt + business info + toggles + model).
  Seeded with one default row (disabled, require_confirmation=true, model=claude-sonnet-4-6).
* ``chatbot_pending_action`` — the propose-and-confirm ledger (one pending action per
  conversation, unique ``conversation_id``).

Revision ID: f1a2b3c4d5e6
Revises: e7a8b9c0d1f2
Create Date: 2026-06-13 12:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "e7a8b9c0d1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"

_DEFAULT_SYSTEM_PROMPT = (
    "Eres el asistente virtual de FitPilot, un gimnasio. Atiendes a clientes por WhatsApp "
    "en español, de forma cálida, breve y profesional. Ayudas con información del negocio "
    "(precios de membresías, horarios de clases, dirección, disponibilidad) y con la gestión "
    "de reservas y pagos. Usa siempre las herramientas disponibles para obtener datos reales; "
    "nunca inventes precios, horarios ni disponibilidad. Antes de crear una reserva, registrar "
    "un pago o renovar una membresía, propón la acción y pide confirmación explícita del cliente; "
    "solo ejecuta el cambio cuando confirme. Si no encuentras al cliente como socio, ofrécele "
    "información y la opción de inscribirse. No reveles datos de otros clientes."
)


def upgrade() -> None:
    op.create_table(
        "chatbot_config",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "require_confirmation", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "model", sa.String(length=80), nullable=False,
            server_default=sa.text("'claude-sonnet-4-6'"),
        ),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("business_name", sa.String(length=200), nullable=True),
        sa.Column("address", sa.String(length=300), nullable=True),
        sa.Column("operating_hours", sa.Text(), nullable=True),
        sa.Column("phone", sa.String(length=40), nullable=True),
        sa.Column("policies", sa.Text(), nullable=True),
        sa.Column("tone", sa.String(length=200), nullable=True),
        sa.Column("extra_info", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        schema=SCHEMA,
    )

    # Seed the single default configuration row (id assigned by the sequence).
    op.bulk_insert(
        sa.table(
            "chatbot_config",
            sa.column("enabled", sa.Boolean),
            sa.column("require_confirmation", sa.Boolean),
            sa.column("model", sa.String),
            sa.column("system_prompt", sa.Text),
            sa.column("tone", sa.String),
            schema=SCHEMA,
        ),
        [
            {
                "enabled": False,
                "require_confirmation": True,
                "model": "claude-sonnet-4-6",
                "system_prompt": _DEFAULT_SYSTEM_PROMPT,
                "tone": "Cálido, breve y profesional",
            }
        ],
    )

    op.create_table(
        "chatbot_pending_action",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("action_type", sa.String(length=40), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("member_id", sa.BigInteger(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.String(length=20), nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        # No FK to app.conversations: that table is externally-created and lacks a
        # unique constraint on `id` in some deployments, so a FK can't reference it.
        # The unique index below still enforces one pending action per conversation.
        schema=SCHEMA,
    )
    op.create_index(
        "uq_chatbot_pending_conversation",
        "chatbot_pending_action",
        ["conversation_id"],
        unique=True,
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_chatbot_pending_conversation",
        table_name="chatbot_pending_action",
        schema=SCHEMA,
    )
    op.drop_table("chatbot_pending_action", schema=SCHEMA)
    op.drop_table("chatbot_config", schema=SCHEMA)
