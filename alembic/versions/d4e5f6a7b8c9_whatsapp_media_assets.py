"""WhatsApp media assets for template headers.

Revision ID: d4e5f6a7b8c9
Revises: c8d4e2f1a6b7
Create Date: 2026-06-11 03:45:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c8d4e2f1a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}.whatsapp_media_assets (
            id BIGSERIAL PRIMARY KEY,
            media_kind VARCHAR(20) NOT NULL,
            display_name VARCHAR(255) NOT NULL,
            original_filename VARCHAR(255) NOT NULL,
            mime_type VARCHAR(120) NOT NULL,
            file_ext VARCHAR(20) NOT NULL,
            file_size BIGINT NOT NULL,
            sha256 VARCHAR(64) NOT NULL,
            storage_key VARCHAR(500) NOT NULL,
            public_url VARCHAR(1000) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            sample_header_handle TEXT NULL,
            sample_handle_generated_at TIMESTAMP NULL,
            created_by_id BIGINT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT now(),
            updated_at TIMESTAMP NOT NULL DEFAULT now(),
            last_validated_at TIMESTAMP NULL
        )
        """
    )
    op.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_whatsapp_media_assets_storage_key
            ON {SCHEMA}.whatsapp_media_assets (storage_key)
        """
    )
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_whatsapp_media_assets_kind_status
            ON {SCHEMA}.whatsapp_media_assets (media_kind, status)
        """
    )
    op.execute(
        f"""
        ALTER TABLE {SCHEMA}.whatsapp_templates
        ADD COLUMN IF NOT EXISTS default_header_media_asset_id BIGINT
        """
    )
    op.execute(
        f"""
        ALTER TABLE {SCHEMA}.notification_settings
        ADD COLUMN IF NOT EXISTS header_media_asset_id BIGINT
        """
    )
    op.execute(
        f"""
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'fk_whatsapp_templates_default_header_media_asset'
              AND conrelid = '{SCHEMA}.whatsapp_templates'::regclass
          ) THEN
            ALTER TABLE {SCHEMA}.whatsapp_templates
              ADD CONSTRAINT fk_whatsapp_templates_default_header_media_asset
              FOREIGN KEY (default_header_media_asset_id)
              REFERENCES {SCHEMA}.whatsapp_media_assets(id)
              ON DELETE SET NULL;
          END IF;

          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'fk_notification_settings_header_media_asset'
              AND conrelid = '{SCHEMA}.notification_settings'::regclass
          ) THEN
            ALTER TABLE {SCHEMA}.notification_settings
              ADD CONSTRAINT fk_notification_settings_header_media_asset
              FOREIGN KEY (header_media_asset_id)
              REFERENCES {SCHEMA}.whatsapp_media_assets(id)
              ON DELETE SET NULL;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE {SCHEMA}.notification_settings
        DROP CONSTRAINT IF EXISTS fk_notification_settings_header_media_asset
        """
    )
    op.execute(
        f"""
        ALTER TABLE {SCHEMA}.notification_settings
        DROP COLUMN IF EXISTS header_media_asset_id
        """
    )
    op.execute(
        f"""
        ALTER TABLE {SCHEMA}.whatsapp_templates
        DROP CONSTRAINT IF EXISTS fk_whatsapp_templates_default_header_media_asset
        """
    )
    op.execute(
        f"""
        ALTER TABLE {SCHEMA}.whatsapp_templates
        DROP COLUMN IF EXISTS default_header_media_asset_id
        """
    )
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.idx_whatsapp_media_assets_kind_status")
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.uq_whatsapp_media_assets_storage_key")
    op.execute(f"DROP TABLE IF EXISTS {SCHEMA}.whatsapp_media_assets")
