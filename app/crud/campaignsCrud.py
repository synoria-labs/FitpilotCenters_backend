"""CRUD for the marketing campaigns feature.

Mirrors the layering of ``notificationsCrud``:

* campaign definition reads/writes (``campaigns`` table).
* A/B variants (``campaign_variants``) — the MVP keeps a single auto-created variant ``A``.
* recipient ledger (``campaign_recipients``) — insert is idempotent via ``ON CONFLICT
  (dedup_key) DO NOTHING``; dispatch claims a recipient with an atomic compare-and-set
  (``UPDATE ... WHERE status IN ('pending','failed') RETURNING id``) so the row itself is the
  idempotency ledger — no separate log table.

Primary keys are assigned by the database. ``commit`` defaults vary per function and are
documented inline; the campaign_service generally commits.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Set

import logging

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Campaign,
    CampaignRecipient,
    CampaignVariant,
    MembershipSubscription,
    Payment,
    Reservation,
)
from app.models.campaignsModel import STATUS_SCHEDULED

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------
@dataclass
class CampaignData:
    id: int
    name: str
    description: Optional[str]
    objective: str
    status: str
    audience_spec: Optional[dict]
    template_id: Optional[int]
    param_mapping: Optional[list]
    header_media_url: Optional[str]
    header_media_asset_id: Optional[int]
    marketing_campaign_id: Optional[int]
    scheduled_at: Optional[datetime]
    send_local_time: bool
    conversion_window_days: int
    conversion_metric: str
    recency_block_days: int
    throttle_per_minute: int
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    @classmethod
    def from_model(cls, m: Campaign) -> "CampaignData":
        return cls(
            id=m.id,
            name=m.name,
            description=m.description,
            objective=m.objective,
            status=m.status,
            audience_spec=m.audience_spec,
            template_id=m.template_id,
            param_mapping=m.param_mapping,
            header_media_url=m.header_media_url,
            header_media_asset_id=m.header_media_asset_id,
            marketing_campaign_id=m.marketing_campaign_id,
            scheduled_at=m.scheduled_at,
            send_local_time=bool(m.send_local_time),
            conversion_window_days=m.conversion_window_days,
            conversion_metric=m.conversion_metric,
            recency_block_days=m.recency_block_days,
            throttle_per_minute=m.throttle_per_minute,
            started_at=m.started_at,
            finished_at=m.finished_at,
            created_at=m.created_at,
            updated_at=m.updated_at,
        )


@dataclass
class CampaignRecipientData:
    id: int
    campaign_id: int
    variant_id: Optional[int]
    person_id: Optional[int]
    lead_id: Optional[int]
    subscription_id: Optional[int]
    phone_e164: Optional[str]
    wa_id: Optional[str]
    status: str
    skip_reason: Optional[str]
    wa_message_id: Optional[str]
    sent_at: Optional[datetime]
    delivered_at: Optional[datetime]
    read_at: Optional[datetime]
    replied_at: Optional[datetime]
    error: Optional[str]
    converted: bool
    converted_at: Optional[datetime]
    targeted_at: Optional[datetime]

    @classmethod
    def from_model(cls, m: CampaignRecipient) -> "CampaignRecipientData":
        return cls(
            id=m.id,
            campaign_id=m.campaign_id,
            variant_id=m.variant_id,
            person_id=m.person_id,
            lead_id=m.lead_id,
            subscription_id=m.subscription_id,
            phone_e164=m.phone_e164,
            wa_id=m.wa_id,
            status=m.status,
            skip_reason=m.skip_reason,
            wa_message_id=m.wa_message_id,
            sent_at=m.sent_at,
            delivered_at=m.delivered_at,
            read_at=m.read_at,
            replied_at=m.replied_at,
            error=m.error,
            converted=bool(m.converted),
            converted_at=m.converted_at,
            targeted_at=m.targeted_at,
        )


# ---------------------------------------------------------------------------
# Campaign reads/writes
# ---------------------------------------------------------------------------
async def list_campaigns(
    db: AsyncSession,
    *,
    status: Optional[str] = None,
    objective: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[CampaignData]:
    stmt = select(Campaign)
    if status:
        stmt = stmt.where(Campaign.status == status)
    if objective:
        stmt = stmt.where(Campaign.objective == objective)
    stmt = stmt.order_by(Campaign.created_at.desc()).offset(offset).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [CampaignData.from_model(r) for r in rows]


async def get_campaign_model(db: AsyncSession, campaign_id: int) -> Optional[Campaign]:
    return (
        await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    ).scalars().first()


async def get_campaign(db: AsyncSession, campaign_id: int) -> Optional[CampaignData]:
    model = await get_campaign_model(db, campaign_id)
    return CampaignData.from_model(model) if model else None


# Fields a draft campaign may set/update (authoritative full-state assignment).
_WRITABLE_FIELDS = (
    "name", "description", "objective", "audience_spec", "template_id",
    "param_mapping", "header_media_url", "header_media_asset_id",
    "marketing_campaign_id", "conversion_window_days", "conversion_metric",
    "recency_block_days", "throttle_per_minute",
)


async def create_campaign(
    db: AsyncSession, *, created_by: Optional[int] = None, commit: bool = True, **fields
) -> Campaign:
    now = _utcnow()
    values = {k: fields.get(k) for k in _WRITABLE_FIELDS}
    campaign = Campaign(
        status="draft",
        created_by=created_by,
        created_at=now,
        updated_at=now,
        **{k: v for k, v in values.items() if v is not None},
    )
    db.add(campaign)
    await db.flush()
    if commit:
        await db.commit()
    return campaign


async def update_campaign(
    db: AsyncSession, campaign: Campaign, *, commit: bool = True, **fields
) -> Campaign:
    for key in _WRITABLE_FIELDS:
        if key in fields:
            setattr(campaign, key, fields[key])
    campaign.updated_at = _utcnow()
    await db.flush()
    if commit:
        await db.commit()
    return campaign


async def set_campaign_status(
    db: AsyncSession,
    campaign: Campaign,
    *,
    status: str,
    scheduled_at: Optional[datetime] = None,
    send_local_time: Optional[bool] = None,
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
    commit: bool = True,
) -> Campaign:
    campaign.status = status
    if scheduled_at is not None:
        campaign.scheduled_at = scheduled_at
    if send_local_time is not None:
        campaign.send_local_time = send_local_time
    if started_at is not None:
        campaign.started_at = started_at
    if finished_at is not None:
        campaign.finished_at = finished_at
    campaign.updated_at = _utcnow()
    await db.flush()
    if commit:
        await db.commit()
    return campaign


async def delete_campaign(db: AsyncSession, campaign: Campaign, commit: bool = True) -> bool:
    await db.delete(campaign)
    await db.flush()
    if commit:
        await db.commit()
    return True


async def campaigns_due_for_send(db: AsyncSession, now: datetime) -> List[Campaign]:
    """Scheduled campaigns whose send time has arrived (for the scheduler sweep)."""
    stmt = select(Campaign).where(
        Campaign.status == STATUS_SCHEDULED,
        Campaign.scheduled_at.isnot(None),
        Campaign.scheduled_at <= now,
    )
    return list((await db.execute(stmt)).scalars().all())


async def campaigns_with_open_conversion_window(db: AsyncSession) -> List[Campaign]:
    """Campaigns that may still accrue conversions (have been sending/finished recently)."""
    stmt = select(Campaign).where(
        Campaign.status.in_(["sending", "paused", "completed"]),
        Campaign.started_at.isnot(None),
    )
    return list((await db.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------
async def list_variants(db: AsyncSession, campaign_id: int) -> List[CampaignVariant]:
    stmt = (
        select(CampaignVariant)
        .where(CampaignVariant.campaign_id == campaign_id)
        .order_by(CampaignVariant.variant_code)
    )
    return list((await db.execute(stmt)).scalars().all())


async def ensure_default_variant(
    db: AsyncSession, campaign_id: int, commit: bool = True
) -> CampaignVariant:
    """Return the campaign's variant 'A', creating it if absent (MVP: single variant)."""
    existing = await db.execute(
        select(CampaignVariant).where(
            CampaignVariant.campaign_id == campaign_id,
            CampaignVariant.variant_code == "A",
        )
    )
    variant = existing.scalars().first()
    if variant is not None:
        return variant
    variant = CampaignVariant(
        campaign_id=campaign_id, variant_code="A", weight=1, is_control=False,
        created_at=_utcnow(),
    )
    db.add(variant)
    await db.flush()
    if commit:
        await db.commit()
    return variant


