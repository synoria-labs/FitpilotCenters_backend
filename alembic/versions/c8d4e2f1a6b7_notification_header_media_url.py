"""Add header media URL to notification settings.

Revision ID: c8d4e2f1a6b7
Revises: b3f1c9a2d7e4
Create Date: 2026-06-10 10:30:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "c8d4e2f1a6b7"
down_revision: Union[str, None] = "b3f1c9a2d7e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE {SCHEMA}.notification_settings
        ADD COLUMN IF NOT EXISTS header_media_url VARCHAR(1000)
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE {SCHEMA}.notification_settings
        DROP COLUMN IF EXISTS header_media_url
        """
    )
