"""Models for the owner/admin WhatsApp agent.

The customer-facing chatbot and the owner-facing admin agent intentionally keep
separate configuration, pending-action, audit, and task state. The owner agent is
allowed to inspect business data and execute operational commands only for
allowlisted WhatsApp senders.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    TIMESTAMP,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.postgresql import Base


OWNER_PENDING_STATUS_PENDING = "pending"
OWNER_PENDING_STATUS_CONFIRMED = "confirmed"
OWNER_PENDING_STATUS_CANCELED = "canceled"
OWNER_PENDING_STATUS_EXPIRED = "expired"

OWNER_TASK_STATUS_OPEN = "open"
OWNER_TASK_STATUS_DONE = "done"
OWNER_TASK_STATUS_CANCELED = "canceled"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OwnerAgentConfig(Base):
    """Single-row runtime configuration for the owner/admin agent."""

    __tablename__ = "owner_agent_config"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    require_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    model: Mapped[str] = mapped_column(String(80), nullable=False, default="claude-sonnet-4-6")
    system_prompt: Mapped[Optional[str]] = mapped_column(Text)
    history_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=1024)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )


class OwnerAgentAuthorizedPhone(Base):
    """A WhatsApp sender allowed to use the owner/admin agent."""

    __tablename__ = "owner_agent_authorized_phone"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(40), nullable=False)
    normalized_wa_id: Mapped[str] = mapped_column(String(30), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("accounts.id"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        Index("uq_owner_agent_authorized_phone_wa", "normalized_wa_id", unique=True),
        Index("idx_owner_agent_authorized_phone_enabled", "enabled"),
    )


class OwnerAgentPendingAction(Base):
    """A proposed admin action awaiting explicit confirmation."""

    __tablename__ = "owner_agent_pending_action"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    authorized_phone_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("owner_agent_authorized_phone.id", ondelete="SET NULL")
    )
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[Optional[dict]] = mapped_column(JSON)
    summary: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=OWNER_PENDING_STATUS_PENDING)
    expires_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        Index("uq_owner_agent_pending_conversation", "conversation_id", unique=True),
        Index("idx_owner_agent_pending_status", "status"),
    )


class OwnerAgentAuditLog(Base):
    """Audit trail for owner-agent tools and command outcomes."""

    __tablename__ = "owner_agent_audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    authorized_phone_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("owner_agent_authorized_phone.id", ondelete="SET NULL")
    )
    tool_name: Mapped[Optional[str]] = mapped_column(String(100))
    action_type: Mapped[Optional[str]] = mapped_column(String(50))
    payload: Mapped[Optional[dict]] = mapped_column(JSON)
    result_summary: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ok")
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        Index("idx_owner_agent_audit_phone_created", "authorized_phone_id", "created_at"),
        Index("idx_owner_agent_audit_conversation", "conversation_id"),
    )


class OwnerTask(Base):
    """Simple owner-managed task created from WhatsApp commands."""

    __tablename__ = "owner_tasks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=OWNER_TASK_STATUS_OPEN)
    due_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    created_by_phone_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("owner_agent_authorized_phone.id", ondelete="SET NULL")
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        Index("idx_owner_tasks_status_due", "status", "due_at"),
        Index("idx_owner_tasks_created_by", "created_by_phone_id"),
    )
