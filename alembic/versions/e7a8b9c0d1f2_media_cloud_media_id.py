"""Persist the Meta Cloud API media id on chat media rows.

Needed to retry downloads after a restart (the temporary download URL
expires in minutes, but the media id stays valid ~30 days).

Revision ID: e7a8b9c0d1f2
Revises: d4e5f6a7b8c9
Create Date: 2026-06-12 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "e7a8b9c0d1f2"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE {SCHEMA}.media
        ADD COLUMN IF NOT EXISTS cloud_media_id VARCHAR(100)
        """
    )
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_media_cloud_media_id
            ON {SCHEMA}.media (cloud_media_id)
        """
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.idx_media_cloud_media_id")
    op.execute(f"ALTER TABLE {SCHEMA}.media DROP COLUMN IF EXISTS cloud_media_id")
