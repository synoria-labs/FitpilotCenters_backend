"""Durable financial evidence, independent of the conversation's replaceable pending action."""
from datetime import datetime, timezone, date
from typing import Optional
from sqlalchemy import BigInteger, String, Text, TIMESTAMP, Date, JSON, ForeignKey, Numeric, Integer
from sqlalchemy.orm import Mapped, mapped_column
from app.db.postgresql import Base


class MembershipPurchaseOrder(Base):
    __tablename__ = "membership_purchase_orders"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    member_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("people.id"))
    action_type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    facts: Mapped[dict] = mapped_column(JSON, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    first_class_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    rule_version: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="awaiting_payment", nullable=False, index=True)
    external_reference: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    mp_preference_id: Mapped[Optional[str]] = mapped_column(String(120))
    mp_init_point: Mapped[Optional[str]] = mapped_column(Text)
    payment_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("payments.id"), unique=True)
    subscription_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("membership_subscriptions.id"))
    approved_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    confirmation_message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    review_reason: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))


class ChatbotTurnAudit(Base):
    __tablename__ = "chatbot_turn_audit"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    requested_model: Mapped[str] = mapped_column(String(80), nullable=False)
    effective_model: Mapped[Optional[str]] = mapped_column(String(100))
    rules_version: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    tools: Mapped[list] = mapped_column(JSON, default=list)
    validation: Mapped[str] = mapped_column(String(80), nullable=False)
    reply_kind: Mapped[Optional[str]] = mapped_column(String(40))
    order_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    usage: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))
