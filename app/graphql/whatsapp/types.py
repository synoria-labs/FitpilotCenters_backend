"""GraphQL types for the WhatsApp chat feature.

Strawberry converts snake_case field names to camelCase in the schema
(e.g. ``text_content`` -> ``textContent``). The ``message_type``/``template_id``
fields are kept so media and template support can be added later without schema churn.
"""
from datetime import datetime
from typing import Optional

import strawberry

from app.crud.whatsappCrud import ChatContactData, ChatMessageData, ConversationData


@strawberry.type
class ChatContact:
    id: int
    wa_id: str
    phone_number: str
    name: Optional[str]
    profile_name: Optional[str]

    @classmethod
    def from_data(cls, d: ChatContactData) -> "ChatContact":
        return cls(
            id=d.id,
            wa_id=d.wa_id,
            phone_number=d.phone_number,
            name=d.name,
            profile_name=d.profile_name,
        )


@strawberry.type
class ChatMessage:
    id: int
    conversation_id: int
    contact_id: int
    direction: str
    message_type: str
    text_content: Optional[str]
    timestamp: datetime
    wa_message_id: Optional[str]
    media_url: Optional[str]

    @classmethod
    def from_data(cls, d: ChatMessageData) -> "ChatMessage":
        return cls(
            id=d.id,
            conversation_id=d.conversation_id,
            contact_id=d.contact_id,
            direction=d.direction,
            message_type=d.message_type,
            text_content=d.text_content,
            timestamp=d.timestamp,
            wa_message_id=d.wa_message_id,
            media_url=d.media_url,
        )


@strawberry.type
class ChatConversation:
    id: int
    status: str
    contact: ChatContact
    last_message: Optional[ChatMessage]
    last_activity: Optional[datetime]
    unread_count: int

    @classmethod
    def from_data(cls, d: ConversationData) -> "ChatConversation":
        return cls(
            id=d.id,
            status=d.status,
            contact=ChatContact.from_data(d.contact),
            last_message=ChatMessage.from_data(d.last_message) if d.last_message else None,
            last_activity=d.last_activity,
            unread_count=d.unread_count,
        )


@strawberry.input
class SendTextMessageInput:
    text: str
    conversation_id: Optional[int] = None
    wa_id: Optional[str] = None


@strawberry.type
class SendMessageResult:
    success: bool = False
    message: Optional[ChatMessage] = None
    error: Optional[str] = None
