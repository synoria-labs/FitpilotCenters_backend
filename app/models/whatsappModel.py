"""
WhatsApp messaging models for FitPilot.

These tables were originally created and populated by an external WhatsApp Cloud API
bot. FitPilot now owns the integration (inbound webhook + outbound send), so these
models are read/write.

NOTE on column types: unlike the FitPilot-native tables (which use TIMESTAMPTZ), these
externally-created tables use plain ``TIMESTAMP`` (no timezone). They are mapped as naive
datetimes to avoid timezone coercion mismatches. Primary keys are assigned by the database
sequence (never set ``id`` manually on insert).
"""
from datetime import datetime
from typing import Optional, List

from sqlalchemy import (
    BigInteger, SmallInteger, String, Text, ForeignKey, TIMESTAMP, JSON
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.postgresql import Base


class Contact(Base):
    """A WhatsApp contact (one per wa_id)."""

    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    wa_id: Mapped[str] = mapped_column(String(30), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(100))
    profile_name: Mapped[Optional[str]] = mapped_column(String(100))
    created_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, default=datetime.utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, default=datetime.utcnow)
    is_saved: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    # Relationships
    conversations: Mapped[List["Conversation"]] = relationship(back_populates="contact")


class Conversation(Base):
    """A conversation thread tied to a contact (24h customer-service window)."""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    contact_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("contacts.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    expiration_timestamp: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)
    created_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, default=datetime.utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, default=datetime.utcnow)

    # Relationships
    contact: Mapped["Contact"] = relationship(back_populates="conversations")
    messages: Mapped[List["Message"]] = relationship(back_populates="conversation")


class Message(Base):
    """An inbound or outbound WhatsApp message. NOTE: this table has no updated_at."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    wa_message_id: Mapped[Optional[str]] = mapped_column(String(100))
    conversation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("conversations.id"), nullable=False)
    contact_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("contacts.id"), nullable=False)
    direction: Mapped[str] = mapped_column(String(30), nullable=False)  # 'inbound' | 'outbound'
    message_type: Mapped[str] = mapped_column(String(30), nullable=False)  # 'text','image','audio',...
    text_content: Mapped[Optional[str]] = mapped_column(Text)
    template_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    context_message_id: Mapped[Optional[str]] = mapped_column(String(100))  # reply context
    timestamp: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    created_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, default=datetime.utcnow)
    is_processed: Mapped[Optional[int]] = mapped_column(SmallInteger, default=0)
    processed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)
    is_temp: Mapped[Optional[int]] = mapped_column(SmallInteger, default=0)

    # Relationships
    conversation: Mapped["Conversation"] = relationship(back_populates="messages")
    contact: Mapped["Contact"] = relationship()
    statuses: Mapped[List["MessageStatus"]] = relationship(back_populates="message")
    media: Mapped[List["Media"]] = relationship(back_populates="message")


class MessageStatus(Base):
    """Delivery status updates for an outbound message (sent/delivered/read/failed)."""

    __tablename__ = "message_statuses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("messages.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    created_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, default=datetime.utcnow)

    # Relationships
    message: Mapped["Message"] = relationship(back_populates="statuses")


class Media(Base):
    """Media attachment for a message (downloaded from the Cloud API to /uploads)."""

    __tablename__ = "media"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("messages.id"), nullable=False)
    media_type: Mapped[str] = mapped_column(String(30), nullable=False)
    mime_type: Mapped[Optional[str]] = mapped_column(String(100))
    sha256: Mapped[Optional[str]] = mapped_column(String(64))
    filename: Mapped[Optional[str]] = mapped_column(String(255))
    file_size: Mapped[Optional[int]] = mapped_column(BigInteger)
    media_url: Mapped[Optional[str]] = mapped_column(String(255))
    caption: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, default=datetime.utcnow)
    downloaded: Mapped[Optional[int]] = mapped_column(SmallInteger, default=0)
    download_time: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)
    download_failed: Mapped[Optional[int]] = mapped_column(SmallInteger, default=0)

    # Relationships
    message: Mapped["Message"] = relationship(back_populates="media")


class WebhookLog(Base):
    """Raw inbound webhook payloads (audit trail / debugging)."""

    __tablename__ = "webhook_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    x_request_id: Mapped[Optional[str]] = mapped_column(String(128))
    payload: Mapped[Optional[dict]] = mapped_column(JSON)
    processed: Mapped[Optional[int]] = mapped_column(SmallInteger, default=1)
    created_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, default=datetime.utcnow)


class WhatsAppTemplate(Base):
    """A Meta Cloud API message template (HSM), mirrored locally.

    The table pre-exists (created by the external bot) and stores the Meta template
    structure: ``template_name`` + ``template_language`` are immutable once created in
    Meta, ``template_status`` (APPROVED/PENDING/REJECTED) is dictated by Meta, and
    ``components`` is the Meta component array (BODY/HEADER/FOOTER/BUTTONS) with
    positional ``{{1}}`` placeholders. ``category`` and ``meta_template_id`` are added by
    FitPilot (see migrations/add_whatsapp_template_meta_fields.sql): ``category`` is required
    by Meta on create, and ``meta_template_id`` stores Meta's template id for edit/delete.
    """

    __tablename__ = "whatsapp_templates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    template_name: Mapped[str] = mapped_column(String(100), nullable=False)
    template_namespace: Mapped[str] = mapped_column(String(100), nullable=False)
    template_language: Mapped[str] = mapped_column(String(10), nullable=False)
    template_status: Mapped[str] = mapped_column(String(30), nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(30))
    meta_template_id: Mapped[Optional[str]] = mapped_column(String(64))
    components: Mapped[Optional[list]] = mapped_column(JSON)
    created_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, default=datetime.utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, default=datetime.utcnow)
