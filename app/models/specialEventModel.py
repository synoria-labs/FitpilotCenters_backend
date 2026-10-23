"""Special events: one-off paid classes with their own seat inventory.

A special event (e.g. "Halloween · 2.º aniversario") is deliberately NOT a ``class_sessions`` row
nor a membership plan. It sells its own seats, so a bike held by a regular member in the class
the event displaces can be sold for the event, and buying a ticket never creates, renews or
consumes a membership. The regular schedule is kept intact and traceable:

* ``special_events`` — the event itself (draft -> published -> completed, or canceled).
* ``special_event_seats`` — which bikes the event offers.
* ``special_event_orders`` — one row per participant: the seat hold while paying, the payment,
  and the resulting ticket. The hold is the row's ``awaiting_payment`` status plus
  ``hold_expires_at``; partial unique indexes make a seat and a participant count once per event.
* ``special_event_takeovers`` — an explicit, audited decision to give the event priority over a
  regular class: the class goes to status ``displaced`` (it keeps its id and history).
* ``special_event_displacements`` — the clients of a displaced class, for the manual follow-up
  (who was told, when, and how they were compensated). Their reservations go to ``displaced``:
  they are kept, but never count as attendance, absence or a consumed class.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    TIMESTAMP,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.postgresql import Base

# --- Event lifecycle -------------------------------------------------------------------------
EVENT_DRAFT = "draft"
EVENT_PUBLISHED = "published"
EVENT_CANCELED = "canceled"
EVENT_COMPLETED = "completed"
EVENT_STATUSES = (EVENT_DRAFT, EVENT_PUBLISHED, EVENT_CANCELED, EVENT_COMPLETED)

# --- Order (participant) lifecycle ----------------------------------------------------------
ORDER_AWAITING_PAYMENT = "awaiting_payment"  # seat held until hold_expires_at
ORDER_PAID = "paid"  # confirmed ticket
ORDER_EXPIRED = "expired"  # hold lapsed without an approved payment
ORDER_CANCELED = "canceled"  # voided before paying (or a non-refundable manual cancel)
ORDER_REFUND_PENDING = "refund_pending"  # money must go back (event canceled, staff cancel...)
ORDER_REFUNDED = "refunded"
ORDER_NEEDS_REVIEW = "needs_review"  # approved payment the system could not settle by itself
ORDER_STATUSES = (
    ORDER_AWAITING_PAYMENT,
    ORDER_PAID,
    ORDER_EXPIRED,
    ORDER_CANCELED,
    ORDER_REFUND_PENDING,
    ORDER_REFUNDED,
    ORDER_NEEDS_REVIEW,
)
# Statuses that occupy a seat/participant slot. Kept in sync with the partial unique indexes.
ORDER_ACTIVE_STATUSES = (ORDER_AWAITING_PAYMENT, ORDER_PAID)

ORDER_CHANNEL_WHATSAPP = "whatsapp"
ORDER_CHANNEL_ADMIN = "admin"

# --- Takeover / displacement ------------------------------------------------------------------
TAKEOVER_ACTIVE = "active"
TAKEOVER_RESTORED = "restored"

FOLLOW_UP_PENDING = "pending"
FOLLOW_UP_NOTIFIED = "notified"
FOLLOW_UP_RESOLVED = "resolved"
FOLLOW_UP_STATUSES = (FOLLOW_UP_PENDING, FOLLOW_UP_NOTIFIED, FOLLOW_UP_RESOLVED)

COMPENSATION_TYPES = (
    "make_up_class",  # reposición en otra clase
    "membership_extension",  # extensión de la membresía
    "event_access",  # acceso al evento como compensación
    "credit",  # saldo a favor
    "refund",  # devolución
    "not_needed",  # no requirió compensación
    "other",
)

RESTORATION_RESTORED = "restored"
RESTORATION_KEPT = "kept_displaced"

# Status a displaced class and its reservations take. Added to the CHECK constraints by the
# special-events migration; regular booking paths only accept 'scheduled' sessions.
SESSION_DISPLACED = "displaced"
RESERVATION_DISPLACED = "displaced"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SpecialEvent(Base):
    __tablename__ = "special_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    # What the ticket includes, one item per line (kit, snacks, DJ...).
    includes: Mapped[Optional[str]] = mapped_column(Text)
    # Address as it should be quoted to customers; falls back to the venue's when empty.
    address: Mapped[Optional[str]] = mapped_column(Text)
    venue_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("venues.id"), nullable=False)
    class_type_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("class_types.id"))
    instructor_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("people.id"))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="America/Mexico_City")
    start_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    # Nullable while drafting (duration may still be unknown); required to publish.
    end_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="MXN")
    # Nullable while drafting; required to publish.
    capacity: Mapped[Optional[int]] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=EVENT_DRAFT)
    # How long a seat stays held while the customer pays (also the payment link's expiry).
    hold_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    cancel_reason: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("accounts.id"))
    updated_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("accounts.id"))
    published_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("accounts.id"))
    published_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    canceled_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("accounts.id"))
    canceled_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=_utcnow)

    seats: Mapped[List["SpecialEventSeat"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )

    # The EXCLUDE constraints (no two live events on the same venue/instructor at once) are
    # created by the migration: they need btree_gist and are not expressible here.
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','published','canceled','completed')", name="ck_special_event_status"
        ),
        CheckConstraint("price >= 0", name="ck_special_event_price"),
        CheckConstraint("capacity IS NULL OR capacity > 0", name="ck_special_event_capacity"),
        CheckConstraint("end_at IS NULL OR end_at > start_at", name="ck_special_event_period"),
        CheckConstraint("hold_minutes BETWEEN 5 AND 240", name="ck_special_event_hold"),
        Index("idx_special_events_start", "start_at"),
    )


class SpecialEventSeat(Base):
    __tablename__ = "special_event_seats"

    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("special_events.id", ondelete="CASCADE"), primary_key=True
    )
    seat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("seats.id"), primary_key=True)

    event: Mapped["SpecialEvent"] = relationship(back_populates="seats")


class SpecialEventOrder(Base):
    __tablename__ = "special_event_orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("special_events.id"), nullable=False)
    # Participant. ``person_id`` links an existing person when the phone matches one; a buyer who
    # is not in the system is kept on the order only (no member/person row is created for them).
    person_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("people.id"))
    participant_name: Mapped[str] = mapped_column(String(200), nullable=False)
    participant_phone: Mapped[Optional[str]] = mapped_column(String(32))
    # Normalized phone (last 10 digits) used to stop the same participant buying twice.
    participant_key: Mapped[Optional[str]] = mapped_column(String(20))
    seat_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("seats.id"))
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default=ORDER_CHANNEL_WHATSAPP)
    # WhatsApp conversation to notify (no FK, like chatbot_pending_action).
    conversation_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    buyer_wa_id: Mapped[Optional[str]] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ORDER_AWAITING_PAYMENT)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="MXN")
    external_reference: Mapped[Optional[str]] = mapped_column(String(120), unique=True)
    mp_preference_id: Mapped[Optional[str]] = mapped_column(String(120))
    mp_init_point: Mapped[Optional[str]] = mapped_column(Text)
    hold_expires_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    mp_payment_id: Mapped[Optional[str]] = mapped_column(String(120))
    mp_status: Mapped[Optional[str]] = mapped_column(String(40))
    mp_status_detail: Mapped[Optional[str]] = mapped_column(String(120))
    payment_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("payments.id"))
    payment_method: Mapped[Optional[str]] = mapped_column(String(40))
    paid_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    canceled_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    cancel_reason: Mapped[Optional[str]] = mapped_column(Text)
    refunded_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    refund_reference: Mapped[Optional[str]] = mapped_column(String(120))
    review_reason: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("accounts.id"))
    updated_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("accounts.id"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        CheckConstraint(
            "status IN ('awaiting_payment','paid','expired','canceled','refund_pending',"
            "'refunded','needs_review')",
            name="ck_special_event_order_status",
        ),
        CheckConstraint("channel IN ('whatsapp','admin')", name="ck_special_event_order_channel"),
        CheckConstraint("amount >= 0", name="ck_special_event_order_amount"),
        # A bike and a participant count once per event while the order holds or owns it.
        Index(
            "uq_special_event_order_seat",
            "event_id",
            "seat_id",
            unique=True,
            postgresql_where="seat_id IS NOT NULL AND status IN ('awaiting_payment','paid')",
        ),
        Index(
            "uq_special_event_order_participant",
            "event_id",
            "participant_key",
            unique=True,
            postgresql_where="participant_key IS NOT NULL AND status IN ('awaiting_payment','paid')",
        ),
        Index(
            "uq_special_event_order_mp_payment",
            "mp_payment_id",
            unique=True,
            postgresql_where="mp_payment_id IS NOT NULL",
        ),
        Index("idx_special_event_orders_event_status", "event_id", "status"),
        Index(
            "idx_special_event_orders_holds",
            "hold_expires_at",
            postgresql_where="status = 'awaiting_payment'",
        ),
        Index("idx_special_event_orders_conversation", "conversation_id"),
    )


class SpecialEventTakeover(Base):
    __tablename__ = "special_event_takeovers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("special_events.id"), nullable=False)
    session_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("class_sessions.id"), nullable=False)
    previous_status: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=TAKEOVER_ACTIVE)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    applied_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("accounts.id"))
    applied_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=_utcnow)
    restored_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("accounts.id"))
    restored_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        CheckConstraint("status IN ('active','restored')", name="ck_special_event_takeover_status"),
        Index(
            "uq_special_event_takeover_active_session",
            "session_id",
            unique=True,
            postgresql_where="status = 'active'",
        ),
        Index("idx_special_event_takeovers_event", "event_id"),
    )


class SpecialEventDisplacement(Base):
    __tablename__ = "special_event_displacements"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("special_events.id"), nullable=False)
    takeover_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("special_event_takeovers.id"), nullable=False
    )
    session_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("class_sessions.id"), nullable=False)
    reservation_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("reservations.id", ondelete="SET NULL")
    )
    person_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("people.id"), nullable=False)
    seat_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("seats.id"))
    previous_reservation_status: Mapped[Optional[str]] = mapped_column(String(20))
    reservation_source: Mapped[Optional[str]] = mapped_column(String(20))
    subscription_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("membership_subscriptions.id", ondelete="SET NULL")
    )
    standing_booking_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("standing_bookings.id", ondelete="SET NULL")
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    follow_up_status: Mapped[str] = mapped_column(String(20), nullable=False, default=FOLLOW_UP_PENDING)
    notified_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    notified_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("accounts.id"))
    notification_channel: Mapped[Optional[str]] = mapped_column(String(30))
    notification_notes: Mapped[Optional[str]] = mapped_column(Text)
    compensation_type: Mapped[Optional[str]] = mapped_column(String(30))
    compensation_notes: Mapped[Optional[str]] = mapped_column(Text)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    resolved_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("accounts.id"))
    restoration_status: Mapped[Optional[str]] = mapped_column(String(20))
    restoration_note: Mapped[Optional[str]] = mapped_column(Text)
    restored_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        CheckConstraint(
            "follow_up_status IN ('pending','notified','resolved')",
            name="ck_special_event_displacement_follow_up",
        ),
        CheckConstraint(
            "compensation_type IS NULL OR compensation_type IN ('make_up_class',"
            "'membership_extension','event_access','credit','refund','not_needed','other')",
            name="ck_special_event_displacement_compensation",
        ),
        CheckConstraint(
            "restoration_status IS NULL OR restoration_status IN ('restored','kept_displaced')",
            name="ck_special_event_displacement_restoration",
        ),
        Index(
            "uq_special_event_displacement_person",
            "takeover_id",
            "person_id",
            unique=True,
        ),
        Index("idx_special_event_displacements_event", "event_id", "follow_up_status"),
    )
