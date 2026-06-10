"""CRUD for the automated notification system.

Two concerns live here:

* ``notification_settings`` reads/writes — the per-event configuration edited from the
  desktop frontend (which template, variable mapping, offsets, enabled flag).
* ``notification_log`` claim/mark — the idempotency ledger. ``claim_log`` performs an
  ``INSERT ... ON CONFLICT (dedup_key) DO NOTHING`` so a claim is atomic: the first caller
  wins (row returned) and any concurrent/duplicate caller gets ``None`` and skips sending.
  This is what makes the daily sweep safe to run on multiple workers.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NotificationLog, NotificationSetting

logger = logging.getLogger(__name__)


@dataclass
class NotificationSettingData:
    id: Optional[int]
    event_type: str
    enabled: bool
    template_id: Optional[int]
    param_mapping: Optional[list]
    header_media_url: Optional[str]
    offsets_days: Optional[list]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    @classmethod
    def from_model(cls, m: NotificationSetting) -> "NotificationSettingData":
        return cls(
            id=m.id,
            event_type=m.event_type,
            enabled=bool(m.enabled),
            template_id=m.template_id,
            param_mapping=m.param_mapping,
            header_media_url=m.header_media_url,
            offsets_days=m.offsets_days,
            created_at=m.created_at,
            updated_at=m.updated_at,
        )


# ---------------------------------------------------------------------------
# Settings reads/writes
# ---------------------------------------------------------------------------
async def list_settings(db: AsyncSession) -> List[NotificationSettingData]:
    stmt = select(NotificationSetting).order_by(NotificationSetting.event_type)
    rows = (await db.execute(stmt)).scalars().all()
    return [NotificationSettingData.from_model(r) for r in rows]


async def get_setting_model(
    db: AsyncSession, event_type: str
) -> Optional[NotificationSetting]:
    stmt = select(NotificationSetting).where(NotificationSetting.event_type == event_type)
    return (await db.execute(stmt)).scalars().first()


async def get_setting(
    db: AsyncSession, event_type: str
) -> Optional[NotificationSettingData]:
    model = await get_setting_model(db, event_type)
    return NotificationSettingData.from_model(model) if model else None


async def upsert_setting(
    db: AsyncSession,
    *,
    event_type: str,
    enabled: bool = False,
    template_id: Optional[int] = None,
    param_mapping: Optional[list] = None,
    header_media_url: Optional[str] = None,
    offsets_days: Optional[list] = None,
    commit: bool = True,
) -> NotificationSetting:
    """Create or update the config row for ``event_type`` with the full desired state.

    The caller (the save mutation) always provides the complete configuration, so every
    field is assigned authoritatively (e.g. ``template_id=None`` clears the template).
    """
    setting = await get_setting_model(db, event_type)
    now = datetime.utcnow()
    if setting is None:
        setting = NotificationSetting(
            event_type=event_type,
            enabled=bool(enabled),
            template_id=template_id,
            param_mapping=param_mapping or [],
            header_media_url=header_media_url,
            offsets_days=offsets_days or [],
            created_at=now,
            updated_at=now,
        )
        db.add(setting)
    else:
        setting.enabled = bool(enabled)
        setting.template_id = template_id
        setting.param_mapping = param_mapping or []
        setting.header_media_url = header_media_url
        setting.offsets_days = offsets_days or []
        setting.updated_at = now
    await db.flush()
    if commit:
        await db.commit()
    return setting


# ---------------------------------------------------------------------------
# Idempotency ledger
# ---------------------------------------------------------------------------
async def claim_log(
    db: AsyncSession,
    *,
    dedup_key: str,
    event_type: str,
    person_id: Optional[int],
    subscription_id: Optional[int] = None,
    template_id: Optional[int] = None,
) -> Optional[NotificationLog]:
    """Atomically claim ``dedup_key`` by inserting a ``pending`` log row.

    Returns the claimed row, or ``None`` if the key already exists (already sent / in
    flight). Caller commits. Uses ``ON CONFLICT DO NOTHING`` so the claim never raises.
    """
    now = datetime.utcnow()
    stmt = (
        pg_insert(NotificationLog)
        .values(
            event_type=event_type,
            person_id=person_id,
            subscription_id=subscription_id,
            template_id=template_id,
            dedup_key=dedup_key,
            status="pending",
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["dedup_key"])
        .returning(NotificationLog.id)
    )
    result = await db.execute(stmt)
    inserted_id = result.scalar_one_or_none()
    await db.flush()
    if inserted_id is None:
        return None
    return await db.get(NotificationLog, inserted_id)


async def mark_log(
    db: AsyncSession,
    log: NotificationLog,
    *,
    status: str,
    wa_message_id: Optional[str] = None,
    error: Optional[str] = None,
    commit: bool = True,
) -> NotificationLog:
    """Update a claimed log row with the final outcome. Caller may commit."""
    log.status = status
    if wa_message_id is not None:
        log.wa_message_id = wa_message_id
    if error is not None:
        log.error = error[:4000]
    log.updated_at = datetime.utcnow()
    await db.flush()
    if commit:
        await db.commit()
    return log
