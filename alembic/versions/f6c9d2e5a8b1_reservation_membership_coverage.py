"""Attribute new reservations to their membership without rewriting history.

Revision ID: f6c9d2e5a8b1
Revises: e5b8c1d4f7a0
"""
from alembic import op
import sqlalchemy as sa

revision = "f6c9d2e5a8b1"
down_revision = "e5b8c1d4f7a0"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("reservations", sa.Column("subscription_id", sa.BigInteger(), nullable=True), schema="app")
    op.create_foreign_key("fk_reservations_subscription", "reservations", "membership_subscriptions",
                          ["subscription_id"], ["id"], source_schema="app", referent_schema="app")
    op.create_index("idx_reservations_subscription", "reservations", ["subscription_id"], schema="app")


def downgrade():
    op.drop_index("idx_reservations_subscription", table_name="reservations", schema="app")
    op.drop_constraint("fk_reservations_subscription", "reservations", type_="foreignkey", schema="app")
    op.drop_column("reservations", "subscription_id", schema="app")
