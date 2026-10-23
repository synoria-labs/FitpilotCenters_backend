"""Allow event payment links without expiry or a seat hold, with manual refund review.

Revision ID: b8e1f4a7c0d3
Revises: a7d0e3f6b9c2
"""
from alembic import op
import sqlalchemy as sa

revision = "b8e1f4a7c0d3"
down_revision = "a7d0e3f6b9c2"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("special_events", "special_event_orders"):
        op.add_column(table, sa.Column("payment_link_expires", sa.Boolean(), nullable=False,
                                      server_default=sa.true()), schema="app")
    op.add_column("special_event_orders", sa.Column("manual_refund_required", sa.Boolean(), nullable=False,
                                                   server_default=sa.false()), schema="app")
    op.add_column("special_event_orders", sa.Column("refund_reviewed_by", sa.BigInteger(),
                  sa.ForeignKey("app.accounts.id")), schema="app")
    op.add_column("special_event_orders", sa.Column("refund_reviewed_at", sa.DateTime(timezone=True)), schema="app")
    op.create_check_constraint("ck_special_event_open_link_no_hold", "special_event_orders",
        "payment_link_expires OR status <> 'awaiting_payment' OR (seat_id IS NULL AND hold_expires_at IS NULL)",
        schema="app")


def downgrade():
    # Do not turn live open links into timeless holds in an older application.
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT EXISTS (SELECT 1 FROM app.special_event_orders "
        "WHERE NOT payment_link_expires AND status IN ('awaiting_payment','needs_review','refund_pending'))")).scalar():
        raise RuntimeError("Resolve open payment links and pending manual refunds before downgrading.")
    op.drop_constraint("ck_special_event_open_link_no_hold", "special_event_orders", schema="app", type_="check")
    for field in ("refund_reviewed_at", "refund_reviewed_by", "manual_refund_required", "payment_link_expires"):
        op.drop_column("special_event_orders", field, schema="app")
    op.drop_column("special_events", "payment_link_expires", schema="app")
