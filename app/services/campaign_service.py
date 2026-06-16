"""User-initiated WhatsApp campaign engine.

Sits beside ``notification_service`` and reuses its proven send-and-persist path verbatim
(``build_variable_context``, ``_resolve_body_params``, ``_is_opted_out``,
``resolve_template_send_header_media``, ``cloud.send_template``, the ``whatsappCrud`` outbound
helpers). The differences from notifications: campaigns are many, target a *segment* via a
declarative ``audience_spec``, freeze that audience into ``campaign_recipients`` (which doubles
as the idempotency + tracking ledger), send on a schedule with throttling, and attribute
conversions to ``payments``.

Robustness mirrors notifications:
* **Idempotent** — each recipient is a unique ``dedup_key`` claimed with an atomic
  compare-and-set before sending, so re-runs and concurrent workers never double-send.
* **Respectful** — only APPROVED templates send; members who revoked WhatsApp consent are
  skipped (and the skip is recorded, not hidden).
* **Resumable** — only ``pending``/``failed`` recipients are picked up, so a paused/crashed
  run resumes by simply re-running.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.crud import campaignsCrud as crud
from app.crud import whatsappCrud as chat_crud
from app.crud import whatsappTemplatesCrud as templates_crud
from app.db.postgresql import async_session_factory
from app.models import (
    Campaign,
    CampaignRecipient,
    CampaignVariant,
    MembershipSubscription,
    People,
)
from app.models.campaignsModel import (
    OBJECTIVE_BROADCAST,
    OBJECTIVE_ENGAGEMENT,
    OBJECTIVE_RENEWAL_PUSH,
    OBJECTIVE_WIN_BACK,
    STATUS_CANCELED,
    STATUS_COMPLETED,
    STATUS_PAUSED,
    STATUS_SCHEDULED,
    STATUS_SENDING,
)
from app.services import segmentation_service
from app.services import whatsapp_cloud_service as cloud
from app.services import whatsapp_media_assets_service as media_service
from app.services.notification_service import (
    VARIABLES,
    _is_opted_out,
    _resolve_body_params,
    build_variable_context,
)
from app.services.whatsapp_template_components import render_template_text
from app.services.whatsapp_template_send_media import resolve_template_send_header_media

logger = logging.getLogger(__name__)

# Meta error codes that mean "back off" rather than "this recipient failed".
_RATE_LIMIT_CODES = {130429, 131048, 131056, 80007}


class _RateLimited(Exception):
    """Raised mid-dispatch when Meta signals a rate limit; the run pauses and can resume."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Catalog (single source of truth, shared with the frontend wizard)
# ---------------------------------------------------------------------------
# Every objective targets members and can resolve the same member-based variables.
_MEMBER_VARIABLE_KEYS = list(VARIABLES.keys())

CAMPAIGN_OBJECTIVES: Dict[str, Dict[str, Any]] = {
    OBJECTIVE_WIN_BACK: {
        "label": "Reactivación / Win-back (socios vencidos)",
        "variables": _MEMBER_VARIABLE_KEYS,
    },
    OBJECTIVE_RENEWAL_PUSH: {
        "label": "Empuje de renovación (por vencer)",
        "variables": _MEMBER_VARIABLE_KEYS,
    },
    OBJECTIVE_ENGAGEMENT: {
        "label": "Engagement (socios activos)",
        "variables": _MEMBER_VARIABLE_KEYS,
    },
    OBJECTIVE_BROADCAST: {
        "label": "Difusión general",
        "variables": _MEMBER_VARIABLE_KEYS,
    },
}

# Audience predicate descriptors for the frontend segment builder.
AUDIENCE_PREDICATES: List[Dict[str, Any]] = [
    {
        "type": "membership_status",
        "label": "Estado de membresía",
        "kind": "multi_enum",
        "options": ["active", "expired", "pending", "canceled"],
    },
    {
        "type": "membership_end_at",
        "label": "Vencimiento (días desde hoy)",
        "kind": "range_days",
        "hint": "Negativo = en el pasado. Win-back típico: [-90, -7]. Por vencer: [0, 7].",
    },
    {
        "type": "plan_id",
        "label": "Plan(es) de membresía",
        "kind": "multi_id",
    },
    {
        "type": "last_activity",
        "label": "Última actividad (reservas)",
        "kind": "days_op",
        "hint": "older_than_days = inactivo desde hace N días.",
    },
]


