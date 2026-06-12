"""Configuration for S3-compatible WhatsApp media storage."""
from __future__ import annotations

import os

from app.core.env import load_environment

load_environment()


class MediaStorageConfig:
    S3_ENDPOINT_URL: str = os.getenv("MEDIA_S3_ENDPOINT_URL", "")
    S3_BUCKET: str = os.getenv("MEDIA_S3_BUCKET", "")
    S3_REGION: str = os.getenv("MEDIA_S3_REGION", "auto")
    S3_ACCESS_KEY_ID: str = os.getenv("MEDIA_S3_ACCESS_KEY_ID", "")
    S3_SECRET_ACCESS_KEY: str = os.getenv("MEDIA_S3_SECRET_ACCESS_KEY", "")
    PUBLIC_BASE_URL: str = os.getenv("MEDIA_PUBLIC_BASE_URL", "")
    CACHE_CONTROL: str = os.getenv(
        "MEDIA_CACHE_CONTROL", "public, max-age=31536000, immutable"
    )

    @classmethod
    def is_configured(cls) -> bool:
        return bool(
            cls.S3_ENDPOINT_URL
            and cls.S3_BUCKET
            and cls.S3_ACCESS_KEY_ID
            and cls.S3_SECRET_ACCESS_KEY
            and cls.PUBLIC_BASE_URL
        )


media_storage_config = MediaStorageConfig()
