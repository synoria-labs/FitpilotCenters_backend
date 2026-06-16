"""Add header-text / button-URL / location runtime params for the newer template components.

These let auto-notifications and campaigns supply runtime values for a TEXT-header variable, a
dynamic URL button, and a LOCATION header (the carousel per-card defaults live in the template's
``components`` JSON, so ``whatsapp_templates`` needs no change here).

Revision ID: a7b8c9d0e1f2
Revises: e6f7a8b9c0d1
Create Date: 2026-06-16 12:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"
_TABLES = ("notification_settings", "campaigns", "campaign_variants")


def upgrade() -> None:
    for table in _TABLES:
        op.execute(
            f"""
            ALTER TABLE {SCHEMA}.{table}
            ADD COLUMN IF NOT EXISTS header_text_param_key VARCHAR(60),
            ADD COLUMN IF NOT EXISTS button_url_param_key VARCHAR(60),
            ADD COLUMN IF NOT EXISTS location_param JSON
            """
        )


def downgrade() -> None:
    for table in _TABLES:
        op.execute(
            f"""
            ALTER TABLE {SCHEMA}.{table}
            DROP COLUMN IF EXISTS header_text_param_key,
            DROP COLUMN IF EXISTS button_url_param_key,
            DROP COLUMN IF EXISTS location_param
            """
        )
