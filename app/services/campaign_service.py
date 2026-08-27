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
import os
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
from app.services import attendance_profile_service
from app.services import segmentation_service
from app.services import whatsapp_cloud_service as cloud
from app.services import whatsapp_media_assets_service as media_service
from app.services import whatsapp_outbound as outbound
from app.services.notification_service import (
    VARIABLES,
    _is_opted_out,
    _resolve_body_params,
    _resolve_param_key,
    build_variable_context,
)
from app.services.whatsapp_template_components import render_template_text
from app.services.whatsapp_template_send_media import resolve_template_send_header_media

logger = logging.getLogger(__name__)

# Meta error codes that mean "back off" rather than "this recipient failed".
_RATE_LIMIT_CODES = {130429, 131048, 131056, 80007}

# How long a recipient deferred mid-purchase waits before the bot conversation is retried.
_PENDING_ACTION_RETRY_MINUTES = 30

# How long a rate-limited recipient waits before becoming eligible again.
_RATE_LIMIT_BACKOFF_SECONDS = 120

# Batches one direct ``run_campaign`` call will process before handing back to the sweep.
# A cap, not a target: the loop stops as soon as nothing is due.
_MAX_BATCHES_PER_RUN = 50


def _env_int(name: str, default: int, *, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(max(value, low), high)


def _concurrency() -> int:
    """Sends in flight at once.

    Kept small on purpose: each send holds a database session, and the engine pool is
    5 + 10 overflow. Throughput comes from not sleeping between sends, not from a wide fan-out.
    """
    return _env_int("CAMPAIGN_SEND_CONCURRENCY", 5, low=1, high=10)


def _batch_size(throttle_per_minute: Optional[int] = None) -> int:
    """How many recipients one slice claims.

    Capped by what the throttle can actually deliver in one sweep interval, so a slice takes
    roughly one tick and the scheduler keeps its rhythm instead of one campaign sitting in
    the single job slot for an hour.
    """
    configured = _env_int("CAMPAIGN_BATCH_SIZE", 200, low=1, high=2000)
    rate = max(int(throttle_per_minute or 60), 1)
    sweep_minutes = _env_int("CAMPAIGN_SWEEP_INTERVAL_MIN", 1, low=1, high=60)
    return max(1, min(configured, rate * sweep_minutes))


class _RateGate:
    """Spaces out send starts so a batch honours ``throttle_per_minute``.

    A plain ``sleep`` between sends would serialise the batch and make every message pay the
    previous one's network latency. Handing out timed slots instead lets sends overlap while
    the *rate* stays exactly where the campaign set it.
    """

    def __init__(self, per_minute: Optional[int]) -> None:
        self._interval = 60.0 / max(int(per_minute or 60), 1)
        self._lock = asyncio.Lock()
        self._next_slot = 0.0

    async def wait(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._interval
            delay = slot - now
        if delay > 0:
            await asyncio.sleep(delay)


class _RateLimited(Exception):
    """Raised mid-dispatch when Meta signals a rate limit; the run pauses and can resume."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Catalog (single source of truth, shared with the frontend wizard)
# ---------------------------------------------------------------------------
# Variables only a campaign can resolve. They are NOT added to ``notification_service.VARIABLES``
# because that catalog is shared with the event-driven notifications, which have no audience and
# therefore no frozen class affinity. Values come from the snapshot taken at build time.
CAMPAIGN_VARIABLES: Dict[str, Dict[str, str]] = {
    "favorite_class_name": {"label": "Clase que más reserva", "sample": "Spinning"},
    "favorite_class_day": {"label": "Día de esa clase", "sample": "lunes"},
    "favorite_class_time": {"label": "Hora de esa clase", "sample": "7:00 a. m."},
    "favorite_class_schedule": {
        "label": "Horario habitual (día y hora)", "sample": "lunes a las 7:00 a. m.",
    },
    "days_inactive": {"label": "Días de inactividad (vencida)", "sample": "84"},
    "kcal_not_burned": {"label": "Kcal no quemadas (estimado)", "sample": "8,700"},
    "kg_fat_equivalent": {"label": "Kg de grasa (equivalente estimado)", "sample": "1.1"},
}

# Estimación deliberadamente simple: no hay dato de intensidad/MET en class_types ni
# class_templates para derivar algo más preciso, y presentarlo como si lo hubiera violaría
# la propia advertencia de "no dar datos médicos/científicos exactos" que acompaña este tipo
# de mensaje motivacional. Un día inactivo se cuenta como una sesión perdida (aproximación,
# no la frecuencia real de asistencia del socio).
AVG_KCAL_PER_SESSION = 900
KCAL_PER_KG_FAT = 7700

# Every objective targets members and can resolve the same member-based variables.
_MEMBER_VARIABLE_KEYS = list(VARIABLES.keys()) + list(CAMPAIGN_VARIABLES.keys())

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
    {
        "type": "class_affinity",
        "label": "Clases que más reserva",
        "kind": "class_affinity",
        "modes": list(segmentation_service.CLASS_AFFINITY_MODES),
        "hint": (
            "groups: una entrada por actividad. Sin template_ids abarca la actividad completa "
            "(incluidas sesiones sueltas sin plantilla); con template_ids se acota a horarios "
            "concretos. mode=favorite exige que la selección concentre más reservas que "
            "cualquier otra actividad del socio; mode=attended solo pide min_reservations."
        ),
    },
]


def apply_favorite_class_variables(context: Dict[str, Any], favorite) -> Dict[str, Any]:
    """Add the class-affinity variables to a variable context.

    A member with no booking history resolves to empty strings rather than a placeholder:
    inventing a class name would put a lie in a marketing message.
    """
    context["favorite_class_name"] = favorite.class_type_name if favorite else ""
    context["favorite_class_day"] = favorite.day_label if favorite else ""
    context["favorite_class_time"] = favorite.time_label if favorite else ""
    context["favorite_class_schedule"] = favorite.schedule_text if favorite else ""
    return context


def apply_inactivity_variables(
    context: Dict[str, Any], subscription, *, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """Days since the membership lapsed, plus its motivational kcal/kg-fat translation.

    Same rule as ``apply_favorite_class_variables``: no real data means empty strings, never
    an invented number. A member whose subscription hasn't actually expired yet (or has none)
    isn't "inactive" in this sense.
    """
    now = now or _now()
    end_at = getattr(subscription, "end_at", None)
    days_inactive: Optional[int] = None
    if end_at is not None:
        end_aware = end_at if end_at.tzinfo else end_at.replace(tzinfo=timezone.utc)
        delta = (now - end_aware).days
        if delta > 0:
            days_inactive = delta

    if days_inactive is None:
        context["days_inactive"] = ""
        context["kcal_not_burned"] = ""
        context["kg_fat_equivalent"] = ""
        return context

    kcal = days_inactive * AVG_KCAL_PER_SESSION
    kg_fat = kcal / KCAL_PER_KG_FAT
    context["days_inactive"] = str(days_inactive)
    context["kcal_not_burned"] = f"{round(kcal / 50) * 50:,}"
    context["kg_fat_equivalent"] = f"{kg_fat:.1f}"
    return context


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


def _variant_extra_params(campaign: Campaign, variant: Optional[CampaignVariant]):
    """(header_text_param_key, button_url_param_key, location_param) with variant fallback."""

    def pick(attr: str):
        if variant is not None and getattr(variant, attr, None) is not None:
            return getattr(variant, attr)
        return getattr(campaign, attr, None)

    return pick("header_text_param_key"), pick("button_url_param_key"), pick("location_param")


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
    # Resolve the whole audience's favourite class in two set-based queries and freeze it on
    # each row, so the message can name the class without an aggregate per recipient later.
    favorites = await attendance_profile_service.favorite_classes_for(
        db, [c.person.id for c in candidates]
    )

    # One consent query for the whole audience instead of one per candidate.
    opted_out = await crud.opted_out_person_ids(db, [c.person.id for c in candidates])

    rows: List[Dict[str, Any]] = []
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
        elif person.id in opted_out:
            status, skip_reason = "skipped", "no_consent"

        favorite = favorites.get(person.id)
        rows.append(
            {
                "campaign_id": campaign_id,
                "dedup_key": f"campaign:{campaign_id}:{person.id}",
                "variant_id": variant.id,
                "person_id": person.id,
                "subscription_id": cand.subscription.id if cand.subscription else None,
                "phone_e164": raw_phone or None,
                "wa_id": wa_id or None,
                "status": status,
                "skip_reason": skip_reason,
                "favorite_class_type_id": favorite.class_type_id if favorite else None,
                "favorite_class_template_id": (
                    favorite.class_template_id if favorite else None
                ),
            }
        )
        stats["skipped" if status == "skipped" else "pending"] += 1

    # Rows already in the snapshot are ignored (ON CONFLICT), so re-building is idempotent.
    stats["targeted"] = await crud.insert_recipients_bulk(db, rows)

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
    favorite=None,
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
    context = apply_favorite_class_variables(
        build_variable_context(person, subscription, plan), favorite
    )
    context = apply_inactivity_variables(context, subscription)
    body_params = _resolve_body_params(_variant_param_mapping(campaign, variant), context)
    header_text_key, button_url_key, location_param = _variant_extra_params(campaign, variant)
    header_text_param = _resolve_param_key(header_text_key, context)
    button_url_param = _resolve_param_key(button_url_key, context)

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
    contact = await chat_crud.upsert_contact(
        db, wa_id=to, phone_number=recipient.phone_e164 or to, authoritative=False
    )
    conversation = await chat_crud.get_or_open_conversation(db, contact.id)
    # Route through the unified outbound gateway: per-contact serialization + marketing gates
    # (consent / mid-purchase defer / quiet hours / daily cap). persist=False -> we keep the
    # richer campaign persistence (message + media + recipient ledger) below.
    gw = await outbound.send_template(
        db,
        kind=outbound.KIND_CAMPAIGN,
        message_class=outbound.CLASS_MARKETING,
        conversation_id=conversation.id,
        contact_id=contact.id,
        wa_id=contact.wa_id,
        template_name=tpl.template_name,
        language_code=tpl.template_language,
        body_params=body_params,
        components=tpl.components,
        header_media_url=resolved_media.media_url,
        header_media_id=resolved_media.media_id,
        header_text_param=header_text_param,
        location=location_param,
        button_url_param=button_url_param,
        persist=False,
        person_id=person.id,
    )
    if gw.status is outbound.SendStatus.SUPPRESSED:
        if gw.reason == "no_consent":
            await crud.mark_recipient_terminal(
                db, recipient, status="opted_out", skip_reason="no_consent"
            )
            return "opted_out"
        # daily_cap: contact already got a marketing message today -> terminal skip for this run.
        await crud.mark_recipient_terminal(
            db, recipient, status="skipped", skip_reason=gw.reason or "suppressed"
        )
        return "skipped"
    if gw.status is outbound.SendStatus.DEFERRED:
        if gw.reason == "rate_limited":
            await db.rollback()
            raise _RateLimited("rate limited")
        # quiet_hours / pending_action: nothing failed, the moment was just not allowed. Park
        # the recipient until it is, instead of marking it failed (which both misreports the
        # campaign and makes the very next sweep retry it into the same wall).
        if gw.reason == "quiet_hours":
            eligible_at = outbound.next_allowed_send_at()
        else:
            # Mid-purchase with the bot: give the conversation room to finish.
            eligible_at = _now() + timedelta(minutes=_PENDING_ACTION_RETRY_MINUTES)
        await crud.defer_recipient(
            db, recipient, send_after=eligible_at, reason=gw.reason or "deferred"
        )
        return "deferred"
    if gw.status is outbound.SendStatus.FAILED:
        await crud.mark_recipient_failed(db, recipient, error=gw.reason or "Error al enviar.")
        return "failed"
    result = {"wa_message_id": gw.wa_message_id}

    message = await chat_crud.insert_outbound_message(
        db,
        conversation_id=conversation.id,
        contact_id=contact.id,
        text=render_template_text(tpl.components, body_params) or tpl.template_name,
        wa_message_id=result.get("wa_message_id"),
        message_type="template",
        template_id=tpl.id,
        message_class="marketing",  # counted by the marketing frequency cap (Phase 2)
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


async def run_campaign(
    campaign_id: int, *, dry_run: bool = False, max_batches: Optional[int] = None
) -> Dict[str, Any]:
    """Build (if needed) and dispatch a campaign in bounded, resumable slices.

    Each slice claims a batch of recipients that are due *now*, sends them with bounded
    concurrency under the campaign throttle, and returns. Nothing here loops for the length
    of the audience, so a restart costs at most one in-flight batch and the next sweep picks
    the campaign straight back up.

    ``max_batches`` caps how many batches a single call processes — the scheduler passes 1 so
    one large campaign cannot monopolise the sweep. ``dry_run`` renders a sample and sends
    nothing. Safe to re-run: every recipient is claimed with a compare-and-set.
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
        # Detached copies: each send worker opens its own session and only reads these.
        db.expunge(campaign)
        if variant is not None:
            db.expunge(variant)

    stats = {"sent": 0, "failed": 0, "skipped": 0, "deferred": 0, "batches": 0}
    gate = _RateGate(campaign.throttle_per_minute)
    limit = _batch_size(campaign.throttle_per_minute)
    paused = False
    budget = max_batches if max_batches is not None else _MAX_BATCHES_PER_RUN

    while stats["batches"] < budget:
        outcome = await _dispatch_slice(campaign, variant, gate, limit)
        stats["batches"] += 1
        for key in ("sent", "failed", "skipped", "deferred"):
            stats[key] += outcome[key]
        if outcome["paused"]:
            paused = True
            break
        if outcome["stopped"] or outcome["claimed"] == 0:
            break

    await _settle_campaign(campaign_id, paused=paused)
    # One structured line per run: enough to explain a slow or stalled campaign without
    # having to reconstruct it from per-message logs.
    logger.info(
        "campaign %s run: batches=%s sent=%s failed=%s skipped=%s deferred=%s paused=%s",
        campaign_id,
        stats["batches"],
        stats["sent"],
        stats["failed"],
        stats["skipped"],
        stats["deferred"],
        paused,
    )
    return {"ok": True, "paused": paused, "deferred": bool(stats["deferred"]), **stats}


async def _dispatch_slice(
    campaign: Campaign,
    variant: Optional[CampaignVariant],
    gate: "_RateGate",
    limit: int,
) -> Dict[str, Any]:
    """Claim one batch of due recipients and send it. Returns per-batch counters."""
    result = {
        "claimed": 0, "sent": 0, "failed": 0, "skipped": 0, "deferred": 0,
        "paused": False, "stopped": False,
    }
    async with async_session_factory() as db:
        status_now = await _current_status(db, campaign.id)
        if status_now in (STATUS_PAUSED, STATUS_CANCELED):
            result["stopped"] = True
            result["paused"] = status_now == STATUS_PAUSED
            return result

        claimed = await crud.claim_recipient_batch(db, campaign.id, limit)
        await crud.touch_campaign_heartbeat(db, campaign.id)
        await db.commit()
        if not claimed:
            return result
        result["claimed"] = len(claimed)

        # Class labels for the whole batch in one pass, never per message.
        claimed_ids = set(claimed)
        refs = [
            row
            for row in await crud.list_recipient_favorite_refs(db, campaign.id)
            if row.id in claimed_ids
        ]
        favorites = await attendance_profile_service.favorite_class_for_recipients(db, refs)

    semaphore = asyncio.Semaphore(_concurrency())
    outcomes = await asyncio.gather(
        *(
            _send_one(campaign, variant, rid, favorites.get(rid), gate, semaphore)
            for rid in claimed
        )
    )
    for outcome in outcomes:
        if outcome == "sent":
            result["sent"] += 1
        elif outcome == "failed":
            result["failed"] += 1
        elif outcome == "rate_limited":
            result["paused"] = True
        else:
            result["skipped"] += 1
            if outcome == "deferred":
                result["deferred"] += 1
    return result


async def _send_one(
    campaign: Campaign,
    variant: Optional[CampaignVariant],
    recipient_id: int,
    favorite,
    gate: "_RateGate",
    semaphore: asyncio.Semaphore,
) -> str:
    """Send to one already-claimed recipient, in its own short-lived session."""
    async with semaphore:
        await gate.wait()
        async with async_session_factory() as db:
            recipient = await crud.get_recipient_model(db, recipient_id)
            if recipient is None:
                return "skipped"
            try:
                return await _send_to_recipient(db, campaign, variant, recipient, favorite)
            except _RateLimited as exc:
                logger.warning("campaign %s rate limited: %s", campaign.id, exc)
                # Not this recipient's fault: put it back in the queue, due after a backoff.
                await db.rollback()
                recipient = await crud.get_recipient_model(db, recipient_id)
                if recipient is not None:
                    await crud.defer_recipient(
                        db,
                        recipient,
                        send_after=_now() + timedelta(seconds=_RATE_LIMIT_BACKOFF_SECONDS),
                        reason="rate_limited",
                    )
                return "rate_limited"
            except Exception:  # noqa: BLE001 - one bad recipient must not kill the batch
                logger.exception(
                    "campaign %s: send failed for recipient %s", campaign.id, recipient_id
                )
                await db.rollback()
                recipient = await crud.get_recipient_model(db, recipient_id)
                if recipient is not None:
                    await crud.mark_recipient_failed(db, recipient, error="Error interno.")
                return "failed"


async def _settle_campaign(campaign_id: int, *, paused: bool) -> None:
    """Decide what a campaign's status should be now that a run has stopped."""
    async with async_session_factory() as db:
        fresh = await crud.get_campaign_model(db, campaign_id)
        if fresh is None or fresh.status != STATUS_SENDING:
            return
        if paused:
            await crud.set_campaign_status(db, fresh, status=STATUS_PAUSED)
            return

        # Anyone still due right now means the run was cut short by its batch budget, not
        # finished: leave it scheduled so the next sweep continues immediately.
        if await crud.list_sendable_recipient_ids(db, campaign_id):
            await crud.set_campaign_status(
                db, fresh, status=STATUS_SCHEDULED, scheduled_at=_now()
            )
            return

        waiting_at = await crud.next_send_after(db, campaign_id)
        if waiting_at is not None:
            # Deferred recipients (quiet hours / mid-purchase / backoff) are neither failures
            # nor losses: reschedule for the instant the first of them becomes eligible.
            await crud.set_campaign_status(
                db, fresh, status=STATUS_SCHEDULED, scheduled_at=waiting_at
            )
            return

        await crud.set_campaign_status(
            db, fresh, status=STATUS_COMPLETED, finished_at=_now()
        )


async def _dry_run_preview(
    db: AsyncSession, campaign: Campaign, variant: Optional[CampaignVariant]
) -> Dict[str, Any]:
    template_id = _variant_template_id(campaign, variant)
    rendered = ""
    if template_id:
        tpl = await templates_crud.get_template_model(db, template_id)
        if tpl is not None:
            sample_context = {
                key: meta.get("sample", "")
                for key, meta in {**VARIABLES, **CAMPAIGN_VARIABLES}.items()
            }
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
    """Advance every campaign that is due — one bounded slice each.

    Each campaign gets a single batch per tick rather than being run to completion, so a
    10,000-recipient blast cannot sit in the scheduler's only job slot while every other
    campaign waits. A campaign that still has work left re-schedules itself for now and is
    picked up again on the next tick.
    """
    stats = {"campaigns": 0, "sent": 0, "failed": 0, "deferred": 0}
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
        result = await run_campaign(campaign_id, max_batches=1)
        stats["campaigns"] += 1
        stats["sent"] += int(result.get("sent", 0))
        stats["failed"] += int(result.get("failed", 0))
        stats["deferred"] += int(result.get("deferred", 0))
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
    """Attribute conversions to recipients still inside their window.

    The common case — ``conversion_metric == 'payment'`` — is a single UPDATE per campaign
    rather than one or two queries per recipient. The reservation/renewal metrics still walk
    their recipients, because both need a second table checked per person; they are the rare
    configurations, so the loop stays where it is genuinely needed.
    """
    stats = {"checked": 0, "converted": 0, "campaigns": 0}
    async with async_session_factory() as db:
        campaigns = await crud.campaigns_with_open_conversion_window(db)
        for campaign in campaigns:
            stats["campaigns"] += 1
            metric = (campaign.conversion_metric or "payment").lower()

            if metric == "payment":
                stats["converted"] += await crud.attribute_payment_conversions(
                    db, campaign.id, window_days=campaign.conversion_window_days
                )
                continue

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
    logger.info("campaign conversion sweep: %s", stats)
    return stats


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def _metrics_from(
    counts: Dict[str, int], converted: int, revenue: Optional[Decimal]
) -> Dict[str, Any]:
    """Shape one campaign's metrics from its status histogram.

    Pure, so the single-campaign and whole-page paths cannot drift apart in how they define
    "sent" or compute a rate.
    """
    skipped = counts.get("skipped", 0)
    # Anyone who was actually contacted (claimed and not skipped/pending).
    sent = sum(counts.get(s, 0) for s in ("sent", "delivered", "read", "replied"))
    delivered = sum(counts.get(s, 0) for s in ("delivered", "read", "replied"))
    read = sum(counts.get(s, 0) for s in ("read", "replied"))
    replied = counts.get("replied", 0)
    failed = counts.get("failed", 0)
    opted_out = counts.get("opted_out", 0)
    pending = counts.get("pending", 0) + counts.get("sending", 0)

    def rate(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator else 0.0

    return {
        "targeted": sum(counts.values()),
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


async def get_metrics(db: AsyncSession, campaign_id: int) -> Dict[str, Any]:
    """Single-table aggregation over ``campaign_recipients`` + attributed revenue."""
    counts = await crud.recipient_status_counts(db, campaign_id)
    # Conversions are tracked on the recipient rows regardless of current delivery status.
    converted = await _count_converted(db, campaign_id)
    revenue = await crud.conversion_revenue(db, campaign_id)
    return _metrics_from(counts, converted, revenue)


async def get_metrics_batch(
    db: AsyncSession, campaign_ids: List[int]
) -> Dict[int, Dict[str, Any]]:
    """Metrics for many campaigns in three queries, not three per campaign.

    This is what lets the campaign list show real numbers: before, every row rendered "—"
    because there was no way to ask for the whole page at once.
    """
    ids = [int(c) for c in campaign_ids if c is not None]
    if not ids:
        return {}
    counts = await crud.recipient_status_counts_for(db, ids)
    converted = await crud.converted_counts_for(db, ids)
    revenue = await crud.conversion_revenue_for(db, ids)
    return {
        cid: _metrics_from(counts.get(cid, {}), converted.get(cid, 0), revenue.get(cid))
        for cid in ids
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
