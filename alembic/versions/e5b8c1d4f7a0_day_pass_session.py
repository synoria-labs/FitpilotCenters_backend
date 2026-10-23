"""Bind new day passes to a class; leave all historical subscriptions untouched."""
from alembic import op
import sqlalchemy as sa

revision = "e5b8c1d4f7a0"
down_revision = "d4a7b0c3e6f9"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("membership_subscriptions", sa.Column("session_id", sa.BigInteger(), nullable=True), schema="app")
    op.create_foreign_key("fk_subscription_session", "membership_subscriptions", "class_sessions", ["session_id"], ["id"], source_schema="app", referent_schema="app", ondelete="RESTRICT")
    op.create_index("ix_app_membership_subscriptions_session_id", "membership_subscriptions", ["session_id"], schema="app")


def downgrade():
    op.drop_index("ix_app_membership_subscriptions_session_id", table_name="membership_subscriptions", schema="app")
    op.drop_constraint("fk_subscription_session", "membership_subscriptions", schema="app", type_="foreignkey")
    op.drop_column("membership_subscriptions", "session_id", schema="app")
