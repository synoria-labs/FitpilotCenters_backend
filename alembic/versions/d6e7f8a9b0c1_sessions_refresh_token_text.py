"""Store session refresh tokens as text.

Revision ID: d6e7f8a9b0c1
Revises: c3d4e5f6a7b8
Create Date: 2026-06-22 15:20:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d6e7f8a9b0c1"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"


def upgrade() -> None:
    op.alter_column(
        "sessions",
        "refresh_token",
        schema=SCHEMA,
        existing_type=sa.String(length=255),
        type_=sa.Text(),
        existing_nullable=False,
    )
    op.alter_column(
        "sessions",
        "user_agent",
        schema=SCHEMA,
        existing_type=sa.String(length=255),
        type_=sa.Text(),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "sessions",
        "user_agent",
        schema=SCHEMA,
        existing_type=sa.Text(),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
    op.alter_column(
        "sessions",
        "refresh_token",
        schema=SCHEMA,
        existing_type=sa.Text(),
        type_=sa.String(length=255),
        existing_nullable=False,
    )
