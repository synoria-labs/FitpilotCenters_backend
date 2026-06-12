"""GraphQL types for WhatsApp message-template management.

The editor is simplified: callers send a BODY text (with positional ``{{1}}`` placeholders),
optional example values and an optional FOOTER; the backend assembles the Meta ``components``
array. The full ``components`` JSON is still exposed for read so the UI can render a preview.
"""
from datetime import datetime
from enum import Enum
from typing import List, Optional

import strawberry
from strawberry.scalars import JSON

from app.crud.whatsappMediaAssetsCrud import WhatsAppMediaAssetData
from app.crud.whatsappTemplatesCrud import WhatsAppTemplateData


@strawberry.enum
class WhatsAppMediaKind(Enum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"


@strawberry.type
class WhatsAppMediaAsset:
    id: int
    media_kind: str
    display_name: str
    original_filename: str
    mime_type: str
    file_ext: str
    file_size: int
    sha256: str
    storage_key: str
    public_url: str
    status: str
    sample_header_handle: Optional[str]
    sample_handle_generated_at: Optional[datetime]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    last_validated_at: Optional[datetime]

    @classmethod
    def from_data(cls, d: WhatsAppMediaAssetData) -> "WhatsAppMediaAsset":
        return cls(
            id=d.id,
            media_kind=d.media_kind,
            display_name=d.display_name,
            original_filename=d.original_filename,
            mime_type=d.mime_type,
            file_ext=d.file_ext,
            file_size=d.file_size,
            sha256=d.sha256,
            storage_key=d.storage_key,
            public_url=d.public_url,
            status=d.status,
            sample_header_handle=d.sample_header_handle,
            sample_handle_generated_at=d.sample_handle_generated_at,
            created_at=d.created_at,
            updated_at=d.updated_at,
            last_validated_at=d.last_validated_at,
        )


@strawberry.type
class WhatsAppTemplate:
    id: int
    template_name: str
    template_namespace: str
    template_language: str
    template_status: str
    category: Optional[str]
    meta_template_id: Optional[str]
    default_header_media_asset_id: Optional[int]
    components: Optional[JSON]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    @classmethod
    def from_data(cls, d: WhatsAppTemplateData) -> "WhatsAppTemplate":
        return cls(
            id=d.id,
            template_name=d.template_name,
            template_namespace=d.template_namespace,
            template_language=d.template_language,
            template_status=d.template_status,
            category=d.category,
            meta_template_id=d.meta_template_id,
            default_header_media_asset_id=d.default_header_media_asset_id,
            components=d.components,
            created_at=d.created_at,
            updated_at=d.updated_at,
        )


@strawberry.input
class CreateTemplateInput:
    name: str
    language: str
    category: str  # AUTHENTICATION | MARKETING | UTILITY
    body_text: str
    body_examples: Optional[List[str]] = None
    footer_text: Optional[str] = None
    header_format: Optional[str] = None
    header_media_asset_id: Optional[int] = None


@strawberry.input
class UpdateTemplateInput:
    id: int
    body_text: str
    body_examples: Optional[List[str]] = None
    footer_text: Optional[str] = None
    header_media_asset_id: Optional[int] = None


@strawberry.input
class SendTemplateTestInput:
    phone: str
    template_id: int
    body_params: Optional[List[str]] = None
    header_media_url: Optional[str] = None
    header_media_id: Optional[str] = None
    header_media_asset_id: Optional[int] = None


@strawberry.type
class TemplateResult:
    success: bool = False
    template: Optional[WhatsAppTemplate] = None
    error: Optional[str] = None