# ---------------------------------------------------------------------------
# Recipients — snapshot + ledger
# ---------------------------------------------------------------------------
async def insert_recipient(
    db: AsyncSession,
    *,
    campaign_id: int,
    dedup_key: str,
    variant_id: Optional[int] = None,
    person_id: Optional[int] = None,
    lead_id: Optional[int] = None,
    subscription_id: Optional[int] = None,
    phone_e164: Optional[str] = None,
    wa_id: Optional[str] = None,
    status: str = "pending",
    skip_reason: Optional[str] = None,
) -> Optional[int]:
    """Insert one recipient idempotently. Returns the new id or None if dedup_key exists.

    Caller commits (build phase commits in batches).
    """
    now = _utcnow()
    stmt = (
        pg_insert(CampaignRecipient)
        .values(
            campaign_id=campaign_id,
            variant_id=variant_id,
            person_id=person_id,
            lead_id=lead_id,
            subscription_id=subscription_id,
            phone_e164=phone_e164,
            wa_id=wa_id,
            dedup_key=dedup_key,
            status=status,
            skip_reason=skip_reason,
            targeted_at=now,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["dedup_key"])
        .returning(CampaignRecipient.id)
    )
    inserted_id = (await db.execute(stmt)).scalar_one_or_none()
    await db.flush()
    return inserted_id


