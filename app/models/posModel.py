"""Point-of-sale models for FitPilot.

Layers a POS on top of the existing ``payments`` table without changing it:

* ``cash_sessions``  — the shared cash register (caja): opened with a float,
  closed with a counted amount for the *corte de caja*. A partial unique index
  enforces at most one open caja at a time.
* ``cash_movements`` — manual cash in/out (retiros / ingresos) against a caja.
* ``sales``          — a POS ticket header (N line items, M tenders).
* ``sale_line_items``— what was sold; membership lines carry the subscription_id
  and payment_id produced by the reused enrollment/renewal CRUD.
* ``sale_payments``  — the tender ledger (how the ticket was paid); drives the
  corte de caja. ``payment_id`` is an optional back-reference, not unique, so a
  single sale can be settled with several tenders.
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, TYPE_CHECKING

from sqlalchemy import (
    ForeignKey, Integer, BigInteger, Numeric, String, Text, Boolean,
    CheckConstraint, Index, JSON,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import TIMESTAMP

from app.db.postgresql import Base

if TYPE_CHECKING:
    from app.models.userModel import People


class Product(Base):
    """A POS catalog product (water, supplements, day-pass, etc.) with optional stock."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    sku: Mapped[Optional[str]] = mapped_column(String(40))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    track_stock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stock_qty: Mapped[Optional[int]] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        Index("uq_products_sku", "sku", unique=True, postgresql_where="sku IS NOT NULL"),
        Index("idx_products_active", "is_active"),
    )


class CashSession(Base):
    """A cash register (caja) session: opened with a float, closed with a count."""

    __tablename__ = "cash_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    opened_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    opening_float: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    closed_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("accounts.id"))
    closed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="open")
    expected_cash: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    counted_cash: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    difference: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    movements: Mapped[List["CashMovement"]] = relationship(
        back_populates="cash_session", cascade="all, delete-orphan"
    )
    sales: Mapped[List["Sale"]] = relationship(back_populates="cash_session")

    __table_args__ = (
        CheckConstraint("status IN ('open','closed')", name="ck_cash_session_status"),
        # At most one open caja across the whole gym (shared register model).
        Index(
            "uq_cash_session_single_open",
            "status",
            unique=True,
            postgresql_where="status = 'open'",
        ),
        Index("idx_cash_session_opened_at", "opened_at"),
    )


class CashMovement(Base):
    """Manual cash movement (retiro / ingreso) against an open caja."""

    __tablename__ = "cash_movements"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cash_session_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cash_sessions.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[str] = mapped_column(String(3), nullable=False)  # 'in' | 'out'
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(200))
    created_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("accounts.id"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    cash_session: Mapped["CashSession"] = relationship(back_populates="movements")

    __table_args__ = (
        CheckConstraint("direction IN ('in','out')", name="ck_cash_movement_direction"),
        Index("idx_cash_movement_session", "cash_session_id"),
    )


class Sale(Base):
    """A POS ticket: one header, N line items, M tenders."""

    __tablename__ = "sales"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    person_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("people.id"))
    cash_session_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("cash_sessions.id"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="completed")
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    discount_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    tax_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    note: Mapped[Optional[str]] = mapped_column(Text)
    sold_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("accounts.id"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    completed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    person: Mapped[Optional["People"]] = relationship()
    cash_session: Mapped[Optional["CashSession"]] = relationship(back_populates="sales")
    line_items: Mapped[List["SaleLineItem"]] = relationship(
        back_populates="sale", cascade="all, delete-orphan"
    )
    sale_payments: Mapped[List["SalePayment"]] = relationship(
        back_populates="sale", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('open','completed','voided','refunded')", name="ck_sale_status"
        ),
        Index("idx_sales_cash_session_status", "cash_session_id", "status"),
        Index("idx_sales_sold_by_created", "sold_by", "created_at"),
    )


class SaleLineItem(Base):
    """A single line of a POS ticket (membership, product or manual charge)."""

    __tablename__ = "sale_line_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sale_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sales.id", ondelete="CASCADE"), nullable=False
    )
    # 'membership_new' | 'membership_renewal' | 'product' | 'manual'
    line_type: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    discount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    plan_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("membership_plans.id"))
    # product_id gains a FK to products in the Phase 2 migration.
    product_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    subscription_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("membership_subscriptions.id")
    )
    payment_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("payments.id"))
    meta: Mapped[Optional[dict]] = mapped_column(JSON)

    sale: Mapped["Sale"] = relationship(back_populates="line_items")

    __table_args__ = (
        CheckConstraint(
            "line_type IN ('membership_new','membership_renewal','product','manual')",
            name="ck_sale_line_type",
        ),
        Index("idx_sale_line_items_sale", "sale_id"),
    )


class SalePayment(Base):
    """Tender ledger row: how (part of) a ticket was paid. Drives the corte de caja."""

    __tablename__ = "sale_payments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sale_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sales.id", ondelete="CASCADE"), nullable=False
    )
    # Optional back-reference to a payments row (not unique: split tenders may
    # share the same membership anchor payment).
    payment_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("payments.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    method: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    sale: Mapped["Sale"] = relationship(back_populates="sale_payments")

    __table_args__ = (
        Index("idx_sale_payment_sale", "sale_id"),
        Index("idx_sale_payment_method", "method"),
    )
