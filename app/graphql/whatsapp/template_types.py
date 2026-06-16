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


@strawberry.enum
class TemplateAiAction(Enum):
    DRAFT = "DRAFT"
    OPTIMIZE = "OPTIMIZE"
    CORRECT = "CORRECT"


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
class TemplateButtonInput:
    type: str  # QUICK_REPLY | URL | PHONE_NUMBER
    text: str
    url: Optional[str] = None
    phone_number: Optional[str] = None
    example: Optional[str] = None  # sample suffix for a dynamic URL ({{1}}) button


@strawberry.input
class TemplateCarouselCardInput:
    header_format: str  # IMAGE | VIDEO
    header_media_asset_id: int
    body_text: str
    body_examples: Optional[List[str]] = None
    buttons: Optional[List[TemplateButtonInput]] = None


@strawberry.input
class LocationInput:
    latitude: str
    longitude: str
    name: Optional[str] = None
    address: Optional[str] = None


@strawberry.input
class CarouselCardSendInput:
    media_asset_id: Optional[int] = None
    media_url: Optional[str] = None
    media_id: Optional[str] = None
    body_params: Optional[List[str]] = None
    button_url_param: Optional[str] = None


@strawberry.input
class CreateTemplateInput:
    name: str
    language: str
    category: str  # AUTHENTICATION | MARKETING | UTILITY
    body_text: str
    body_examples: Optional[List[str]] = None
    footer_text: Optional[str] = None
    header_format: Optional[str] = None  # IMAGE | VIDEO | DOCUMENT | TEXT | LOCATION
    header_media_asset_id: Optional[int] = None
    header_text: Optional[str] = None
    header_text_example: Optional[str] = None
    buttons: Optional[List[TemplateButtonInput]] = None
    carousel_cards: Optional[List[TemplateCarouselCardInput]] = None


@strawberry.input
class UpdateTemplateInput:
    id: int
    body_text: str
    body_examples: Optional[List[str]] = None
    footer_text: Optional[str] = None
    header_media_asset_id: Optional[int] = None
    header_text: Optional[str] = None
    header_text_example: Optional[str] = None
    buttons: Optional[List[TemplateButtonInput]] = None


@strawberry.input
class SendTemplateTestInput:
    phone: str
    template_id: int
    body_params: Optional[List[str]] = None
    header_media_url: Optional[str] = None
    header_media_id: Optional[str] = None
    header_media_asset_id: Optional[int] = None
    header_text_param: Optional[str] = None
    button_url_param: Optional[str] = None
    location: Optional[LocationInput] = None
    carousel_card_overrides: Optional[List[CarouselCardSendInput]] = None


@strawberry.input
class AssistWhatsappTemplateInput:
    action: TemplateAiAction
    body_text: str = ""
    body_examples: Optional[List[str]] = None
    footer_text: Optional[str] = None
    template_name: Optional[str] = None
    category: Optional[str] = None
    language: Optional[str] = None
    instruction: Optional[str] = None


@strawberry.type
class TemplateAiSuggestion:
    body_text: str
    body_examples: List[str]
    footer_text: Optional[str]
    suggested_name: Optional[str]
    suggested_category: Optional[str]
    notes: List[str]
    warnings: List[str]

    @classmethod
    def from_data(cls, data) -> "TemplateAiSuggestion":
        return cls(
            body_text=data.body_text,
            body_examples=list(data.body_examples or []),
            footer_text=data.footer_text,
            suggested_name=data.suggested_name,
            suggested_category=data.suggested_category,
            notes=list(data.notes or []),
            warnings=list(data.warnings or []),
        )


@strawberry.type
class TemplateResult:
    success: bool = False
    template: Optional[WhatsAppTemplate] = None
    error: Optional[str] = None


@strawberry.type
class AssistWhatsappTemplateResult:
    success: bool = False
    suggestion: Optional[TemplateAiSuggestion] = None
    error: Optional[str] = None