async def get_recipient_model(
    db: AsyncSession, recipient_id: int
) -> Optional[CampaignRecipient]:
    return await db.get(CampaignRecipient, recipient_id)


async def list_sendable_recipient_ids(db: AsyncSession, campaign_id: int) -> List[int]:
    """Ids of recipients that still need sending (pending or previously failed)."""
    stmt = (
        select(CampaignRecipient.id)
        .where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.status.in_(["pending", "failed"]),
        )
        .order_by(CampaignRecipient.id)
    )
    return [row for row in (await db.execute(stmt)).scalars().all()]


async def claim_recipient_for_send(db: AsyncSession, recipient_id: int) -> bool:
    """Atomically move a recipient to 'sending'. Returns True if this worker claimed it.

    Compare-and-set: only a row currently in ('pending','failed') flips, so concurrent
    workers / re-runs never double-send. Caller commits.
    """
    stmt = (
        update(CampaignRecipient)
        .where(
            CampaignRecipient.id == recipient_id,
            CampaignRecipient.status.in_(["pending", "failed"]),
        )
        .values(status="sending", error=None, updated_at=_utcnow())
        .returning(CampaignRecipient.id)
    )
    claimed = (await db.execute(stmt)).scalar_one_or_none()
    await db.flush()
    return claimed is not None


async def mark_recipient_sent(
    db: AsyncSession,
    recipient: CampaignRecipient,
    *,
    wa_message_id: Optional[str],
    message_id: Optional[int],
    commit: bool = True,
) -> CampaignRecipient:
    now = _utcnow()
    recipient.status = "sent"
    recipient.wa_message_id = wa_message_id
    recipient.message_id = message_id
    recipient.sent_at = now
    recipient.error = None
    recipient.updated_at = now
    await db.flush()
    if commit:
        await db.commit()
    return recipient


async def mark_recipient_failed(
    db: AsyncSession, recipient: CampaignRecipient, *, error: str, commit: bool = True
) -> CampaignRecipient:
    recipient.status = "failed"
    recipient.error = (error or "")[:4000]
    recipient.updated_at = _utcnow()
    await db.flush()
    if commit:
        await db.commit()
    return recipient


async def mark_recipient_terminal(
    db: AsyncSession,
    recipient: CampaignRecipient,
    *,
    status: str,
    skip_reason: Optional[str] = None,
    commit: bool = True,
) -> CampaignRecipient:
    """Set a non-sent terminal status (e.g. 'opted_out', 'skipped') on a claimed recipient."""
    recipient.status = status
    if skip_reason is not None:
        recipient.skip_reason = skip_reason
    recipient.updated_at = _utcnow()
    await db.flush()
    if commit:
        await db.commit()
    return recipient


async def list_recipients(
    db: AsyncSession,
    campaign_id: int,
    *,
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[CampaignRecipientData]:
    stmt = select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign_id)
    if status:
        stmt = stmt.where(CampaignRecipient.status == status)
    stmt = stmt.order_by(CampaignRecipient.id).offset(offset).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [CampaignRecipientData.from_model(r) for r in rows]


async def recipient_status_counts(db: AsyncSession, campaign_id: int) -> Dict[str, int]:
    stmt = (
        select(CampaignRecipient.status, func.count())
        .where(CampaignRecipient.campaign_id == campaign_id)
        .group_by(CampaignRecipient.status)
    )
    rows = (await db.execute(stmt)).all()
    return {status: int(count) for status, count in rows}


async def recently_targeted_person_ids(
    db: AsyncSession, *, days: int, exclude_campaign_id: Optional[int] = None
) -> Set[int]:
    """Person ids targeted by any campaign within the last ``days`` (send-fatigue guard)."""
    if days <= 0:
        return set()
    floor = _utcnow() - timedelta(days=days)
    stmt = (
        select(CampaignRecipient.person_id)
        .where(
            CampaignRecipient.person_id.isnot(None),
            CampaignRecipient.targeted_at >= floor,
            CampaignRecipient.status != "skipped",
        )
        .distinct()
    )
    if exclude_campaign_id is not None:
        stmt = stmt.where(CampaignRecipient.campaign_id != exclude_campaign_id)
    return {row for row in (await db.execute(stmt)).scalars().all() if row is not None}


