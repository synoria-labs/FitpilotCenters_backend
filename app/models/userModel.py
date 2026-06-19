"""
User authentication and identity models for FitPilot
Based on the modern schema with English naming
"""
from datetime import datetime
from typing import Optional, List, TYPE_CHECKING
from sqlalchemy import (
    DateTime, ForeignKey, Integer, BigInteger, String, Boolean, Index
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import TIMESTAMP

from app.db.postgresql import Base

if TYPE_CHECKING:
    from app.models.membershipsModel import MembershipSubscription, Payment
    from app.models.classModel import Reservation, StandingBooking, ClassTemplate, ClassSession


class People(Base):
    """Unified people table for all persons in the system"""

    __tablename__ = "people"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(200))
    phone_number: Mapped[Optional[str]] = mapped_column(String(32))
    email: Mapped[Optional[str]] = mapped_column(String(200))
    wa_id: Mapped[Optional[str]] = mapped_column(String(100))
    profile_picture_path: Mapped[Optional[str]] = mapped_column(String(255))
    profile_picture_uploaded_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    # Relationships
    roles: Mapped[List["PersonRole"]] = relationship(back_populates="person")
    accounts: Mapped[List["Account"]] = relationship(back_populates="person")
    subscriptions: Mapped[List["MembershipSubscription"]] = relationship(back_populates="person")
    payments: Mapped[List["Payment"]] = relationship(back_populates="person")
    reservations: Mapped[List["Reservation"]] = relationship(back_populates="person")
    standing_bookings: Mapped[List["StandingBooking"]] = relationship(back_populates="person")
    instructor_templates: Mapped[List["ClassTemplate"]] = relationship(
        back_populates="instructor",
        foreign_keys="ClassTemplate.instructor_id"
    )
    instructor_sessions: Mapped[List["ClassSession"]] = relationship(
        back_populates="instructor",
        foreign_keys="ClassSession.instructor_id"
    )

    # Indexes
    __table_args__ = (
        Index("idx_people_phone", "phone_number", postgresql_where="phone_number IS NOT NULL"),
        Index("idx_people_email", "email", postgresql_where="email IS NOT NULL"),
        Index("idx_people_wa_id", "wa_id", postgresql_where="wa_id IS NOT NULL"),
    )


class Role(Base):
    """System roles (member, instructor, staff, admin)"""

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)

    # Relationships
    person_roles: Mapped[List["PersonRole"]] = relationship(back_populates="role")


class PersonRole(Base):
    """Many-to-many relationship between people and roles"""

    __tablename__ = "person_roles"

    person_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("people.id"), primary_key=True)
    role_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("roles.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)

    # Relationships
    person: Mapped["People"] = relationship(back_populates="roles")
    role: Mapped["Role"] = relationship(back_populates="person_roles")


class RoleCapability(Base):
    """Capabilities granted to a role (capability-based authorization).

    A role can be granted named capabilities (e.g. ``manage_membership_plans``).
    The ``admin`` role is treated as an implicit super-user in code and does not
    depend on rows here, but it may be seeded for display purposes.
    """

    __tablename__ = "role_capabilities"

    role_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    capability: Mapped[str] = mapped_column(String(60), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow
    )

    # Relationships
    role: Mapped["Role"] = relationship()


class Account(Base):
    """Login accounts for system users"""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    person_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("people.id"), nullable=False)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    # Relationships
    person: Mapped["People"] = relationship(back_populates="accounts")