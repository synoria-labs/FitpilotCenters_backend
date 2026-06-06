"""GraphQL types for WhatsApp message-template management.

The editor is simplified: callers send a BODY text (with positional ``{{1}}`` placeholders),
optional example values and an optional FOOTER; the backend assembles the Meta ``components``
array. The full ``components`` JSON is still exposed for read so the UI can render a preview.
"""
from datetime import datetime
from typing import List, Optional

import strawberry
from strawberry.scalars import JSON

from app.crud.whatsappTemplatesCrud import WhatsAppTemplateData


@strawberry.type
class WhatsAppTemplate:
    id: int
    template_name: str
    template_namespace: str
    template_language: str
    template_status: str
    category: Optional[str]
    meta_template_id: Optional[str]
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


@strawberry.input
class UpdateTemplateInput:
    id: int
    body_text: str
    body_examples: Optional[List[str]] = None
    footer_text: Optional[str] = None


@strawberry.input
class SendTemplateTestInput:
    phone: str
    template_id: int
    body_params: Optional[List[str]] = None
    header_media_url: Optional[str] = None
    header_media_id: Optional[str] = None


@strawberry.type
class TemplateResult:
    success: bool = False
    template: Optional[WhatsAppTemplate] = None
    error: Optional[str] = None
