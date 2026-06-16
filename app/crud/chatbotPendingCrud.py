"""CRUD for the propose-and-confirm pending-action ledger.

One pending action per conversation (unique ``conversation_id``). ``upsert_pending`` replaces
any existing pending row for the conversation so a new proposal supersedes a stale one.
``get_pending`` returns the active (``status='pending'``, non-expired) action, marking expired
rows so they are never executed.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChatbotPendingAction
from app.models.chatbotModel import (
    PENDING_STATUS_AWAITING_PAYMENT,
    PENDING_STATUS_CANCELED,
    PENDING_STATUS_EXPIRED,
    PENDING_STATUS_PENDING,
    PENDING_STATUS_PROCESSING,
)

DEFAULT_TTL_MINUTES = 30


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def get_pending(
    db: AsyncSession, conversation_id: int
) -> Optional[ChatbotPendingAction]:
    """Return the active pending action for a conversation, or None.

    Expired rows are flagged ``expired`` (and not returned) so they never execute.
    """
    stmt = select(ChatbotPendingAction).where(
        ChatbotPendingAction.conversation_id == conversation_id,
        ChatbotPendingAction.status == PENDING_STATUS_PENDING,
    )
    row = (await db.execute(stmt)).scalars().first()
    if row is None:
        return None
    if row.expires_at is not None and row.expires_at < _utcnow():
        row.status = PENDING_STATUS_EXPIRED
        row.updated_at = _utcnow()
        await db.flush()
        return None
    return row


async def get_active_pending(
    db: AsyncSession, conversation_id: int
) -> Optional[ChatbotPendingAction]:
    """Return the active action (``pending``, ``awaiting_payment`` or ``processing``), or None.

    Used to inject the pending state into the agent context so it confirms / reminds-to-pay /
    waits-while-processing instead of re-proposing (a re-propose mid-``processing`` would clobber
    the row the payment webhook is executing on). Expired rows are flagged and not returned.
    """
    stmt = select(ChatbotPendingAction).where(
        ChatbotPendingAction.conversation_id == conversation_id,
        ChatbotPendingAction.status.in_(
            [PENDING_STATUS_PENDING, PENDING_STATUS_AWAITING_PAYMENT, PENDING_STATUS_PROCESSING]
        ),
    )
    row = (await db.execute(stmt)).scalars().first()
    if row is None:
        return None
    if row.expires_at is not None and row.expires_at < _utcnow():
        row.status = PENDING_STATUS_EXPIRED
        row.updated_at = _utcnow()
        await db.flush()
        return None
    return row


async def get_by_external_reference(
    db: AsyncSession, external_reference: str, *, for_update: bool = False
) -> Optional[ChatbotPendingAction]:
    """Look up a pending action by its MercadoPago ``external_reference`` (webhook matching).

    With ``for_update=True`` the row is locked (``SELECT ... FOR UPDATE``) so concurrent duplicate
    webhooks for the same payment serialize: the second blocks until the first commits its claim.
    Must be called inside a transaction.
    """
    if not external_reference:
        return None
    stmt = select(ChatbotPendingAction).where(
        ChatbotPendingAction.external_reference == external_reference
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalars().first()


async def upsert_pending(
    db: AsyncSession,
    *,
    conversation_id: int,
    action_type: str,
    payload: dict,
    member_id: Optional[int] = None,
    summary: Optional[str] = None,
    status: str = PENDING_STATUS_PENDING,
    external_reference: Optional[str] = None,
    mp_preference_id: Optional[str] = None,
    mp_init_point: Optional[str] = None,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
    commit: bool = False,
) -> ChatbotPendingAction:
    """Create-or-replace the pending action for a conversation.

    Any prior row for the conversation is replaced (the unique index allows only one row per
    conversation). ``status`` is ``pending`` (await "Sí") or ``awaiting_payment`` (await the
    MercadoPago webhook); the MP fields carry the generated link/preference.
    """
    now = _utcnow()
    existing_stmt = select(ChatbotPendingAction).where(
        ChatbotPendingAction.conversation_id == conversation_id
    )
    existing = (await db.execute(existing_stmt)).scalars().first()

    expires_at = now + timedelta(minutes=ttl_minutes)
    if existing is not None:
        existing.action_type = action_type
        existing.payload = payload
        existing.member_id = member_id
        existing.summary = summary
        existing.status = status
        existing.external_reference = external_reference
        existing.mp_preference_id = mp_preference_id
        existing.mp_init_point = mp_init_point
        existing.expires_at = expires_at
        existing.updated_at = now
        row = existing
    else:
        row = ChatbotPendingAction(
            conversation_id=conversation_id,
            action_type=action_type,
            payload=payload,
            member_id=member_id,
            summary=summary,
            status=status,
            external_reference=external_reference,
            mp_preference_id=mp_preference_id,
            mp_init_point=mp_init_point,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        db.add(row)

    await db.flush()
    if commit:
        await db.commit()
    return row


async def mark_status(
    db: AsyncSession,
    pending_id: int,
    status: str,
    *,
    commit: bool = False,
) -> Optional[ChatbotPendingAction]:
    """Update the status of a pending action (confirmed / canceled / expired)."""
    row = await db.get(ChatbotPendingAction, pending_id)
    if row is None:
        return None
    row.status = status
    row.updated_at = _utcnow()
    await db.flush()
    if commit:
        await db.commit()
    return row


async def cancel_pending(
    db: AsyncSession, conversation_id: int, *, commit: bool = False
) -> bool:
    """Cancel the active pending action for a conversation (pending or awaiting payment)."""
    row = await get_active_pending(db, conversation_id)
    if row is None:
        return False
    row.status = PENDING_STATUS_CANCELED
    row.updated_at = _utcnow()
    await db.flush()
    if commit:
        await db.commit()
    return True
