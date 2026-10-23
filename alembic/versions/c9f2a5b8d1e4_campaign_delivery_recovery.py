"""Durable campaign requests and opt-in recovery. Existing campaigns never opt in."""
from alembic import op
import sqlalchemy as sa

revision = "c9f2a5b8d1e4"
down_revision = "a7d0e3f6b9c2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("campaigns", sa.Column("daily_cap_policy", sa.String(24), nullable=False,
                                       server_default="respect"), schema="app")
    op.add_column("campaigns", sa.Column("auto_retry_meta_limits", sa.Boolean(), nullable=False,
                                       server_default=sa.false()), schema="app")
    op.create_check_constraint("ck_campaign_daily_cap_policy", "campaigns",
                               "daily_cap_policy IN ('respect','defer','allow_additional')", schema="app")
    op.add_column("campaign_recipients", sa.Column("phone_key", sa.String(100)), schema="app")
    op.create_index("ix_app_campaign_recipients_phone_key", "campaign_recipients", ["phone_key"], schema="app")
    op.drop_constraint("ck_campaign_recipient_status", "campaign_recipients", schema="app")
    op.create_check_constraint("ck_campaign_recipient_status", "campaign_recipients",
                               "status IN ('pending','sending','sent','delivered','read','replied',"
                               "'failed','skipped','opted_out','needs_review')", schema="app")
    op.create_table(
        "campaign_send_attempts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("campaign_id", sa.BigInteger(), sa.ForeignKey("app.campaigns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recipient_id", sa.BigInteger(), sa.ForeignKey("app.campaign_recipients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("phone_key", sa.String(100), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        *[sa.Column(col, sa.DateTime(timezone=True)) for col in
          ("accepted_at", "sent_at", "delivered_at", "read_at", "failed_at")],
        sa.Column("wa_message_id", sa.String(120), unique=True),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("error_code", sa.Integer()),
        sa.Column("error_detail", sa.Text()),
        sa.Column("errors", sa.JSON()),
        sa.CheckConstraint("state IN ('started','accepted','sent','delivered','read','failed','uncertain','not_sent')",
                           name="ck_campaign_attempt_state"), schema="app",
    )
    for col in ("campaign_id", "recipient_id"):
        op.create_index(f"ix_app_campaign_send_attempts_{col}", "campaign_send_attempts", [col], schema="app")
    op.create_index("idx_campaign_attempt_phone", "campaign_send_attempts", ["campaign_id", "phone_key"], schema="app")


def downgrade():
    if op.get_bind().execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM app.campaign_send_attempts)"
    )).scalar():
        raise RuntimeError("Exporta el historial de intentos antes de revertir esta migración.")
    op.drop_table("campaign_send_attempts", schema="app")
    op.execute("UPDATE app.campaign_recipients SET status='failed' WHERE status='needs_review'")
    op.drop_constraint("ck_campaign_recipient_status", "campaign_recipients", schema="app")
    op.create_check_constraint("ck_campaign_recipient_status", "campaign_recipients",
                               "status IN ('pending','sending','sent','delivered','read','replied',"
                               "'failed','skipped','opted_out')", schema="app")
    op.drop_column("campaign_recipients", "phone_key", schema="app")
    op.drop_constraint("ck_campaign_daily_cap_policy", "campaigns", schema="app")
    op.drop_column("campaigns", "auto_retry_meta_limits", schema="app")
    op.drop_column("campaigns", "daily_cap_policy", schema="app")
