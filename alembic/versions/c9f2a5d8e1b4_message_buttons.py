"""Store the buttons sent with an interactive WhatsApp message (e.g. the «Pagar ahora» link).

Revision ID: c9f2a5d8e1b4
Revises: c9f2a5b8d1e4
"""
from alembic import op
import sqlalchemy as sa

revision = "c9f2a5d8e1b4"
down_revision = "c9f2a5b8d1e4"
branch_labels = None
depends_on = None


def upgrade():
    # Nullable and without a default: a metadata-only change, instant even on a large table.
    op.add_column("messages", sa.Column("buttons", sa.JSON(), nullable=True), schema="app")


def downgrade():
    op.drop_column("messages", "buttons", schema="app")
