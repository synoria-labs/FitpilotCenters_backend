"""Marketing campaigns: definition, A/B variants, and recipient snapshot/ledger.

Creates three FitPilot-native tables in the ``app`` schema for the user-initiated WhatsApp
broadcast feature (recapture-first MVP):

* ``campaigns`` — campaign definition (objective, audience filter, template + mapping,
  schedule, conversion window, throttle).
* ``campaign_variants`` — A/B variants (one auto-created variant per campaign in the MVP).
* ``campaign_recipients`` — frozen audience snapshot + per-recipient send/track/convert ledger,
  with a unique ``dedup_key`` for idempotency.

Purely additive: no changes to existing tables, no data backfill.

Revision ID: c1d2e3f4a5b6
Revises: a1b2c3d4e5f6
Create Date: 2026-06-15 21:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"


def upgrade() -> None:
    op.create_table(
        "campaigns",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("objective", sa.String(length=30), nullable=False, server_default=sa.text("'win_back'")),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("audience_spec", sa.JSON(), nullable=True),
        sa.Column("template_id", sa.BigInteger(), nullable=True),
        sa.Column("param_mapping", sa.JSON(), nullable=True),
        sa.Column("header_media_url", sa.String(length=1000), nullable=True),
        sa.Column("header_media_asset_id", sa.BigInteger(), nullable=True),
        sa.Column("marketing_campaign_id", sa.BigInteger(), nullable=True),
        sa.Column("scheduled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("send_local_time", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("conversion_window_days", sa.Integer(), nullable=False, server_default=sa.text("14")),
        sa.Column("conversion_metric", sa.String(length=20), nullable=False, server_default=sa.text("'payment'")),
        sa.Column("recency_block_days", sa.Integer(), nullable=False, server_default=sa.text("30")),
        sa.Column("throttle_per_minute", sa.Integer(), nullable=False, server_default=sa.text("60")),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "objective IN ('win_back','renewal_push','engagement','broadcast')",
            name="ck_campaign_objective",
        ),
        sa.CheckConstraint(
            "status IN ('draft','scheduled','sending','paused','completed','canceled')",
            name="ck_campaign_status",
        ),
        sa.ForeignKeyConstraint(["template_id"], [f"{SCHEMA}.whatsapp_templates.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["header_media_asset_id"], [f"{SCHEMA}.whatsapp_media_assets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["marketing_campaign_id"], [f"{SCHEMA}.marketing_campaigns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], [f"{SCHEMA}.accounts.id"], ondelete="SET NULL"),
        schema=SCHEMA,
    )
    op.create_index("idx_campaigns_status_scheduled", "campaigns", ["status", "scheduled_at"], schema=SCHEMA)
    op.create_index("idx_campaigns_objective_created", "campaigns", ["objective", "created_at"], schema=SCHEMA)

    op.create_table(
        "campaign_variants",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("variant_code", sa.String(length=8), nullable=False, server_default=sa.text("'A'")),
        sa.Column("template_id", sa.BigInteger(), nullable=True),
        sa.Column("param_mapping", sa.JSON(), nullable=True),
        sa.Column("header_media_url", sa.String(length=1000), nullable=True),
        sa.Column("header_media_asset_id", sa.BigInteger(), nullable=True),
        sa.Column("weight", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_control", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["campaign_id"], [f"{SCHEMA}.campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["template_id"], [f"{SCHEMA}.whatsapp_templates.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["header_media_asset_id"], [f"{SCHEMA}.whatsapp_media_assets.id"], ondelete="SET NULL"),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_campaign_variant_code", "campaign_variants", ["campaign_id", "variant_code"],
        unique=True, schema=SCHEMA,
    )

    op.create_table(
        "campaign_recipients",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("variant_id", sa.BigInteger(), nullable=True),
        sa.Column("person_id", sa.BigInteger(), nullable=True),
        sa.Column("lead_id", sa.BigInteger(), nullable=True),
        sa.Column("subscription_id", sa.BigInteger(), nullable=True),
        sa.Column("phone_e164", sa.String(length=32), nullable=True),
        sa.Column("wa_id", sa.String(length=100), nullable=True),
        sa.Column("dedup_key", sa.String(length=140), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("skip_reason", sa.String(length=40), nullable=True),
        sa.Column("wa_message_id", sa.String(length=120), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("read_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("replied_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("converted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("converted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("conversion_payment_id", sa.BigInteger(), nullable=True),
        sa.Column("targeted_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status IN ('pending','sending','sent','delivered','read','replied',"
            "'failed','skipped','opted_out')",
            name="ck_campaign_recipient_status",
        ),
        sa.CheckConstraint(
            "person_id IS NOT NULL OR lead_id IS NOT NULL OR phone_e164 IS NOT NULL",
            name="ck_campaign_recipient_target",
        ),
        sa.ForeignKeyConstraint(["campaign_id"], [f"{SCHEMA}.campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["variant_id"], [f"{SCHEMA}.campaign_variants.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["person_id"], [f"{SCHEMA}.people.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lead_id"], [f"{SCHEMA}.leads.id"], ondelete="SET NULL"),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_campaign_recipient_dedup", "campaign_recipients", ["dedup_key"],
        unique=True, schema=SCHEMA,
    )
    op.create_index(
        "idx_campaign_recipient_campaign_status", "campaign_recipients",
        ["campaign_id", "status"], schema=SCHEMA,
    )
    op.create_index(
        "idx_campaign_recipient_person_targeted", "campaign_recipients",
        ["person_id", "targeted_at"], schema=SCHEMA,
    )
    op.create_index(
        "idx_campaign_recipient_wa_message", "campaign_recipients",
        ["wa_message_id"], schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("idx_campaign_recipient_wa_message", table_name="campaign_recipients", schema=SCHEMA)
    op.drop_index("idx_campaign_recipient_person_targeted", table_name="campaign_recipients", schema=SCHEMA)
    op.drop_index("idx_campaign_recipient_campaign_status", table_name="campaign_recipients", schema=SCHEMA)
    op.drop_index("uq_campaign_recipient_dedup", table_name="campaign_recipients", schema=SCHEMA)
    op.drop_table("campaign_recipients", schema=SCHEMA)

    op.drop_index("uq_campaign_variant_code", table_name="campaign_variants", schema=SCHEMA)
    op.drop_table("campaign_variants", schema=SCHEMA)

    op.drop_index("idx_campaigns_objective_created", table_name="campaigns", schema=SCHEMA)
    op.drop_index("idx_campaigns_status_scheduled", table_name="campaigns", schema=SCHEMA)
    op.drop_table("campaigns", schema=SCHEMA)
