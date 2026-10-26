"""Additive sales orders, calendar versioning and turn audit. No historical repairs."""
from alembic import op
import sqlalchemy as sa

revision = "e1a4c7d0f3b6"
down_revision = "d0f3b6a9e2c5"
branch_labels = depends_on = None


def upgrade():
    op.add_column("chatbot_config", sa.Column("offer_in_person_registration", sa.Boolean(), server_default=sa.false(), nullable=False), schema="app")
    op.add_column("chatbot_config", sa.Column("sales_v2_enabled", sa.Boolean(), server_default=sa.false(), nullable=False), schema="app")
    op.add_column("membership_subscriptions", sa.Column("period_rule_version", sa.Integer(), server_default="1", nullable=False), schema="app")
    op.create_table("membership_purchase_orders",
        sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("member_id", sa.BigInteger(), sa.ForeignKey("app.people.id")),
        sa.Column("action_type", sa.String(40), nullable=False), sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("facts", sa.JSON(), nullable=False), sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("first_class_at", sa.DateTime(timezone=True)), sa.Column("rule_version", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False), sa.Column("status", sa.String(40), nullable=False),
        sa.Column("external_reference", sa.String(120), unique=True, nullable=False),
        sa.Column("mp_preference_id", sa.String(120)), sa.Column("mp_init_point", sa.Text()),
        sa.Column("payment_id", sa.BigInteger(), sa.ForeignKey("app.payments.id"), unique=True),
        sa.Column("subscription_id", sa.BigInteger(), sa.ForeignKey("app.membership_subscriptions.id")),
        sa.Column("approved_at", sa.DateTime(timezone=True)), sa.Column("confirmation_message_id", sa.BigInteger()),
        sa.Column("review_reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), schema="app")
    op.create_index("ix_membership_purchase_orders_conversation_id", "membership_purchase_orders", ["conversation_id"], schema="app")
    op.create_index("ix_membership_purchase_orders_status", "membership_purchase_orders", ["status"], schema="app")
    op.create_table("chatbot_turn_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False), sa.Column("requested_model", sa.String(80), nullable=False),
        sa.Column("effective_model", sa.String(100)), sa.Column("rules_version", sa.Integer(), nullable=False),
        sa.Column("tools", sa.JSON(), nullable=False), sa.Column("validation", sa.String(80), nullable=False),
        sa.Column("reply_kind", sa.String(40)), sa.Column("order_id", sa.BigInteger()),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False), sa.Column("usage", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), schema="app")
    op.create_index("ix_chatbot_turn_audit_conversation_id", "chatbot_turn_audit", ["conversation_id"], schema="app")


def downgrade():
    op.drop_table("chatbot_turn_audit", schema="app")
    op.drop_table("membership_purchase_orders", schema="app")
    op.drop_column("membership_subscriptions", "period_rule_version", schema="app")
    op.drop_column("chatbot_config", "sales_v2_enabled", schema="app")
    op.drop_column("chatbot_config", "offer_in_person_registration", schema="app")