def allowed_variables_for(objective: str) -> set:
    meta = CAMPAIGN_OBJECTIVES.get(objective)
    return set(meta.get("variables", [])) if meta else set(_MEMBER_VARIABLE_KEYS)


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------
async def _load_subscription(
    db: AsyncSession, subscription_id: int
) -> Optional[MembershipSubscription]:
    stmt = (
        select(MembershipSubscription)
        .options(selectinload(MembershipSubscription.plan))
        .where(MembershipSubscription.id == subscription_id)
    )
    return (await db.execute(stmt)).scalars().first()


def _variant_template_id(campaign: Campaign, variant: Optional[CampaignVariant]) -> Optional[int]:
    if variant is not None and variant.template_id:
        return variant.template_id
    return campaign.template_id


def _variant_param_mapping(campaign: Campaign, variant: Optional[CampaignVariant]):
    if variant is not None and variant.param_mapping is not None:
        return variant.param_mapping
    return campaign.param_mapping


def _variant_media(campaign: Campaign, variant: Optional[CampaignVariant]):
    if variant is not None and (variant.header_media_asset_id or variant.header_media_url):
        return variant.header_media_asset_id, variant.header_media_url
    return campaign.header_media_asset_id, campaign.header_media_url


# ---------------------------------------------------------------------------
# Build phase — materialize the audience snapshot
# ---------------------------------------------------------------------------
async def build_campaign_audience(db: AsyncSession, campaign_id: int) -> Dict[str, int]:
    """Resolve the audience and (idempotently) write ``campaign_recipients`` rows.

    Candidates failing consent / recency / phone checks are recorded as ``skipped`` with a
    reason rather than dropped. Returns counts by outcome.
    """
    stats = {"targeted": 0, "pending": 0, "skipped": 0}
    campaign = await crud.get_campaign_model(db, campaign_id)
    if campaign is None:
        return stats

    variant = await crud.ensure_default_variant(db, campaign_id, commit=False)
    candidates = await segmentation_service.resolve_candidates(db, campaign.audience_spec)
    blocked = await crud.recently_targeted_person_ids(
        db, days=campaign.recency_block_days, exclude_campaign_id=campaign_id
    )

    for cand in candidates:
        person = cand.person
        raw_phone = (person.phone_number or person.wa_id or "").strip()
        wa_id = re.sub(r"\D", "", raw_phone)

        status = "pending"
        skip_reason: Optional[str] = None
        if not wa_id:
            status, skip_reason = "skipped", "no_phone"
        elif person.id in blocked:
            status, skip_reason = "skipped", "recency_block"
        elif await _is_opted_out(db, person.id):
            status, skip_reason = "skipped", "no_consent"

        inserted = await crud.insert_recipient(
            db,
            campaign_id=campaign_id,
            dedup_key=f"campaign:{campaign_id}:{person.id}",
            variant_id=variant.id,
            person_id=person.id,
            subscription_id=cand.subscription.id if cand.subscription else None,
            phone_e164=raw_phone or None,
            wa_id=wa_id or None,
            status=status,
            skip_reason=skip_reason,
        )
        if inserted is None:
            continue  # already in the snapshot
        stats["targeted"] += 1
        stats["skipped" if status == "skipped" else "pending"] += 1

    await db.commit()
    return stats


