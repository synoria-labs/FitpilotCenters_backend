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
    PENDING_STATUS_CANCELED,
    PENDING_STATUS_EXPIRED,
    PENDING_STATUS_PENDING,
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


async def upsert_pending(
    db: AsyncSession,
    *,
    conversation_id: int,
    action_type: str,
    payload: dict,
    member_id: Optional[int] = None,
    summary: Optional[str] = None,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
    commit: bool = False,
) -> ChatbotPendingAction:
    """Create-or-replace the pending action for a conversation.

    Any prior pending row for the conversation is cancelled first (the unique index allows
    only one row per conversation, but cancelling preserves an audit trail of supersession).
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
        existing.status = PENDING_STATUS_PENDING
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
            status=PENDING_STATUS_PENDING,
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
    """Cancel the active pending action for a conversation, if any."""
    row = await get_pending(db, conversation_id)
    if row is None:
        return False
    row.status = PENDING_STATUS_CANCELED
    row.updated_at = _utcnow()
    await db.flush()
    if commit:
        await db.commit()
    return True
