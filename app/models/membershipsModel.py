"""
Membership and payment models for FitPilot
Based on the modern schema with English naming
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, TYPE_CHECKING
from sqlalchemy import (
    DateTime, ForeignKey, Integer, BigInteger, Numeric, String, Text, Boolean,
    CheckConstraint, Index
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import TIMESTAMP

from app.db.postgresql import Base

if TYPE_CHECKING:
    from app.models.userModel import People
    from app.models.classModel import StandingBooking


class MembershipPlan(Base):
    """Membership plan templates"""

    __tablename__ = "membership_plans"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    duration_value: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_unit: Mapped[str] = mapped_column(String(10), nullable=False)
    class_limit: Mapped[Optional[int]] = mapped_column(Integer)
    fixed_time_slot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_sessions_per_day: Mapped[Optional[int]] = mapped_column(Integer)
    max_sessions_per_week: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    # Relationships
    subscriptions: Mapped[List["MembershipSubscription"]] = relationship(back_populates="plan")

    __table_args__ = (
        CheckConstraint("duration_unit IN ('day','week','month')", name="ck_duration_unit"),
    )


class MembershipSubscription(Base):
    """Individual membership subscriptions"""

    __tablename__ = "membership_subscriptions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    person_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("people.id"), nullable=False)
    plan_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("membership_plans.id"), nullable=False)
    start_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    created_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("accounts.id"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    # Relationships
    person: Mapped["People"] = relationship(back_populates="subscriptions")
    plan: Mapped["MembershipPlan"] = relationship(back_populates="subscriptions")
    payments: Mapped[List["Payment"]] = relationship(back_populates="subscription")
    standing_bookings: Mapped[List["StandingBooking"]] = relationship(back_populates="subscription")

    __table_args__ = (
        CheckConstraint("status IN ('active','expired','canceled','pending')", name="ck_subscription_status"),
        Index("idx_subscriptions_person", "person_id", "status", "end_at"),
        Index("idx_subscriptions_active", "status", "end_at", postgresql_where="status = 'active'"),
    )


class Payment(Base):
    """Payment records"""

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    subscription_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("membership_subscriptions.id"))
    person_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("people.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    paid_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    method: Mapped[str] = mapped_column(String(40), nullable=False)
    provider: Mapped[Optional[str]] = mapped_column(String(40))
    provider_payment_id: Mapped[Optional[str]] = mapped_column(String(120))
    external_reference: Mapped[Optional[str]] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="COMPLETED")
    comment: Mapped[Optional[str]] = mapped_column(Text)
    recorded_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("accounts.id"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    # Relationships
    subscription: Mapped[Optional["MembershipSubscription"]] = relationship(back_populates="payments")
    person: Mapped["People"] = relationship(back_populates="payments")

    __table_args__ = (
        CheckConstraint(
            "status IN ('COMPLETED','PENDING','FAILED','REFUNDED')",
            name="ck_payments_status",
        ),
        Index("idx_payments_person_paidat", "person_id", "paid_at"),
        Index("idx_payments_subscription", "subscription_id", "paid_at"),
        Index("idx_payments_status_paidat", "status", "paid_at"),
        Index(
            "uq_payments_provider_payment_id",
            "provider_payment_id",
            unique=True,
            postgresql_where="provider_payment_id IS NOT NULL",
        ),
    )