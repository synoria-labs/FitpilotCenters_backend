"""CRUD for WhatsApp marketing consent (``communications_opt_in``).

Each change appends a NEW row (never mutates the prior one) so that the "most recent record wins"
read in ``notification_service._is_opted_out`` reflects the latest STOP/ALTA. Caller commits unless
``commit=True``.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.leadsModel import CommunicationOptIn


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def revoke_whatsapp_consent(
    db: AsyncSession, person_id: int, *, evidence: Optional[dict] = None, commit: bool = False
) -> CommunicationOptIn:
    """Record a WhatsApp marketing opt-out (e.g. inbound STOP/BAJA)."""
    row = CommunicationOptIn(
        person_id=person_id, channel="whatsapp",
        granted_at=None, revoked_at=_utcnow(), source="whatsapp", evidence=evidence,
    )
    db.add(row)
    await db.flush()
    if commit:
        await db.commit()
    return row


async def grant_whatsapp_consent(
    db: AsyncSession, person_id: int, *, evidence: Optional[dict] = None, commit: bool = False
) -> CommunicationOptIn:
    """Record a WhatsApp marketing opt-in (e.g. inbound ALTA/START)."""
    row = CommunicationOptIn(
        person_id=person_id, channel="whatsapp",
        granted_at=_utcnow(), revoked_at=None, source="whatsapp", evidence=evidence,
    )
    db.add(row)
    await db.flush()
    if commit:
        await db.commit()
    return row
