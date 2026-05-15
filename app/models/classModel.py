"""
Class scheduling and reservation models for FitPilot
Based on the modern schema with English naming
"""
from datetime import datetime, date, time
from typing import Optional, List, TYPE_CHECKING
from sqlalchemy import (
    Date, Time, ForeignKey, Integer, BigInteger, String, Text,
    Boolean, CheckConstraint, UniqueConstraint, Index
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import TIMESTAMP

from app.db.postgresql import Base

if TYPE_CHECKING:
    from app.models.userModel import People
    from app.models.venueModel import Venue, Seat
    from app.models.membershipsModel import MembershipSubscription


class ClassType(Base):
    """Types of classes offered"""

    __tablename__ = "class_types"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)

    # Relationships
    class_templates: Mapped[List["ClassTemplate"]] = relationship(back_populates="class_type")
    class_sessions: Mapped[List["ClassSession"]] = relationship(back_populates="class_type")


class ClassTemplate(Base):
    """Recurring class schedules"""

    __tablename__ = "class_templates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    class_type_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("class_types.id"), nullable=False)
    venue_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("venues.id"), nullable=False)
    default_capacity: Mapped[Optional[int]] = mapped_column(Integer)
    default_duration_min: Mapped[int] = mapped_column(Integer, nullable=False)
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)  # 0=Sunday, 6=Saturday
    start_time_local: Mapped[time] = mapped_column(Time, nullable=False)
    instructor_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("people.id"))
    name: Mapped[Optional[str]] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)

    # Relationships
    class_type: Mapped["ClassType"] = relationship(back_populates="class_templates")
    venue: Mapped["Venue"] = relationship(back_populates="class_templates")
    instructor: Mapped[Optional["People"]] = relationship(
        "People",
        back_populates="instructor_templates",
        foreign_keys=[instructor_id]
    )
    class_sessions: Mapped[List["ClassSession"]] = relationship(back_populates="template")
    standing_bookings: Mapped[List["StandingBooking"]] = relationship(back_populates="template")

    __table_args__ = (
        CheckConstraint("default_duration_min BETWEEN 15 AND 240", name="ck_duration_range"),
        CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_weekday_range"),
    )


class ClassSession(Base):
    """Individual class instances"""

    __tablename__ = "class_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    class_type_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("class_types.id"), nullable=False)
    venue_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("venues.id"), nullable=False)
    template_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("class_templates.id"))
    instructor_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("people.id"))
    name: Mapped[Optional[str]] = mapped_column(String(120))
    start_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="scheduled")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)

    # Relationships
    class_type: Mapped["ClassType"] = relationship(back_populates="class_sessions")
    venue: Mapped["Venue"] = relationship(back_populates="class_sessions")
    template: Mapped[Optional["ClassTemplate"]] = relationship(back_populates="class_sessions")
    instructor: Mapped[Optional["People"]] = relationship(
        "People",
        back_populates="instructor_sessions",
        foreign_keys=[instructor_id]
    )
    reservations: Mapped[List["Reservation"]] = relationship(back_populates="session")

    __table_args__ = (
        CheckConstraint("capacity > 0", name="ck_session_capacity"),
        CheckConstraint("status IN ('scheduled','canceled','completed')", name="ck_session_status"),
        Index("idx_sessions_time", "start_at", "venue_id"),
        Index("idx_sessions_instructor", "instructor_id", "start_at"),
        Index("idx_sessions_template", "template_id", "start_at"),
    )


class Reservation(Base):
    """Individual class reservations"""

    __tablename__ = "reservations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("class_sessions.id", ondelete="CASCADE"), nullable=False)
    person_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("people.id"), nullable=False)
    seat_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("seats.id"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="reserved")
    reserved_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)
    checkin_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    checkout_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    waitlist_position: Mapped[Optional[int]] = mapped_column(Integer)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120))
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")

    # Relationships
    session: Mapped["ClassSession"] = relationship(back_populates="reservations")
    person: Mapped["People"] = relationship(back_populates="reservations")
    seat: Mapped[Optional["Seat"]] = relationship(back_populates="reservations")

    __table_args__ = (
        UniqueConstraint("session_id", "person_id", name="uq_session_person"),
        CheckConstraint("status IN ('reserved','waitlisted','canceled','checked_in','no_show')", name="ck_reservation_status"),
        CheckConstraint("source IN ('manual','standing','override')", name="ck_reservation_source"),
        Index("uq_reservations_seat_once", "session_id", "seat_id", unique=True,
              postgresql_where="seat_id IS NOT NULL AND status IN ('reserved','checked_in')"),
        Index("idx_reservations_person", "person_id", "reserved_at"),
        Index("idx_reservations_session", "session_id", "status"),
    )


class StandingBooking(Base):
    """Recurring reservations for fixed schedules"""

    __tablename__ = "standing_bookings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    person_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("people.id"), nullable=False)
    subscription_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("membership_subscriptions.id", ondelete="CASCADE"), nullable=False)
    template_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("class_templates.id"), nullable=False)
    seat_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("seats.id"))
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)

    # Relationships
    person: Mapped["People"] = relationship(back_populates="standing_bookings")
    subscription: Mapped["MembershipSubscription"] = relationship(back_populates="standing_bookings")
    template: Mapped["ClassTemplate"] = relationship(back_populates="standing_bookings")
    exceptions: Mapped[List["StandingBookingException"]] = relationship(back_populates="standing_booking")

    __table_args__ = (
        CheckConstraint("status IN ('active','paused','canceled')", name="ck_standing_booking_status"),
    )


class StandingBookingException(Base):
    """Exceptions to standing bookings (skip/reschedule)"""

    __tablename__ = "standing_booking_exceptions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    standing_booking_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("standing_bookings.id", ondelete="CASCADE"), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    new_session_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("class_sessions.id"))
    new_seat_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("seats.id"))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)

    # Relationships
    standing_booking: Mapped["StandingBooking"] = relationship(back_populates="exceptions")

    __table_args__ = (
        UniqueConstraint("standing_booking_id", "session_date", name="uq_standing_booking_date"),
        CheckConstraint("action IN ('skip','reschedule')", name="ck_exception_action"),
    )
