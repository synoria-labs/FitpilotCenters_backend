"""Product catalog + inventory CRUD."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.posModel import Product

_UNSET = object()


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


async def get_products(
    db: AsyncSession,
    *,
    include_inactive: bool = False,
    search: Optional[str] = None,
) -> List[Product]:
    stmt = select(Product)
    if not include_inactive:
        stmt = stmt.where(Product.is_active.is_(True))
    if search:
        term = f"%{search}%"
        stmt = stmt.where(or_(Product.name.ilike(term), Product.sku.ilike(term)))
    stmt = stmt.order_by(Product.name.asc())
    return list((await db.execute(stmt)).scalars().all())


async def get_product_by_id(db: AsyncSession, product_id: int) -> Optional[Product]:
    return await db.get(Product, product_id)


async def create_product(
    db: AsyncSession,
    *,
    name: str,
    price: Decimal | float,
    sku: Optional[str] = None,
    track_stock: bool = False,
    stock_qty: Optional[int] = None,
    is_active: bool = True,
    commit: bool = True,
) -> Product:
    product = Product(
        name=name,
        price=_dec(price),
        sku=(sku or None),
        track_stock=bool(track_stock),
        stock_qty=stock_qty if track_stock else None,
        is_active=is_active,
    )
    db.add(product)
    await db.flush()
    if commit:
        await db.commit()
        await db.refresh(product)
    return product


async def update_product(
    db: AsyncSession,
    product_id: int,
    *,
    name=_UNSET,
    price=_UNSET,
    sku=_UNSET,
    track_stock=_UNSET,
    stock_qty=_UNSET,
    is_active=_UNSET,
    commit: bool = True,
) -> Optional[Product]:
    product = await db.get(Product, product_id)
    if product is None:
        return None
    if name is not _UNSET:
        product.name = name
    if price is not _UNSET:
        product.price = _dec(price)
    if sku is not _UNSET:
        product.sku = sku or None
    if track_stock is not _UNSET:
        product.track_stock = bool(track_stock)
    if stock_qty is not _UNSET:
        product.stock_qty = stock_qty
    if is_active is not _UNSET:
        product.is_active = bool(is_active)
    product.updated_at = datetime.now(timezone.utc)
    await db.flush()
    if commit:
        await db.commit()
        await db.refresh(product)
    return product


async def set_product_active(
    db: AsyncSession, product_id: int, is_active: bool, *, commit: bool = True
) -> Optional[Product]:
    return await update_product(db, product_id, is_active=is_active, commit=commit)


async def adjust_stock(
    db: AsyncSession, product_id: int, delta: int, *, commit: bool = True
) -> Optional[Product]:
    """Apply a stock delta (negative to decrement on a sale)."""
    product = await db.get(Product, product_id)
    if product is None:
        return None
    if product.track_stock:
        current = product.stock_qty or 0
        product.stock_qty = current + delta
    await db.flush()
    if commit:
        await db.commit()
        await db.refresh(product)
    return product
