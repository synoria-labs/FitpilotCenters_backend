"""POS + caja: cash sessions, movements, sales, line items, tenders + capabilities.

Creates the FitPilot-native POS tables in the ``app`` schema:

* ``cash_sessions``   — shared cash register (caja); one open at a time.
* ``cash_movements``  — manual cash in/out against a caja.
* ``sales``           — POS ticket header.
* ``sale_line_items`` — what was sold (membership lines carry subscription_id/payment_id).
* ``sale_payments``   — tender ledger (drives the corte de caja).

Also seeds + grants the POS capabilities to admin (display) and staff.
Purely additive: no changes to existing tables, no data backfill.

Revision ID: a1b2c3d4e5f7
Revises: e7f8a9b0c1d2
Create Date: 2026-06-22 18:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a1b2c3d4e5f7"
down_revision: Union[str, None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"

POS_CAPABILITIES = ("operate_pos", "manage_cash_session", "view_pos_reports", "manage_products")


def upgrade() -> None:
    # ---------------------------------------------------------- cash_sessions
    op.create_table(
        "cash_sessions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("opened_by", sa.BigInteger(), nullable=False),
        sa.Column("opened_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("opening_float", sa.Numeric(12, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("closed_by", sa.BigInteger(), nullable=True),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False, server_default=sa.text("'open'")),
        sa.Column("expected_cash", sa.Numeric(12, 2), nullable=True),
        sa.Column("counted_cash", sa.Numeric(12, 2), nullable=True),
        sa.Column("difference", sa.Numeric(12, 2), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('open','closed')", name="ck_cash_session_status"),
        sa.ForeignKeyConstraint(["opened_by"], [f"{SCHEMA}.accounts.id"]),
        sa.ForeignKeyConstraint(["closed_by"], [f"{SCHEMA}.accounts.id"]),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_cash_session_single_open", "cash_sessions", ["status"],
        unique=True, schema=SCHEMA, postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index("idx_cash_session_opened_at", "cash_sessions", ["opened_at"], schema=SCHEMA)

    # --------------------------------------------------------- cash_movements
    op.create_table(
        "cash_movements",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("cash_session_id", sa.BigInteger(), nullable=False),
        sa.Column("direction", sa.String(length=3), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("direction IN ('in','out')", name="ck_cash_movement_direction"),
        sa.ForeignKeyConstraint(["cash_session_id"], [f"{SCHEMA}.cash_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], [f"{SCHEMA}.accounts.id"]),
        schema=SCHEMA,
    )
    op.create_index("idx_cash_movement_session", "cash_movements", ["cash_session_id"], schema=SCHEMA)

    # ------------------------------------------------------------------ sales
    op.create_table(
        "sales",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("person_id", sa.BigInteger(), nullable=True),
        sa.Column("cash_session_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'completed'")),
        sa.Column("subtotal", sa.Numeric(12, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("discount_total", sa.Numeric(12, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("tax_total", sa.Numeric(12, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("total", sa.Numeric(12, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("amount_paid", sa.Numeric(12, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("sold_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('open','completed','voided','refunded')", name="ck_sale_status"
        ),
        sa.ForeignKeyConstraint(["person_id"], [f"{SCHEMA}.people.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["cash_session_id"], [f"{SCHEMA}.cash_sessions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["sold_by"], [f"{SCHEMA}.accounts.id"]),
        schema=SCHEMA,
    )
    op.create_index("idx_sales_cash_session_status", "sales", ["cash_session_id", "status"], schema=SCHEMA)
    op.create_index("idx_sales_sold_by_created", "sales", ["sold_by", "created_at"], schema=SCHEMA)

    # -------------------------------------------------------- sale_line_items
    op.create_table(
        "sale_line_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("sale_id", sa.BigInteger(), nullable=False),
        sa.Column("line_type", sa.String(length=20), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("discount", sa.Numeric(12, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("line_total", sa.Numeric(12, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("plan_id", sa.BigInteger(), nullable=True),
        sa.Column("product_id", sa.BigInteger(), nullable=True),  # FK added in Phase 2
        sa.Column("subscription_id", sa.BigInteger(), nullable=True),
        sa.Column("payment_id", sa.BigInteger(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "line_type IN ('membership_new','membership_renewal','product','manual')",
            name="ck_sale_line_type",
        ),
        sa.ForeignKeyConstraint(["sale_id"], [f"{SCHEMA}.sales.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_id"], [f"{SCHEMA}.membership_plans.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["subscription_id"], [f"{SCHEMA}.membership_subscriptions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["payment_id"], [f"{SCHEMA}.payments.id"], ondelete="SET NULL"),
        schema=SCHEMA,
    )
    op.create_index("idx_sale_line_items_sale", "sale_line_items", ["sale_id"], schema=SCHEMA)

    # ----------------------------------------------------------- sale_payments
    op.create_table(
        "sale_payments",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("sale_id", sa.BigInteger(), nullable=False),
        sa.Column("payment_id", sa.BigInteger(), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("method", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["sale_id"], [f"{SCHEMA}.sales.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["payment_id"], [f"{SCHEMA}.payments.id"], ondelete="SET NULL"),
        schema=SCHEMA,
    )
    op.create_index("idx_sale_payment_sale", "sale_payments", ["sale_id"], schema=SCHEMA)
    op.create_index("idx_sale_payment_method", "sale_payments", ["method"], schema=SCHEMA)

    # ---------------------------------------------- capability seeds + grants
    for capability in POS_CAPABILITIES:
        op.execute(
            f"""
            INSERT INTO {SCHEMA}.role_capabilities (role_id, capability)
            SELECT id, '{capability}' FROM {SCHEMA}.roles WHERE code IN ('admin','staff')
            ON CONFLICT DO NOTHING
            """
        )


def downgrade() -> None:
    for capability in POS_CAPABILITIES:
        op.execute(f"DELETE FROM {SCHEMA}.role_capabilities WHERE capability = '{capability}'")

    op.drop_index("idx_sale_payment_method", table_name="sale_payments", schema=SCHEMA)
    op.drop_index("idx_sale_payment_sale", table_name="sale_payments", schema=SCHEMA)
    op.drop_table("sale_payments", schema=SCHEMA)

    op.drop_index("idx_sale_line_items_sale", table_name="sale_line_items", schema=SCHEMA)
    op.drop_table("sale_line_items", schema=SCHEMA)

    op.drop_index("idx_sales_sold_by_created", table_name="sales", schema=SCHEMA)
    op.drop_index("idx_sales_cash_session_status", table_name="sales", schema=SCHEMA)
    op.drop_table("sales", schema=SCHEMA)

    op.drop_index("idx_cash_movement_session", table_name="cash_movements", schema=SCHEMA)
    op.drop_table("cash_movements", schema=SCHEMA)

    op.drop_index("idx_cash_session_opened_at", table_name="cash_sessions", schema=SCHEMA)
    op.drop_index("uq_cash_session_single_open", table_name="cash_sessions", schema=SCHEMA)
    op.drop_table("cash_sessions", schema=SCHEMA)
