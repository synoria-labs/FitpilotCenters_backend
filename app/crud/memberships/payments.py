from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Payment

from .utils import _normalize_to_utc


def _apply_payment_filters(
    stmt,
    *,
    search: Optional[str],
    start_date: Optional[datetime],
    end_date: Optional[datetime],
    status: Optional[str],
    method: Optional[str],
):
    from app.models import People

    if search:
        search_term = f"%{search}%"
        stmt = stmt.where(
            or_(
                People.first_name.ilike(search_term),
                People.last_name.ilike(search_term),
                Payment.method.ilike(search_term),
                Payment.status.ilike(search_term),
            )
        )
    if start_date is not None:
        stmt = stmt.where(Payment.paid_at >= _normalize_to_utc(start_date))
    if end_date is not None:
        stmt = stmt.where(Payment.paid_at <= _normalize_to_utc(end_date))
    if status:
        stmt = stmt.where(Payment.status == status)
    if method:
        stmt = stmt.where(Payment.method == method)
    return stmt


async def create_payment(
    db: AsyncSession,
    *,
    person_id: int,
    amount: Decimal | float,
    method: str,
    subscription_id: Optional[int] = None,
    status: str = "COMPLETED",
    paid_at: Optional[datetime] = None,
    provider: Optional[str] = None,
    provider_payment_id: Optional[str] = None,
    external_reference: Optional[str] = None,
    comment: Optional[str] = None,
    recorded_by: Optional[int] = None,
    commit: bool = True,
) -> Payment:
    """Record a payment for a person and optional subscription.

    Idempotent on provider_payment_id: if a payment with the same external
    transaction id already exists, the existing row is returned instead of
    creating a duplicate. The unique partial index uq_payments_provider_payment_id
    acts as a final safety net at the DB layer.
    """
    if provider_payment_id:
        existing_stmt = select(Payment).where(
            Payment.provider_payment_id == provider_payment_id
        )
        existing = (await db.execute(existing_stmt)).scalar_one_or_none()
        if existing is not None:
            return existing

    amount_value = amount if isinstance(amount, Decimal) else Decimal(str(amount))

    payment = Payment(
        person_id=person_id,
        subscription_id=subscription_id,
        amount=amount_value,
        method=method,
        status=status,
        provider=provider,
        provider_payment_id=provider_payment_id,
        external_reference=external_reference,
        comment=comment,
        recorded_by=recorded_by,
    )

    if paid_at:
        payment.paid_at = _normalize_to_utc(paid_at)

    db.add(payment)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        if provider_payment_id:
            existing_stmt = select(Payment).where(
                Payment.provider_payment_id == provider_payment_id
            )
            existing = (await db.execute(existing_stmt)).scalar_one_or_none()
            if existing is not None:
                return existing
        raise

    if commit:
        await db.commit()
        await db.refresh(payment)

    return payment


async def get_payments(
    db: AsyncSession,
    *,
    limit: int = 100,
    offset: int = 0,
    search: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    status: Optional[str] = None,
    method: Optional[str] = None,
) -> list[Payment]:
    """Get a list of payments with optional filters and pagination."""
    stmt = select(Payment).options(selectinload(Payment.person)).join(Payment.person)
    stmt = _apply_payment_filters(
        stmt,
        search=search,
        start_date=start_date,
        end_date=end_date,
        status=status,
        method=method,
    )
    stmt = stmt.order_by(Payment.paid_at.desc()).offset(offset).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def count_payments(
    db: AsyncSession,
    *,
    search: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    status: Optional[str] = None,
    method: Optional[str] = None,
) -> int:
    """Count payments matching the same filter set as get_payments."""
    stmt = select(func.count(Payment.id)).select_from(Payment).join(Payment.person)
    stmt = _apply_payment_filters(
        stmt,
        search=search,
        start_date=start_date,
        end_date=end_date,
        status=status,
        method=method,
    )
    result = await db.execute(stmt)
    return int(result.scalar_one() or 0)


async def get_payment_by_id(db: AsyncSession, payment_id: int) -> Optional[Payment]:
    """Get a single payment by its ID."""
    stmt = select(Payment).where(Payment.id == payment_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def update_payment(
    db: AsyncSession,
    payment_id: int,
    *,
    amount: Optional[Decimal | float] = None,
    method: Optional[str] = None,
    status: Optional[str] = None,
    comment: Optional[str] = None,
    commit: bool = True,
) -> Optional[Payment]:
    """Update a payment's details."""
    payment = await get_payment_by_id(db, payment_id)
    if not payment:
        return None

    if amount is not None:
        payment.amount = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    if method is not None:
        payment.method = method
    if status is not None:
        payment.status = status
    if comment is not None:
        payment.comment = comment

    if commit:
        await db.commit()
        await db.refresh(payment)

    return payment


async def delete_payment(db: AsyncSession, payment_id: int, commit: bool = True) -> bool:
    """Delete a payment record."""
    payment = await get_payment_by_id(db, payment_id)
    if not payment:
        return False

    await db.delete(payment)
    if commit:
        await db.commit()

    return True
