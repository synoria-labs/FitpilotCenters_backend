"""Configure independent AI models and move the previous Sonnet default to 5.5.

Revision ID: a7d0e3f6b9c2
Revises: f6c9d2e5a8b1
"""
from alembic import op
import sqlalchemy as sa

revision = "a7d0e3f6b9c2"
down_revision = "f6c9d2e5a8b1"
branch_labels = None
depends_on = None


def upgrade():
    for field in ("template_ai_model", "prompt_optimizer_model"):
        op.add_column("chatbot_config", sa.Column(field, sa.String(80), nullable=False,
                      server_default="claude-sonnet-5-5"), schema="app")
    for table in ("chatbot_config", "owner_agent_config"):
        op.alter_column(table, "model", server_default="claude-sonnet-5-5", schema="app")
        # Preserve explicitly chosen alternatives; upgrade the existing deployment's default.
        op.execute(sa.text(f"UPDATE app.{table} SET model = 'claude-sonnet-5-5' "
                           "WHERE model = 'claude-sonnet-4-6' OR trim(model) = ''"))


def downgrade():
    for table in ("chatbot_config", "owner_agent_config"):
        op.alter_column(table, "model", server_default="claude-sonnet-4-6", schema="app")
    for field in ("prompt_optimizer_model", "template_ai_model"):
        op.drop_column("chatbot_config", field, schema="app")
    # Do not overwrite model choices made after the upgrade.