# ---------------------------------------------------------------------------
# Send one recipient (mirror of notification_service.dispatch inner block)
# ---------------------------------------------------------------------------
async def _send_to_recipient(
    db: AsyncSession,
    campaign: Campaign,
    variant: Optional[CampaignVariant],
    recipient: CampaignRecipient,
) -> str:
    """Send the campaign template to one claimed recipient. Returns 'sent' | 'failed' | 'opted_out'.

    Raises ``_RateLimited`` if Meta signals a rate limit (the caller pauses the run).
    """
    template_id = _variant_template_id(campaign, variant)
    if not template_id:
        await crud.mark_recipient_failed(db, recipient, error="Campaña sin plantilla.")
        return "failed"

    tpl = await templates_crud.get_template_model(db, template_id)
    if tpl is None or (tpl.template_status or "").upper() != "APPROVED":
        await crud.mark_recipient_failed(
            db, recipient, error="La plantilla no está disponible o no está aprobada."
        )
        return "failed"

    if recipient.person_id is None:
        await crud.mark_recipient_failed(db, recipient, error="Destinatario sin persona.")
        return "failed"

    person = await db.get(People, recipient.person_id)
    if person is None:
        await crud.mark_recipient_failed(db, recipient, error="No se encontró la persona.")
        return "failed"

    # Last-moment consent re-check (consent may have been revoked after the build).
    if await _is_opted_out(db, person.id):
        await crud.mark_recipient_terminal(
            db, recipient, status="opted_out", skip_reason="no_consent"
        )
        return "opted_out"

    subscription = (
        await _load_subscription(db, recipient.subscription_id)
        if recipient.subscription_id is not None
        else None
    )
    plan = subscription.plan if subscription is not None else None
    context = build_variable_context(person, subscription, plan)
    body_params = _resolve_body_params(_variant_param_mapping(campaign, variant), context)

    media_asset_id, media_url = _variant_media(campaign, variant)
    try:
        resolved_media = await resolve_template_send_header_media(
            db,
            template=tpl,
            override_media_asset_id=media_asset_id,
            legacy_header_media_url=media_url,
        )
    except media_service.MediaAssetError as exc:
        await crud.mark_recipient_failed(db, recipient, error=str(exc))
        return "failed"

    to = recipient.wa_id or re.sub(r"\D", "", (person.phone_number or person.wa_id or ""))
    try:
        contact = await chat_crud.upsert_contact(
            db, wa_id=to, phone_number=recipient.phone_e164 or to, authoritative=False
        )
        conversation = await chat_crud.get_or_open_conversation(db, contact.id)
        result = await cloud.send_template(
            to=contact.wa_id,
            template_name=tpl.template_name,
            language_code=tpl.template_language,
            body_params=body_params,
            components=tpl.components,
            header_media_url=resolved_media.media_url,
            header_media_id=resolved_media.media_id,
        )
    except cloud.WhatsAppError as e:
        if e.code in _RATE_LIMIT_CODES:
            await db.rollback()
            raise _RateLimited(e.message)
        await crud.mark_recipient_failed(db, recipient, error=e.message)
        return "failed"

    message = await chat_crud.insert_outbound_message(
        db,
        conversation_id=conversation.id,
        contact_id=contact.id,
        text=render_template_text(tpl.components, body_params) or tpl.template_name,
        wa_message_id=result.get("wa_message_id"),
        message_type="template",
        template_id=tpl.id,
    )
    if resolved_media.media_url and resolved_media.media_format:
        await chat_crud.insert_outbound_media(
            db,
            message_id=message.id,
            media_type=resolved_media.media_format.lower(),
            mime_type=None,
            filename=None,
            file_size=None,
            sha256=None,
            media_url=resolved_media.media_url,
            cloud_media_id=resolved_media.media_id,
        )
    await crud.mark_recipient_sent(
        db, recipient, wa_message_id=result.get("wa_message_id"), message_id=message.id
    )
    return "sent"


# ---------------------------------------------------------------------------
# Dispatch phase — run a campaign
# ---------------------------------------------------------------------------
async def _current_status(db: AsyncSession, campaign_id: int) -> Optional[str]:
    return (
        await db.execute(select(Campaign.status).where(Campaign.id == campaign_id))
    ).scalar_one_or_none()


