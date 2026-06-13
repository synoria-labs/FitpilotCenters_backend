"""Models for the customer-facing WhatsApp chatbot agent.

Two FitPilot-native tables in the ``app`` schema:

* ``chatbot_config`` — a single-row configuration the business edits from the desktop
  frontend: the agent's system prompt, business info (operating hours, address, policies,
  tone), the toggles (``enabled``, ``require_confirmation``) and the model id. Read on every
  inbound turn so changes apply without a redeploy.

* ``chatbot_pending_action`` — the propose-and-confirm ledger. When the agent proposes a
  write (reservation / payment / renewal / enrollment), it stores the validated payload here
  with ``status='pending'`` and asks the customer to confirm. The next affirmative turn
  executes it; anything else cancels it. One pending action per conversation (unique
  ``conversation_id``), with an ``expires_at`` so stale proposals never execute.

Like the other FitPilot-native tables (notifications), these use TIMESTAMPTZ and let the DB
sequence assign the primary key.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, ForeignKey, String, Text, TIMESTAMP, Index, JSON
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.postgresql import Base


# Pending action lifecycle.
PENDING_STATUS_PENDING = "pending"
PENDING_STATUS_CONFIRMED = "confirmed"
PENDING_STATUS_CANCELED = "canceled"
PENDING_STATUS_EXPIRED = "expired"

# Supported propose/confirm action types (payload shape documented in chatbot/tools.py).
ACTION_CREATE_RESERVATION = "create_reservation"
ACTION_CREATE_PAYMENT = "create_payment"
ACTION_RENEW_SUBSCRIPTION = "renew_subscription"
ACTION_CREATE_ENROLLMENT = "create_enrollment"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ChatbotConfig(Base):
    """Single-row chatbot configuration, editable from the desktop frontend."""

    __tablename__ = "chatbot_config"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Operational toggle (the env CHATBOT_ENABLED is the deploy-level kill-switch).
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # When true, the agent must never execute a write without an explicit confirmation turn.
    require_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Anthropic model id (defaults to the latest Sonnet).
    model: Mapped[str] = mapped_column(String(80), nullable=False, default="claude-sonnet-4-6")
    # The configurable system prompt (initial context + behaviour for the business).
    system_prompt: Mapped[Optional[str]] = mapped_column(Text)
    # Business info surfaced to the agent (and to the get_business_info tool).
    business_name: Mapped[Optional[str]] = mapped_column(String(200))
    address: Mapped[Optional[str]] = mapped_column(String(300))
    operating_hours: Mapped[Optional[str]] = mapped_column(Text)
    phone: Mapped[Optional[str]] = mapped_column(String(40))
    policies: Mapped[Optional[str]] = mapped_column(Text)
    tone: Mapped[Optional[str]] = mapped_column(String(200))
    extra_info: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )


class ChatbotPendingAction(Base):
    """A proposed write awaiting explicit confirmation in the conversation."""

    __tablename__ = "chatbot_pending_action"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[Optional[dict]] = mapped_column(JSON)
    # Resolved member id this action acts on (None for prospect enrollment).
    member_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    # Human-readable summary echoed to the customer when asking for confirmation.
    summary: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PENDING_STATUS_PENDING
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        # At most one pending action per conversation.
        Index("uq_chatbot_pending_conversation", "conversation_id", unique=True),
    )
