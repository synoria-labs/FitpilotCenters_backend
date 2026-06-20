"""Owner/admin WhatsApp agent configuration and state.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-20 10:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"

_DEFAULT_SYSTEM_PROMPT = (
    "Eres el agente administrativo de FitPilot. Respondes por WhatsApp al dueno o "
    "administradores autorizados. Puedes consultar datos reales del negocio usando "
    "herramientas y resumirlos de forma breve y accionable. Nunca inventes metricas, "
    "pagos, horarios, disponibilidad ni datos de socios. Para cualquier accion que "
    "cambie datos, primero presenta un resumen claro y pide confirmacion explicita."
)


def upgrade() -> None:
    op.create_table(
        "owner_agent_config",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "require_confirmation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "model",
            sa.String(length=80),
            nullable=False,
            server_default=sa.text("'claude-sonnet-4-6'"),
        ),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("history_limit", sa.Integer(), nullable=False, server_default=sa.text("30")),
        sa.Column("max_tokens", sa.Integer(), nullable=False, server_default=sa.text("1024")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        schema=SCHEMA,
    )
    op.bulk_insert(
        sa.table(
            "owner_agent_config",
            sa.column("enabled", sa.Boolean),
            sa.column("require_confirmation", sa.Boolean),
            sa.column("model", sa.String),
            sa.column("system_prompt", sa.Text),
            sa.column("history_limit", sa.Integer),
            sa.column("max_tokens", sa.Integer),
            schema=SCHEMA,
        ),
        [
            {
                "enabled": False,
                "require_confirmation": True,
                "model": "claude-sonnet-4-6",
                "system_prompt": _DEFAULT_SYSTEM_PROMPT,
                "history_limit": 30,
                "max_tokens": 1024,
            }
        ],
    )

    op.create_table(
        "owner_agent_authorized_phone",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("phone_number", sa.String(length=40), nullable=False),
        sa.Column("normalized_wa_id", sa.String(length=30), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["created_by"], [f"{SCHEMA}.accounts.id"]),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_owner_agent_authorized_phone_wa",
        "owner_agent_authorized_phone",
        ["normalized_wa_id"],
        unique=True,
        schema=SCHEMA,
    )
    op.create_index(
        "idx_owner_agent_authorized_phone_enabled",
        "owner_agent_authorized_phone",
        ["enabled"],
        unique=False,
        schema=SCHEMA,
    )
    op.bulk_insert(
        sa.table(
            "owner_agent_authorized_phone",
            sa.column("label", sa.String),
            sa.column("phone_number", sa.String),
            sa.column("normalized_wa_id", sa.String),
            sa.column("enabled", sa.Boolean),
            schema=SCHEMA,
        ),
        [
            {
                "label": "Dueno",
                "phone_number": "8719708890",
                "normalized_wa_id": "5218719708890",
                "enabled": True,
            }
        ],
    )

    op.create_table(
        "owner_agent_pending_action",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("authorized_phone_id", sa.BigInteger(), nullable=True),
        sa.Column("action_type", sa.String(length=50), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["authorized_phone_id"],
            [f"{SCHEMA}.owner_agent_authorized_phone.id"],
            ondelete="SET NULL",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_owner_agent_pending_conversation",
        "owner_agent_pending_action",
        ["conversation_id"],
        unique=True,
        schema=SCHEMA,
    )
    op.create_index(
        "idx_owner_agent_pending_status",
        "owner_agent_pending_action",
        ["status"],
        unique=False,
        schema=SCHEMA,
    )

    op.create_table(
        "owner_agent_audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("authorized_phone_id", sa.BigInteger(), nullable=True),
        sa.Column("tool_name", sa.String(length=100), nullable=True),
        sa.Column("action_type", sa.String(length=50), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'ok'")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["authorized_phone_id"],
            [f"{SCHEMA}.owner_agent_authorized_phone.id"],
            ondelete="SET NULL",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "idx_owner_agent_audit_phone_created",
        "owner_agent_audit_log",
        ["authorized_phone_id", "created_at"],
        unique=False,
        schema=SCHEMA,
    )
    op.create_index(
        "idx_owner_agent_audit_conversation",
        "owner_agent_audit_log",
        ["conversation_id"],
        unique=False,
        schema=SCHEMA,
    )

    op.create_table(
        "owner_tasks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'open'")),
        sa.Column("due_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_by_phone_id", sa.BigInteger(), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_phone_id"],
            [f"{SCHEMA}.owner_agent_authorized_phone.id"],
            ondelete="SET NULL",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "idx_owner_tasks_status_due",
        "owner_tasks",
        ["status", "due_at"],
        unique=False,
        schema=SCHEMA,
    )
    op.create_index(
        "idx_owner_tasks_created_by",
        "owner_tasks",
        ["created_by_phone_id"],
        unique=False,
        schema=SCHEMA,
    )

    op.execute(
        f"""
        INSERT INTO {SCHEMA}.role_capabilities (role_id, capability)
        SELECT id, 'manage_owner_agent' FROM {SCHEMA}.roles WHERE code = 'admin'
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        f"DELETE FROM {SCHEMA}.role_capabilities WHERE capability = 'manage_owner_agent'"
    )
    op.drop_index("idx_owner_tasks_created_by", table_name="owner_tasks", schema=SCHEMA)
    op.drop_index("idx_owner_tasks_status_due", table_name="owner_tasks", schema=SCHEMA)
    op.drop_table("owner_tasks", schema=SCHEMA)
    op.drop_index(
        "idx_owner_agent_audit_conversation",
        table_name="owner_agent_audit_log",
        schema=SCHEMA,
    )
    op.drop_index(
        "idx_owner_agent_audit_phone_created",
        table_name="owner_agent_audit_log",
        schema=SCHEMA,
    )
    op.drop_table("owner_agent_audit_log", schema=SCHEMA)
    op.drop_index(
        "idx_owner_agent_pending_status",
        table_name="owner_agent_pending_action",
        schema=SCHEMA,
    )
    op.drop_index(
        "uq_owner_agent_pending_conversation",
        table_name="owner_agent_pending_action",
        schema=SCHEMA,
    )
    op.drop_table("owner_agent_pending_action", schema=SCHEMA)
    op.drop_index(
        "idx_owner_agent_authorized_phone_enabled",
        table_name="owner_agent_authorized_phone",
        schema=SCHEMA,
    )
    op.drop_index(
        "uq_owner_agent_authorized_phone_wa",
        table_name="owner_agent_authorized_phone",
        schema=SCHEMA,
    )
    op.drop_table("owner_agent_authorized_phone", schema=SCHEMA)
    op.drop_table("owner_agent_config", schema=SCHEMA)
