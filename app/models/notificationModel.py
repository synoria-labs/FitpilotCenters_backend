"""Notification configuration and audit models for FitPilot.

Two tables power the automated WhatsApp notification system:

* ``notification_settings`` — one row per business event (new registration, renewal
  reminder, renewal confirmation, expired membership). Each row stores which approved
  Meta template to use, how its ``{{1}}..{{n}}`` placeholders map to member variables,
  whether the event is enabled, an optional media URL for templates with media headers,
  and (for reminders) the day offsets before expiry.

* ``notification_log`` — an idempotency + audit ledger. Each send attempt claims a unique
  ``dedup_key`` *before* contacting Meta, so the same notification is never sent twice even
  if the daily sweep runs on multiple workers or is retried.

Unlike the externally-created WhatsApp chat tables (plain TIMESTAMP), these are FitPilot
native tables and use TIMESTAMPTZ like the rest of the domain. Primary keys are assigned
by the database sequence (never set ``id`` manually).
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, ForeignKey, String, Text, TIMESTAMP, Index, JSON
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.postgresql import Base


# Canonical event types. Keep in sync with EVENT_TYPES in
# app/services/notification_service.py (single source of truth for behaviour).
EVENT_NEW_REGISTRATION = "new_registration"
EVENT_RENEWAL_REMINDER = "renewal_reminder"
EVENT_RENEWAL_CONFIRMATION = "renewal_confirmation"
EVENT_MEMBERSHIP_EXPIRED = "membership_expired"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NotificationSetting(Base):
    """Per-event notification configuration (editable from the desktop frontend)."""

    __tablename__ = "notification_settings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # FK to app.whatsapp_templates.id; nullable until an admin assigns a template.
    template_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("whatsapp_templates.id", ondelete="SET NULL")
    )
    # Ordered list of variable keys for the template body placeholders, e.g.
    # ["member_first_name", "plan_name", "end_date"] maps {{1}}->name, {{2}}->plan, {{3}}->date.
    param_mapping: Mapped[Optional[list]] = mapped_column(JSON)
    # Public HTTPS URL used when the selected template has IMAGE/VIDEO/DOCUMENT header media.
    header_media_url: Mapped[Optional[str]] = mapped_column(String(1000))
    # Reminder day offsets before end_at, e.g. [7, 1]. Only used by renewal_reminder.
    offsets_days: Mapped[Optional[list]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        Index("uq_notification_settings_event", "event_type", unique=True),
    )


class NotificationLog(Base):
    """Idempotency + audit ledger for every notification dispatch attempt."""

    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    person_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("people.id", ondelete="CASCADE")
    )
    subscription_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    template_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    # Unique claim key: e.g. "renewal_reminder:{subscription_id}:{offset}" — claimed before
    # send so duplicates (multiple workers / retries) are impossible.
    dedup_key: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    wa_message_id: Mapped[Optional[str]] = mapped_column(String(120))
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        Index("uq_notification_log_dedup", "dedup_key", unique=True),
        Index("idx_notification_log_event_person", "event_type", "person_id"),
    )