# ---------------------------------------------------------------------------
# Webhook hook: status -> recipient (delivered/read/...)
# ---------------------------------------------------------------------------
# Meta status -> (recipient status, timestamp column). Forward-only: never downgrade
# (e.g. a late 'sent' callback must not overwrite 'read').
_STATUS_RANK = {
    "pending": 0, "sending": 1, "sent": 2, "delivered": 3, "read": 4, "replied": 5,
}
_META_STATUS_MAP = {
    "sent": ("sent", "sent_at"),
    "delivered": ("delivered", "delivered_at"),
    "read": ("read", "read_at"),
    "failed": ("failed", None),
}


async def apply_delivery_status(
    db: AsyncSession, *, wa_message_id: str, meta_status: str, timestamp: datetime
) -> bool:
    """Update the campaign recipient matching ``wa_message_id`` from a Meta status callback.

    Idempotent and forward-only. Returns True if a recipient row was updated. Caller commits
    (the webhook ingest commits the whole batch).
    """
    mapped = _META_STATUS_MAP.get((meta_status or "").lower())
    if mapped is None:
        return False
    new_status, ts_col = mapped

    recipient = (
        await db.execute(
            select(CampaignRecipient).where(CampaignRecipient.wa_message_id == wa_message_id)
        )
    ).scalars().first()
    if recipient is None:
        return False

    changed = False
    # Always stamp the lifecycle timestamp when present (even out of order).
    if ts_col is not None and getattr(recipient, ts_col) is None:
        setattr(recipient, ts_col, timestamp)
        changed = True

    if new_status == "failed":
        if recipient.status not in ("replied",):
            recipient.status = "failed"
            changed = True
    else:
        # Only advance the status forward.
        if _STATUS_RANK.get(new_status, 0) > _STATUS_RANK.get(recipient.status, 0):
            recipient.status = new_status
            changed = True

    if changed:
        recipient.updated_at = _utcnow()
        await db.flush()
    return changed


# ---------------------------------------------------------------------------
# Conversion attribution (reuses payments / reservations directly)
# ---------------------------------------------------------------------------
async def list_recipients_pending_conversion(
    db: AsyncSession, campaign_id: int, *, window_days: int
) -> List[CampaignRecipient]:
    """Sent recipients still inside the conversion window and not yet converted."""
    floor = _utcnow() - timedelta(days=max(window_days, 0))
    stmt = select(CampaignRecipient).where(
        CampaignRecipient.campaign_id == campaign_id,
        CampaignRecipient.person_id.isnot(None),
        CampaignRecipient.converted.is_(False),
        CampaignRecipient.sent_at.isnot(None),
        CampaignRecipient.sent_at >= floor,
        CampaignRecipient.status.in_(["sent", "delivered", "read", "replied"]),
    )
    return list((await db.execute(stmt)).scalars().all())


async def find_first_completed_payment(
    db: AsyncSession, person_id: int, *, start: datetime, end: datetime
) -> Optional[Payment]:
    stmt = (
        select(Payment)
        .where(
            Payment.person_id == person_id,
            Payment.status == "COMPLETED",
            Payment.paid_at >= start,
            Payment.paid_at <= end,
        )
        .order_by(Payment.paid_at.asc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


async def has_new_subscription_since(
    db: AsyncSession, person_id: int, *, since: datetime
) -> bool:
    stmt = (
        select(MembershipSubscription.id)
        .where(
            MembershipSubscription.person_id == person_id,
            MembershipSubscription.start_at >= since,
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first() is not None


async def has_reservation_in_window(
    db: AsyncSession, person_id: int, *, start: datetime, end: datetime
) -> bool:
    stmt = (
        select(Reservation.id)
        .where(
            Reservation.person_id == person_id,
            Reservation.reserved_at >= start,
            Reservation.reserved_at <= end,
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first() is not None


async def mark_recipient_converted(
    db: AsyncSession,
    recipient: CampaignRecipient,
    *,
    payment_id: Optional[int],
    converted_at: datetime,
    commit: bool = True,
) -> CampaignRecipient:
    recipient.converted = True
    recipient.converted_at = converted_at
    recipient.conversion_payment_id = payment_id
    recipient.updated_at = _utcnow()
    await db.flush()
    if commit:
        await db.commit()
    return recipient


async def conversion_revenue(db: AsyncSession, campaign_id: int) -> Decimal:
    """Sum of the attributed payment amounts for a campaign (revenue recovered)."""
    stmt = (
        select(func.coalesce(func.sum(Payment.amount), 0))
        .select_from(CampaignRecipient)
        .join(Payment, Payment.id == CampaignRecipient.conversion_payment_id)
        .where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.converted.is_(True),
        )
    )
    value = (await db.execute(stmt)).scalar_one()
    return Decimal(str(value or 0))
