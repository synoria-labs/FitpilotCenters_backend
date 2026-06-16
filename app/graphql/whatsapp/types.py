"""GraphQL types for the WhatsApp chat feature.

Strawberry converts snake_case field names to camelCase in the schema
(e.g. ``text_content`` -> ``textContent``). The ``message_type``/``template_id``
fields are kept so media and template support can be added later without schema churn.
"""
from datetime import datetime
from typing import Optional

import strawberry

from app.crud.whatsappCrud import (
    ChatContactData,
    ChatMediaData,
    ChatMembershipData,
    ChatMessageData,
    ConversationData,
)


@strawberry.type
class ChatMembershipSummary:
    status: str
    remaining_days: Optional[int]

    @classmethod
    def from_data(cls, d: ChatMembershipData) -> "ChatMembershipSummary":
        return cls(
            status=d.status,
            remaining_days=d.remaining_days,
        )


@strawberry.type
class ChatContact:
    id: int
    wa_id: str
    phone_number: str
    name: Optional[str]
    profile_name: Optional[str]
    member_id: Optional[int]
    member_name: Optional[str]
    member_membership: Optional[ChatMembershipSummary]

    @classmethod
    def from_data(cls, d: ChatContactData) -> "ChatContact":
        return cls(
            id=d.id,
            wa_id=d.wa_id,
            phone_number=d.phone_number,
            name=d.name,
            profile_name=d.profile_name,
            member_id=d.member_id,
            member_name=d.member_name,
            member_membership=(
                ChatMembershipSummary.from_data(d.member_membership)
                if d.member_membership
                else None
            ),
        )


@strawberry.type
class ChatMessageMedia:
    """Attachment metadata of a chat message (one per message in practice)."""

    id: int
    media_type: str
    mime_type: Optional[str]
    filename: Optional[str]
    caption: Optional[str]
    file_size: Optional[int]
    media_url: Optional[str]
    downloaded: bool
    download_failed: bool

    @classmethod
    def from_data(cls, d: ChatMediaData) -> "ChatMessageMedia":
        return cls(
            id=d.id,
            media_type=d.media_type,
            mime_type=d.mime_type,
            filename=d.filename,
            caption=d.caption,
            file_size=d.file_size,
            media_url=d.media_url,
            downloaded=d.downloaded,
            download_failed=d.download_failed,
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
    context_message_id: Optional[str]  # reacted-to / referenced message wa id
    media_url: Optional[str]  # deprecated: kept for older clients, use ``media``
    media: Optional[ChatMessageMedia]

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
            context_message_id=d.context_message_id,
            media_url=d.media_url,
            media=ChatMessageMedia.from_data(d.media) if d.media else None,
        )


@strawberry.type
class ChatConversation:
    id: int
    status: str
    contact: ChatContact
    last_message: Optional[ChatMessage]
    last_activity: Optional[datetime]
    unread_count: int
    bot_enabled: bool = True

    @classmethod
    def from_data(cls, d: ConversationData) -> "ChatConversation":
        return cls(
            id=d.id,
            status=d.status,
            contact=ChatContact.from_data(d.contact),
            last_message=ChatMessage.from_data(d.last_message) if d.last_message else None,
            last_activity=d.last_activity,
            unread_count=d.unread_count,
            bot_enabled=getattr(d, "bot_enabled", True),
        )


@strawberry.input
class SendTextMessageInput:
    text: str
    conversation_id: Optional[int] = None
    wa_id: Optional[str] = None


@strawberry.input
class SendMediaMessageInput:
    conversation_id: Optional[int] = None
    wa_id: Optional[str] = None
    caption: Optional[str] = None
    voice_note: bool = False


@strawberry.input
class SendReactionInput:
    message_id: str  # wa_message_id of the target message
    emoji: str = ""  # empty string removes the reaction
    conversation_id: Optional[int] = None
    wa_id: Optional[str] = None


@strawberry.type
class SendMessageResult:
    success: bool = False
    message: Optional[ChatMessage] = None
    error: Optional[str] = None
