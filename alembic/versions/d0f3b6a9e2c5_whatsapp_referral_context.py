"""Preserve inbound ad/post attribution for event discovery.

Revision ID: d0f3b6a9e2c5
Revises: c9f2a5d8e1b4
"""
from alembic import op
import sqlalchemy as sa

revision = "d0f3b6a9e2c5"
down_revision = "c9f2a5d8e1b4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("messages", sa.Column("referral", sa.JSON(), nullable=True), schema="app")


def downgrade():
    op.drop_column("messages", "referral", schema="app")
