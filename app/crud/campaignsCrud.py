"""CRUD for the marketing campaigns feature.

Mirrors the layering of ``notificationsCrud``:

* campaign definition reads/writes (``campaigns`` table).
* A/B variants (``campaign_variants``) — the MVP keeps a single auto-created variant ``A``.
* recipient ledger (``campaign_recipients``) — insert is idempotent via ``ON CONFLICT
  (dedup_key) DO NOTHING``; dispatch claims a *batch* with an atomic compare-and-set
  (``UPDATE ... WHERE status IN ('pending','failed') ... FOR UPDATE SKIP LOCKED RETURNING id``)
  so the row itself is the idempotency ledger — no separate log table, and concurrent
  dispatchers take disjoint batches instead of colliding.

Primary keys are assigned by the database. ``commit`` defaults vary per function and are
documented inline; the campaign_service generally commits.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Set

import logging
import re

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
from app.models.campaignsModel import STATUS_SCHEDULED, STATUS_SENDING

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
    header_text_param_key: Optional[str]
    button_url_param_key: Optional[str]
    location_param: Optional[dict]
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
            header_text_param_key=m.header_text_param_key,
            button_url_param_key=m.button_url_param_key,
            location_param=m.location_param,
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
    favorite_class_type_id: Optional[int]
    favorite_class_template_id: Optional[int]
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
            favorite_class_type_id=m.favorite_class_type_id,
            favorite_class_template_id=m.favorite_class_template_id,
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
    # Runtime values for the newer template components. These are read at send time but were
    # missing here, so a template with a TEXT header, a dynamic URL button or a LOCATION
    # header could never be configured — and failed for the entire audience.
    "header_text_param_key", "button_url_param_key", "location_param",
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
    if status == STATUS_SENDING:
        # Claim the run now so the stale-run sweep does not immediately reclaim a campaign
        # that was just marked sending but has not reached its dispatch loop yet.
        campaign.heartbeat_at = _utcnow()
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


# A ``sending`` campaign that has not stamped its heartbeat in this long was orphaned by a
# process restart, not merely slow: the run loop stamps far more often than this.
STALE_RUN_MINUTES = 10


async def touch_campaign_heartbeat(db: AsyncSession, campaign_id: int) -> None:
    """Stamp ``heartbeat_at`` to signal that a dispatch run is still alive.

    Written with a bare UPDATE (not through the ORM object) so it never clobbers other
    in-flight field changes, and committed by the caller alongside its own work.
    """
    await db.execute(
        update(Campaign).where(Campaign.id == campaign_id).values(heartbeat_at=_utcnow())
    )
    await db.flush()


async def campaigns_due_for_send(db: AsyncSession, now: datetime) -> List[Campaign]:
    """Campaigns the sweep should dispatch: scheduled-and-due, plus abandoned runs.

    A campaign left in ``sending`` by a crashed process has a stale (or absent) heartbeat
    and would otherwise stay stuck forever — no other path picks it up. Reclaiming it is
    safe because every recipient is claimed with an atomic compare-and-set, so a second
    dispatcher can never double-send.
    """
    stale_floor = now - timedelta(minutes=STALE_RUN_MINUTES)
    stmt = select(Campaign).where(
        or_(
            and_(
                Campaign.status == STATUS_SCHEDULED,
                Campaign.scheduled_at.isnot(None),
                Campaign.scheduled_at <= now,
            ),
            and_(
                Campaign.status == STATUS_SENDING,
                or_(
                    Campaign.heartbeat_at.is_(None),
                    Campaign.heartbeat_at < stale_floor,
                ),
            ),
        )
    )
    return list((await db.execute(stmt)).scalars().all())


async def campaigns_with_open_conversion_window(db: AsyncSession) -> List[Campaign]:
    """Campaigns that may still accrue conversions.

    Bounded by each campaign's own window: without the time bound this returned every
    campaign ever sent, so the nightly sweep grew without limit and kept re-checking blasts
    whose attribution window closed months ago.
    """
    stmt = select(Campaign).where(
        Campaign.status.in_(["sending", "paused", "completed"]),
        Campaign.started_at.isnot(None),
        Campaign.started_at
        >= func.now() - func.make_interval(0, 0, 0, Campaign.conversion_window_days),
    )
    return list((await db.execute(stmt)).scalars().all())


async def attribute_payment_conversions(
    db: AsyncSession, campaign_id: int, *, window_days: int, commit: bool = False
) -> int:
    """Attribute completed payments to a campaign's recipients in one statement.

    Replaces a per-recipient loop that ran one or two queries each. Picks the earliest
    qualifying payment inside each recipient's own window, which is the same choice the
    row-by-row version made.
    """
    days = max(int(window_days), 0)

    def _in_window():
        """The recipient's own attribution window, earliest payment first.

        Ordered by ``paid_at`` — not by id — to match ``find_first_completed_payment``: a
        backdated payment has a higher id but is the earlier payment, and picking the wrong
        one would misreport both the conversion date and the attributed amount.
        """
        return (
            select(Payment)
            .where(
                Payment.person_id == CampaignRecipient.person_id,
                Payment.status == "COMPLETED",
                Payment.paid_at >= CampaignRecipient.sent_at,
                Payment.paid_at
                <= CampaignRecipient.sent_at + func.make_interval(0, 0, 0, days),
            )
            .order_by(Payment.paid_at.asc())
            .limit(1)
            .correlate(CampaignRecipient)
        )

    first_payment = _in_window().with_only_columns(Payment.id).scalar_subquery()
    first_paid_at = _in_window().with_only_columns(Payment.paid_at).scalar_subquery()
    stmt = (
        update(CampaignRecipient)
        .where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.converted.is_(False),
            CampaignRecipient.person_id.isnot(None),
            CampaignRecipient.sent_at.isnot(None),
            CampaignRecipient.status.in_(["sent", "delivered", "read", "replied"]),
            first_payment.isnot(None),
        )
        .values(
            converted=True,
            conversion_payment_id=first_payment,
            converted_at=first_paid_at,
            updated_at=_utcnow(),
        )
        .execution_options(synchronize_session=False)
        .returning(CampaignRecipient.id)
    )
    attributed = list((await db.execute(stmt)).scalars().all())
    await db.flush()
    if commit:
        await db.commit()
    return len(attributed)


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
    favorite_class_type_id: Optional[int] = None,
    favorite_class_template_id: Optional[int] = None,
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
            favorite_class_type_id=favorite_class_type_id,
            favorite_class_template_id=favorite_class_template_id,
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


async def insert_recipients_bulk(
    db: AsyncSession, rows: List[Dict], *, chunk_size: int = 500
) -> int:
    """Insert many recipients idempotently. Returns how many were actually new.

    Same ``ON CONFLICT (dedup_key) DO NOTHING`` guarantee as the single-row insert, but a
    few round-trips instead of one per member — building a 5,000-member audience was 5,000
    INSERT statements. Caller commits.
    """
    if not rows:
        return 0
    now = _utcnow()
    inserted = 0
    for start in range(0, len(rows), chunk_size):
        chunk = [
            {"targeted_at": now, "created_at": now, "updated_at": now, **row}
            for row in rows[start : start + chunk_size]
        ]
        stmt = (
            pg_insert(CampaignRecipient)
            .values(chunk)
            .on_conflict_do_nothing(index_elements=["dedup_key"])
            .returning(CampaignRecipient.id)
        )
        inserted += len((await db.execute(stmt)).scalars().all())
    await db.flush()
    return inserted


async def opted_out_person_ids(
    db: AsyncSession, person_ids: List[int]
) -> Set[int]:
    """Which of these people have revoked WhatsApp consent, in one query.

    Mirrors ``notification_service._is_opted_out`` exactly: only the LATEST whatsapp consent
    row counts, and only a revocation at or after the grant means opted out.
    """
    if not person_ids:
        return set()
    from app.models import CommunicationOptIn

    latest = (
        select(
            CommunicationOptIn.person_id,
            CommunicationOptIn.granted_at,
            CommunicationOptIn.revoked_at,
        )
        .where(
            CommunicationOptIn.person_id.in_(person_ids),
            CommunicationOptIn.channel == "whatsapp",
        )
        .distinct(CommunicationOptIn.person_id)
        .order_by(CommunicationOptIn.person_id, CommunicationOptIn.created_at.desc())
    )
    return {
        person_id
        for person_id, granted_at, revoked_at in (await db.execute(latest)).all()
        if revoked_at is not None and (granted_at is None or revoked_at >= granted_at)
    }


async def get_recipient_model(
    db: AsyncSession, recipient_id: int
) -> Optional[CampaignRecipient]:
    return await db.get(CampaignRecipient, recipient_id)


async def list_sendable_recipient_ids(db: AsyncSession, campaign_id: int) -> List[int]:
    """Ids of recipients that need sending AND are already due (pending or previously failed)."""
    now = _utcnow()
    stmt = (
        select(CampaignRecipient.id)
        .where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.status.in_(["pending", "failed"]),
            or_(
                CampaignRecipient.send_after.is_(None),
                CampaignRecipient.send_after <= now,
            ),
        )
        .order_by(CampaignRecipient.id)
    )
    return [row for row in (await db.execute(stmt)).scalars().all()]


async def claim_recipient_batch(
    db: AsyncSession, campaign_id: int, limit: int
) -> List[int]:
    """Atomically claim up to ``limit`` recipients that are due now. Caller commits.

    One round-trip instead of one per recipient, and ``FOR UPDATE SKIP LOCKED`` means two
    dispatchers running at once simply take disjoint batches rather than fighting over rows —
    so a reclaimed run and its original can safely overlap.
    """
    if limit <= 0:
        return []
    now = _utcnow()
    due = (
        select(CampaignRecipient.id)
        .where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.status.in_(["pending", "failed"]),
            or_(
                CampaignRecipient.send_after.is_(None),
                CampaignRecipient.send_after <= now,
            ),
        )
        .order_by(CampaignRecipient.send_after.nulls_first(), CampaignRecipient.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    stmt = (
        update(CampaignRecipient)
        .where(CampaignRecipient.id.in_(due.scalar_subquery()))
        .values(status="sending", error=None, updated_at=now)
        .returning(CampaignRecipient.id)
        .execution_options(synchronize_session=False)
    )
    claimed = list((await db.execute(stmt)).scalars().all())
    await db.flush()
    return claimed


async def defer_recipient(
    db: AsyncSession,
    recipient: CampaignRecipient,
    *,
    send_after: datetime,
    reason: Optional[str] = None,
    commit: bool = True,
) -> CampaignRecipient:
    """Put a claimed recipient back to ``pending``, eligible again at ``send_after``.

    Deliberately NOT ``failed``: nothing went wrong, the moment was simply not allowed yet.
    Recording it as a failure both misreports the campaign and (because failed rows are
    retryable) makes the next sweep try again immediately.
    """
    recipient.status = "pending"
    recipient.send_after = send_after
    recipient.skip_reason = reason
    recipient.error = None
    recipient.updated_at = _utcnow()
    await db.flush()
    if commit:
        await db.commit()
    return recipient


async def next_send_after(db: AsyncSession, campaign_id: int) -> Optional[datetime]:
    """Earliest future eligibility instant, so the campaign can be rescheduled precisely."""
    stmt = (
        select(func.min(CampaignRecipient.send_after))
        .where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.status.in_(["pending", "failed"]),
            CampaignRecipient.send_after.isnot(None),
            CampaignRecipient.send_after > _utcnow(),
        )
    )
    return (await db.execute(stmt)).scalar_one_or_none()


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


async def list_recipient_favorite_refs(db: AsyncSession, campaign_id: int) -> List:
    """(id, favorite_class_type_id, favorite_class_template_id) for a campaign's recipients.

    Three narrow columns, fetched once per run, so a dispatch can resolve every recipient's
    class labels in one pass instead of joining per message.
    """
    stmt = select(
        CampaignRecipient.id,
        CampaignRecipient.favorite_class_type_id,
        CampaignRecipient.favorite_class_template_id,
    ).where(CampaignRecipient.campaign_id == campaign_id)
    return list((await db.execute(stmt)).all())


async def recipient_status_counts(db: AsyncSession, campaign_id: int) -> Dict[str, int]:
    stmt = (
        select(CampaignRecipient.status, func.count())
        .where(CampaignRecipient.campaign_id == campaign_id)
        .group_by(CampaignRecipient.status)
    )
    rows = (await db.execute(stmt)).all()
    return {status: int(count) for status, count in rows}


async def recipient_status_counts_for(
    db: AsyncSession, campaign_ids: List[int]
) -> Dict[int, Dict[str, int]]:
    """Status histogram for several campaigns at once.

    The campaign list shows sent/converted per row; asking per row is a query per campaign on
    every refresh. One GROUP BY answers the whole page.
    """
    if not campaign_ids:
        return {}
    stmt = (
        select(
            CampaignRecipient.campaign_id,
            CampaignRecipient.status,
            func.count(),
        )
        .where(CampaignRecipient.campaign_id.in_(campaign_ids))
        .group_by(CampaignRecipient.campaign_id, CampaignRecipient.status)
    )
    out: Dict[int, Dict[str, int]] = {cid: {} for cid in campaign_ids}
    for campaign_id, status, count in (await db.execute(stmt)).all():
        out.setdefault(campaign_id, {})[status] = int(count)
    return out


async def converted_counts_for(
    db: AsyncSession, campaign_ids: List[int]
) -> Dict[int, int]:
    if not campaign_ids:
        return {}
    stmt = (
        select(CampaignRecipient.campaign_id, func.count())
        .where(
            CampaignRecipient.campaign_id.in_(campaign_ids),
            CampaignRecipient.converted.is_(True),
        )
        .group_by(CampaignRecipient.campaign_id)
    )
    counts = {cid: 0 for cid in campaign_ids}
    for campaign_id, count in (await db.execute(stmt)).all():
        counts[campaign_id] = int(count)
    return counts


async def conversion_revenue_for(
    db: AsyncSession, campaign_ids: List[int]
) -> Dict[int, Decimal]:
    """Attributed revenue per campaign, in one join instead of one query per campaign."""
    if not campaign_ids:
        return {}
    stmt = (
        select(
            CampaignRecipient.campaign_id,
            func.coalesce(func.sum(Payment.amount), 0),
        )
        .select_from(CampaignRecipient)
        .join(Payment, Payment.id == CampaignRecipient.conversion_payment_id)
        .where(
            CampaignRecipient.campaign_id.in_(campaign_ids),
            CampaignRecipient.converted.is_(True),
        )
        .group_by(CampaignRecipient.campaign_id)
    )
    revenue = {cid: Decimal(0) for cid in campaign_ids}
    for campaign_id, total in (await db.execute(stmt)).all():
        revenue[campaign_id] = Decimal(str(total or 0))
    return revenue


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
# Terminal states rank above every delivery state so a late webhook can never resurrect a
# recipient we deliberately did not send to. Without explicit entries they defaulted to 0 and
# a stray 'sent' callback would have promoted them.
_TERMINAL_RANK = 99
_STATUS_RANK = {
    "pending": 0, "sending": 1, "sent": 2, "delivered": 3, "read": 4, "replied": 5,
    "skipped": _TERMINAL_RANK, "opted_out": _TERMINAL_RANK,
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
        if recipient.status not in ("replied", "skipped", "opted_out"):
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


# A reply is attributed to the most recent campaign message sent to that number within
# this window. Deliberately much tighter than the conversion window: a message two weeks
# later is a new conversation, not a reply to the campaign.
REPLY_ATTRIBUTION_DAYS = 7


async def apply_inbound_reply(
    db: AsyncSession, *, wa_id: str, timestamp: datetime
) -> bool:
    """Mark the campaign recipient this inbound message replies to as ``replied``.

    Meta status callbacks only ever report sent/delivered/read, so ``replied`` can only be
    derived from inbound traffic. Idempotent and forward-only (mirrors
    :func:`apply_delivery_status`). Returns True if a recipient row was updated. Caller
    commits (the webhook ingest commits the whole batch).
    """
    digits = re.sub(r"\D", "", wa_id or "")
    if not digits:
        return False
    floor = _utcnow() - timedelta(days=REPLY_ATTRIBUTION_DAYS)
    stmt = (
        select(CampaignRecipient)
        .where(
            CampaignRecipient.wa_id == digits,
            CampaignRecipient.sent_at.isnot(None),
            CampaignRecipient.sent_at >= floor,
            CampaignRecipient.status.in_(["sent", "delivered", "read"]),
        )
        .order_by(CampaignRecipient.sent_at.desc())
        .limit(1)
    )
    recipient = (await db.execute(stmt)).scalars().first()
    if recipient is None:
        return False

    recipient.status = "replied"
    if recipient.replied_at is None:
        recipient.replied_at = timestamp
    recipient.updated_at = _utcnow()
    await db.flush()
    return True


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