async def run_campaign(campaign_id: int, *, dry_run: bool = False) -> Dict[str, Any]:
    """Build (if needed) and dispatch a campaign in its own session.

    ``dry_run`` renders a sample and sends nothing. Safe to re-run (idempotent resume).
    """
    async with async_session_factory() as db:
        campaign = await crud.get_campaign_model(db, campaign_id)
        if campaign is None:
            return {"ok": False, "error": "Campaña no encontrada."}

        counts = await crud.recipient_status_counts(db, campaign_id)
        if not counts:
            await build_campaign_audience(db, campaign_id)
            campaign = await crud.get_campaign_model(db, campaign_id)

        variant = await crud.ensure_default_variant(db, campaign_id)

        if dry_run:
            return await _dry_run_preview(db, campaign, variant)

        await crud.set_campaign_status(
            db, campaign, status=STATUS_SENDING, started_at=campaign.started_at or _now()
        )

        interval = 60.0 / max(int(campaign.throttle_per_minute or 60), 1)
        stats = {"sent": 0, "failed": 0, "skipped": 0}
        ids = await crud.list_sendable_recipient_ids(db, campaign_id)
        paused = False

        for index, rid in enumerate(ids):
            status_now = await _current_status(db, campaign_id)
            if status_now in (STATUS_PAUSED, STATUS_CANCELED):
                paused = status_now == STATUS_PAUSED
                break

            claimed = await crud.claim_recipient_for_send(db, rid)
            await db.commit()
            if not claimed:
                continue

            recipient = await crud.get_recipient_model(db, rid)
            try:
                outcome = await _send_to_recipient(db, campaign, variant, recipient)
            except _RateLimited as exc:
                logger.warning("campaign %s paused (rate limit): %s", campaign_id, exc)
                recipient = await crud.get_recipient_model(db, rid)
                await crud.mark_recipient_failed(db, recipient, error=f"rate_limited: {exc}")
                paused = True
                break

            if outcome == "sent":
                stats["sent"] += 1
            elif outcome == "failed":
                stats["failed"] += 1
            else:
                stats["skipped"] += 1

            if index < len(ids) - 1:
                await asyncio.sleep(interval)

        fresh = await crud.get_campaign_model(db, campaign_id)
        if fresh is not None and fresh.status == STATUS_SENDING:
            await crud.set_campaign_status(
                db,
                fresh,
                status=STATUS_PAUSED if paused else STATUS_COMPLETED,
                finished_at=None if paused else _now(),
            )
        return {"ok": True, "paused": paused, **stats}


async def _dry_run_preview(
    db: AsyncSession, campaign: Campaign, variant: Optional[CampaignVariant]
) -> Dict[str, Any]:
    template_id = _variant_template_id(campaign, variant)
    rendered = ""
    if template_id:
        tpl = await templates_crud.get_template_model(db, template_id)
        if tpl is not None:
            sample_context = {key: meta.get("sample", "") for key, meta in VARIABLES.items()}
            body_params = _resolve_body_params(
                _variant_param_mapping(campaign, variant), sample_context
            )
            rendered = render_template_text(tpl.components, body_params) or tpl.template_name
    counts = await crud.recipient_status_counts(db, campaign.id)
    return {
        "ok": True,
        "dry_run": True,
        "rendered_preview": rendered,
        "pending": counts.get("pending", 0),
        "skipped": counts.get("skipped", 0),
    }


async def trigger_in_background(campaign_id: int) -> None:
    """Run a campaign from a fire-after-commit ``asyncio.create_task``. Swallows errors."""
    try:
        result = await run_campaign(campaign_id)
        logger.info("campaign %s dispatch -> %s", campaign_id, result)
    except Exception:  # noqa: BLE001
        logger.exception("campaign %s background dispatch failed", campaign_id)


# ---------------------------------------------------------------------------
# Scheduled sweeps (piggyback the notification APScheduler)
# ---------------------------------------------------------------------------
async def run_campaign_sweep() -> Dict[str, int]:
    """Dispatch every scheduled campaign whose time has arrived."""
    stats = {"campaigns": 0, "sent": 0, "failed": 0}
    async with async_session_factory() as db:
        due = await crud.campaigns_due_for_send(db, _now())
        due_ids = []
        for campaign in due:
            # Flip out of 'scheduled' so a second worker won't also pick it up.
            await crud.set_campaign_status(
                db, campaign, status=STATUS_SENDING, started_at=campaign.started_at or _now()
            )
            due_ids.append(campaign.id)

    for campaign_id in due_ids:
        result = await run_campaign(campaign_id)
        stats["campaigns"] += 1
        stats["sent"] += int(result.get("sent", 0))
        stats["failed"] += int(result.get("failed", 0))
    return stats


