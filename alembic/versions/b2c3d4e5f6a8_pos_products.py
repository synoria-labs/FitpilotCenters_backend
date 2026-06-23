"""POS products / inventory.

* Creates ``products`` (catalog with optional stock).
* Adds the FK ``sale_line_items.product_id -> products.id``.
* Makes ``payments.person_id`` nullable so the POS can sell products to a
  walk-in customer with no member record.

Revision ID: b2c3d4e5f6a8
Revises: a1b2c3d4e5f7
Create Date: 2026-06-22 18:30:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b2c3d4e5f6a8"
down_revision: Union[str, None] = "a1b2c3d4e5f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("sku", sa.String(length=40), nullable=True),
        sa.Column("price", sa.Numeric(12, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("track_stock", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("stock_qty", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_products_sku", "products", ["sku"], unique=True, schema=SCHEMA,
        postgresql_where=sa.text("sku IS NOT NULL"),
    )
    op.create_index("idx_products_active", "products", ["is_active"], schema=SCHEMA)

    op.create_foreign_key(
        "fk_sale_line_items_product",
        "sale_line_items",
        "products",
        ["product_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="SET NULL",
    )

    op.alter_column(
        "payments", "person_id",
        existing_type=sa.BigInteger(),
        nullable=True,
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.alter_column(
        "payments", "person_id",
        existing_type=sa.BigInteger(),
        nullable=False,
        schema=SCHEMA,
    )
    op.drop_constraint("fk_sale_line_items_product", "sale_line_items", schema=SCHEMA, type_="foreignkey")
    op.drop_index("idx_products_active", table_name="products", schema=SCHEMA)
    op.drop_index("uq_products_sku", table_name="products", schema=SCHEMA)
    op.drop_table("products", schema=SCHEMA)