async def _check_conversion(
    db: AsyncSession, campaign: Campaign, recipient: CampaignRecipient
) -> Optional[tuple]:
    """Return (payment_id_or_None, converted_at) if the recipient converted, else None."""
    if recipient.sent_at is None or recipient.person_id is None:
        return None
    start = recipient.sent_at
    end = start + timedelta(days=max(int(campaign.conversion_window_days or 14), 0))
    metric = (campaign.conversion_metric or "payment").lower()

    if metric == "reservation":
        if await crud.has_reservation_in_window(db, recipient.person_id, start=start, end=end):
            return (None, _now())
        return None

    payment = await crud.find_first_completed_payment(
        db, recipient.person_id, start=start, end=end
    )
    if metric == "renewal":
        if payment is not None and await crud.has_new_subscription_since(
            db, recipient.person_id, since=start
        ):
            return (payment.id, payment.paid_at)
        return None

    # default 'payment'
    if payment is not None:
        return (payment.id, payment.paid_at)
    return None


async def run_conversion_sweep() -> Dict[str, int]:
    """Attribute conversions (payments) to recipients still inside their window."""
    stats = {"checked": 0, "converted": 0}
    async with async_session_factory() as db:
        campaigns = await crud.campaigns_with_open_conversion_window(db)
        for campaign in campaigns:
            recipients = await crud.list_recipients_pending_conversion(
                db, campaign.id, window_days=campaign.conversion_window_days
            )
            for recipient in recipients:
                stats["checked"] += 1
                outcome = await _check_conversion(db, campaign, recipient)
                if outcome is not None:
                    payment_id, when = outcome
                    await crud.mark_recipient_converted(
                        db, recipient, payment_id=payment_id, converted_at=when, commit=False
                    )
                    stats["converted"] += 1
        await db.commit()
    return stats


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
async def get_metrics(db: AsyncSession, campaign_id: int) -> Dict[str, Any]:
    """Single-table aggregation over ``campaign_recipients`` + attributed revenue."""
    counts = await crud.recipient_status_counts(db, campaign_id)

    skipped = counts.get("skipped", 0)
    # Anyone who was actually contacted (claimed and not skipped/pending).
    sent = sum(counts.get(s, 0) for s in ("sent", "delivered", "read", "replied"))
    delivered = sum(counts.get(s, 0) for s in ("delivered", "read", "replied"))
    read = sum(counts.get(s, 0) for s in ("read", "replied"))
    replied = counts.get("replied", 0)
    failed = counts.get("failed", 0)
    opted_out = counts.get("opted_out", 0)
    pending = counts.get("pending", 0) + counts.get("sending", 0)

    # Conversions are tracked on the recipient rows regardless of current delivery status.
    converted = await _count_converted(db, campaign_id)
    revenue = await crud.conversion_revenue(db, campaign_id)

    def rate(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator else 0.0

    targeted = sum(counts.values())
    return {
        "targeted": targeted,
        "pending": pending,
        "sent": sent,
        "delivered": delivered,
        "read": read,
        "replied": replied,
        "failed": failed,
        "skipped": skipped,
        "opted_out": opted_out,
        "converted": converted,
        "delivery_rate": rate(delivered, sent),
        "read_rate": rate(read, sent),
        "reply_rate": rate(replied, sent),
        "conversion_rate": rate(converted, sent),
        "revenue_recovered": float(revenue or Decimal(0)),
    }


async def _count_converted(db: AsyncSession, campaign_id: int) -> int:
    from sqlalchemy import func  # local import to keep module header lean

    stmt = (
        select(func.count())
        .select_from(CampaignRecipient)
        .where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.converted.is_(True),
        )
    )
    return int((await db.execute(stmt)).scalar_one())
