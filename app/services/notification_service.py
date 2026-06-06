"""Automated WhatsApp notification dispatcher.

This is the heart of the renewal/registration notification system. Given a business event
and a member, it resolves the admin-configured template + variable mapping, builds the body
parameters from member data, and sends an approved Meta template — reusing the same proven
send-and-persist path as the manual ``send_template_test`` mutation.

Robustness guarantees:
* **Idempotent** — every attempt claims a unique ``dedup_key`` in ``notification_log``
  before sending, so a notification is never sent twice (safe across workers and retries).
* **Isolated** — triggers run in their own DB session, after the business transaction has
  committed, so a WhatsApp failure can never roll back an enrollment/renewal.
* **Respectful** — only APPROVED templates are sent, and members who revoked WhatsApp
  consent (``communications_opt_in``) are skipped.

The variable catalog (``VARIABLES`` / ``EVENT_TYPES``) is the single source of truth shared
with the frontend (exposed via the ``notificationCatalog`` GraphQL query) so the placeholder
pickers always match what the backend can actually resolve.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.crud import notificationsCrud as crud
from app.crud import whatsappCrud as chat_crud
from app.crud import whatsappTemplatesCrud as templates_crud
from app.db.postgresql import async_session_factory
from app.models import CommunicationOptIn, MembershipSubscription, People
from app.models.notificationModel import (
    EVENT_MEMBERSHIP_EXPIRED,
    EVENT_NEW_REGISTRATION,
    EVENT_RENEWAL_CONFIRMATION,
    EVENT_RENEWAL_REMINDER,
)
from app.services import whatsapp_cloud_service as cloud
from app.services.whatsapp_template_components import render_template_text

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo

    _LOCAL_TZ = ZoneInfo("America/Mexico_City")
except Exception:  # noqa: BLE001 - fallback if tzdata unavailable
    _LOCAL_TZ = timezone.utc

GYM_NAME = os.getenv("NOTIFICATION_GYM_NAME", os.getenv("GYM_NAME", "FitPilot"))


# ---------------------------------------------------------------------------
# Variable catalog (single source of truth, shared with the frontend)
# ---------------------------------------------------------------------------
VARIABLES: Dict[str, Dict[str, str]] = {
    "member_name": {"label": "Nombre completo del socio", "sample": "Juan Pérez"},
    "member_first_name": {"label": "Primer nombre del socio", "sample": "Juan"},
    "plan_name": {"label": "Nombre del plan", "sample": "Mensualidad"},
    "end_date": {"label": "Fecha de vencimiento", "sample": "15/07/2026"},
    "start_date": {"label": "Fecha de inicio", "sample": "15/06/2026"},
    "amount": {"label": "Monto del plan", "sample": "$500.00"},
    "days_left": {"label": "Días restantes", "sample": "7"},
    "gym_name": {"label": "Nombre del gimnasio", "sample": GYM_NAME},
}

EVENT_TYPES: Dict[str, Dict[str, Any]] = {
    EVENT_NEW_REGISTRATION: {
        "label": "Bienvenida nuevo registro",
        "variables": [
            "member_name", "member_first_name", "plan_name",
            "start_date", "end_date", "amount", "gym_name",
        ],
        "supports_offsets": False,
    },
    EVENT_RENEWAL_REMINDER: {
        "label": "Recordatorio de renovación",
        "variables": [
            "member_name", "member_first_name", "plan_name",
            "end_date", "days_left", "amount", "gym_name",
        ],
        "supports_offsets": True,
    },
    EVENT_RENEWAL_CONFIRMATION: {
        "label": "Confirmación de renovación",
        "variables": [
            "member_name", "member_first_name", "plan_name",
            "start_date", "end_date", "amount", "gym_name",
        ],
        "supports_offsets": False,
    },
    EVENT_MEMBERSHIP_EXPIRED: {
        "label": "Membresía vencida / reactivación",
        "variables": [
            "member_name", "member_first_name", "plan_name", "end_date", "gym_name",
        ],
        "supports_offsets": False,
    },
}

DEFAULT_REMINDER_OFFSETS = [7, 1]
# Don't blast win-back messages to memberships that lapsed long ago (e.g. on first deploy).
EXPIRED_WINDOW_DAYS = 2


# ---------------------------------------------------------------------------
# Variable resolution
# ---------------------------------------------------------------------------
def _fmt_date(value: Optional[datetime]) -> str:
    if not value:
        return ""
    dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_LOCAL_TZ).strftime("%d/%m/%Y")


def _fmt_amount(value: Optional[Any]) -> str:
    if value is None:
        return ""
    try:
        return f"${Decimal(str(value)):,.2f}"
    except Exception:  # noqa: BLE001
        return str(value)


def build_variable_context(
    person: People,
    subscription: Optional[MembershipSubscription] = None,
    plan: Optional[Any] = None,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, str]:
    """Resolve every catalog variable to its concrete string value for ``person``."""
    now = now or datetime.now(timezone.utc)
    full_name = (person.full_name or "").strip() or "socio"
    first_name = full_name.split()[0] if full_name else "socio"

    plan = plan or (subscription.plan if subscription is not None else None)
    plan_name = getattr(plan, "name", "") or ""
    amount = getattr(plan, "price", None)

    end_at = getattr(subscription, "end_at", None)
    start_at = getattr(subscription, "start_at", None)
    days_left = ""
    if end_at is not None:
        end_aware = end_at if end_at.tzinfo else end_at.replace(tzinfo=timezone.utc)
        delta_days = (end_aware - now).days
        days_left = str(max(delta_days, 0))

    return {
        "member_name": full_name,
        "member_first_name": first_name,
        "plan_name": plan_name,
        "end_date": _fmt_date(end_at),
        "start_date": _fmt_date(start_at),
        "amount": _fmt_amount(amount),
        "days_left": days_left,
        "gym_name": GYM_NAME,
    }


def _resolve_body_params(
    param_mapping: Optional[List[str]], context: Dict[str, str]
) -> Optional[List[str]]:
    if not param_mapping:
        return None
    return [str(context.get(key, "")) for key in param_mapping]


# ---------------------------------------------------------------------------
# Opt-out
# ---------------------------------------------------------------------------
async def _is_opted_out(db: AsyncSession, person_id: int) -> bool:
    """True only when the member's most recent WhatsApp consent record is revoked.

    Absence of any record means allowed (the member shared their number at signup and
    these are transactional UTILITY templates).
    """
    stmt = (
        select(CommunicationOptIn)
        .where(
            CommunicationOptIn.person_id == person_id,
            CommunicationOptIn.channel == "whatsapp",
        )
        .order_by(CommunicationOptIn.created_at.desc())
        .limit(1)
    )
    row = (await db.execute(stmt)).scalars().first()
    if row is None:
        return False
    if row.revoked_at is not None and (
        row.granted_at is None or row.revoked_at >= row.granted_at
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# Dispatch (single event for a single member)
# ---------------------------------------------------------------------------
async def dispatch(
    db: AsyncSession,
    *,
    event_type: str,
    person: People,
    dedup_key: str,
    subscription: Optional[MembershipSubscription] = None,
    plan: Optional[Any] = None,
) -> str:
    """Send one notification for ``event_type`` to ``person``. Returns an outcome string.

    Outcomes: ``sent`` | ``failed`` | ``duplicate`` | ``disabled`` | ``no_template`` |
    ``no_phone`` | ``opted_out``. Never raises — failures are recorded in ``notification_log``.
    """
    setting = await crud.get_setting_model(db, event_type)
    if setting is None or not setting.enabled or not setting.template_id:
        return "disabled"

    tpl = await templates_crud.get_template_model(db, setting.template_id)
    if tpl is None or (tpl.template_status or "").upper() != "APPROVED":
        logger.info("notification %s: template missing/not approved", event_type)
        return "no_template"

    phone = (person.phone_number or person.wa_id or "").strip()
    wa_id = re.sub(r"\D", "", phone)
    if not wa_id:
        return "no_phone"

    # Claim first so concurrent duplicates (multiple workers / retries) bail out here.
    log = await crud.claim_log(
        db,
        dedup_key=dedup_key,
        event_type=event_type,
        person_id=person.id,
        subscription_id=getattr(subscription, "id", None),
        template_id=tpl.id,
    )
    if log is None:
        return "duplicate"

    if await _is_opted_out(db, person.id):
        await crud.mark_log(db, log, status="skipped", error="opted_out", commit=True)
        return "opted_out"

    context = build_variable_context(person, subscription, plan)
    body_params = _resolve_body_params(setting.param_mapping, context)

    try:
        # authoritative=False: reuse the existing 52/521-aware contact, never overwrite its wa_id.
        contact = await chat_crud.upsert_contact(
            db, wa_id=wa_id, phone_number=phone, authoritative=False
        )
        conversation = await chat_crud.get_or_open_conversation(db, contact.id)
        result = await cloud.send_template(
            to=contact.wa_id,
            template_name=tpl.template_name,
            language_code=tpl.template_language,
            body_params=body_params,
            components=tpl.components,
        )
    except cloud.WhatsAppError as e:
        await crud.mark_log(db, log, status="failed", error=e.message, commit=True)
        logger.warning("notification %s send failed: %s", event_type, e.message)
        return "failed"
    except Exception as e:  # noqa: BLE001
        await db.rollback()
        logger.exception("notification %s unexpected error", event_type)
        # Best effort: record the failure in a fresh transaction.
        try:
            async with async_session_factory() as db2:
                fresh = await crud.claim_log(
                    db2,
                    dedup_key=dedup_key,
                    event_type=event_type,
                    person_id=person.id,
                    subscription_id=getattr(subscription, "id", None),
                    template_id=tpl.id,
                )
                if fresh is not None:
                    await crud.mark_log(db2, fresh, status="failed", error=str(e), commit=True)
        except Exception:  # noqa: BLE001
            pass
        return "failed"

    await chat_crud.insert_outbound_message(
        db,
        conversation_id=conversation.id,
        contact_id=contact.id,
        text=render_template_text(tpl.components, body_params) or tpl.template_name,
        wa_message_id=result.get("wa_message_id"),
        message_type="template",
        template_id=tpl.id,
    )
    await crud.mark_log(
        db, log, status="sent", wa_message_id=result.get("wa_message_id"), commit=True
    )
    return "sent"


# ---------------------------------------------------------------------------
# Background trigger helpers (own session, called after the business commit)
# ---------------------------------------------------------------------------
async def _load_person(db: AsyncSession, person_id: int) -> Optional[People]:
    return (
        await db.execute(select(People).where(People.id == person_id))
    ).scalars().first()


async def _load_subscription(
    db: AsyncSession, subscription_id: int
) -> Optional[MembershipSubscription]:
    stmt = (
        select(MembershipSubscription)
        .options(selectinload(MembershipSubscription.plan))
        .where(MembershipSubscription.id == subscription_id)
    )
    return (await db.execute(stmt)).scalars().first()


async def dispatch_event_in_background(
    event_type: str,
    *,
    person_id: int,
    subscription_id: Optional[int] = None,
) -> None:
    """Open a fresh session, reload entities by id and dispatch. Swallows all errors.

    Meant to be scheduled with ``asyncio.create_task`` from a GraphQL mutation *after* the
    business transaction has committed, so notification work never affects that transaction.
    """
    try:
        async with async_session_factory() as db:
            person = await _load_person(db, person_id)
            if person is None:
                return
            subscription = (
                await _load_subscription(db, subscription_id)
                if subscription_id is not None
                else None
            )
            if event_type == EVENT_NEW_REGISTRATION:
                dedup_key = f"{event_type}:{person_id}"
            elif subscription_id is not None:
                dedup_key = f"{event_type}:{subscription_id}"
            else:
                dedup_key = f"{event_type}:{person_id}"
            outcome = await dispatch(
                db,
                event_type=event_type,
                person=person,
                subscription=subscription,
                dedup_key=dedup_key,
            )
            logger.info("notification %s -> %s (person=%s)", event_type, outcome, person_id)
    except Exception:  # noqa: BLE001
        logger.exception("background dispatch failed for %s person=%s", event_type, person_id)


# ---------------------------------------------------------------------------
# Sweeps (scheduled daily + manual trigger)
# ---------------------------------------------------------------------------
async def _active_subscriptions_expiring_within(
    db: AsyncSession, days_ahead: int
) -> List[MembershipSubscription]:
    now = datetime.now(timezone.utc)
    future = now + timedelta(days=days_ahead)
    stmt = (
        select(MembershipSubscription)
        .options(
            selectinload(MembershipSubscription.person),
            selectinload(MembershipSubscription.plan),
        )
        .where(
            and_(
                MembershipSubscription.status == "active",
                MembershipSubscription.end_at.between(now, future),
            )
        )
        .order_by(MembershipSubscription.end_at.asc())
    )
    return list((await db.execute(stmt)).scalars().all())


async def _recently_lapsed_subscriptions(
    db: AsyncSession, window_days: int
) -> List[MembershipSubscription]:
    now = datetime.now(timezone.utc)
    floor = now - timedelta(days=window_days)
    stmt = (
        select(MembershipSubscription)
        .options(
            selectinload(MembershipSubscription.person),
            selectinload(MembershipSubscription.plan),
        )
        .where(
            and_(
                MembershipSubscription.status == "active",
                MembershipSubscription.end_at < now,
                MembershipSubscription.end_at >= floor,
            )
        )
        .order_by(MembershipSubscription.end_at.desc())
    )
    return list((await db.execute(stmt)).scalars().all())


def _days_until(end_at: datetime, now: datetime) -> float:
    end_aware = end_at if end_at.tzinfo else end_at.replace(tzinfo=timezone.utc)
    return (end_aware - now).total_seconds() / 86400.0


def _matches_offset(days_until: float, offset: int) -> bool:
    """A subscription crosses offset ``N`` exactly once: when N-1 < days_until <= N.

    With a daily sweep this fires each reminder once on the right day and avoids sending
    several offsets at once for memberships shorter than the largest offset.
    """
    return (offset - 1) < days_until <= offset


async def run_renewal_sweep(db: AsyncSession) -> Dict[str, int]:
    """Send renewal reminders for each configured offset. Idempotent per (sub, offset)."""
    stats = {"sent": 0, "skipped": 0, "failed": 0}
    setting = await crud.get_setting_model(db, EVENT_RENEWAL_REMINDER)
    if setting is None or not setting.enabled or not setting.template_id:
        return stats

    offsets = sorted({int(o) for o in (setting.offsets_days or DEFAULT_REMINDER_OFFSETS) if int(o) > 0})
    if not offsets:
        return stats

    now = datetime.now(timezone.utc)
    subs = await _active_subscriptions_expiring_within(db, max(offsets))
    for sub in subs:
        if sub.person is None:
            continue
        days_until = _days_until(sub.end_at, now)
        for offset in offsets:
            if not _matches_offset(days_until, offset):
                continue
            outcome = await dispatch(
                db,
                event_type=EVENT_RENEWAL_REMINDER,
                person=sub.person,
                subscription=sub,
                plan=sub.plan,
                dedup_key=f"{EVENT_RENEWAL_REMINDER}:{sub.id}:{offset}",
            )
            _tally(stats, outcome)
    return stats


async def run_expired_sweep(db: AsyncSession) -> Dict[str, int]:
    """Send win-back messages for memberships that lapsed within the safety window."""
    stats = {"sent": 0, "skipped": 0, "failed": 0}
    setting = await crud.get_setting_model(db, EVENT_MEMBERSHIP_EXPIRED)
    if setting is None or not setting.enabled or not setting.template_id:
        return stats

    subs = await _recently_lapsed_subscriptions(db, EXPIRED_WINDOW_DAYS)
    for sub in subs:
        if sub.person is None:
            continue
        outcome = await dispatch(
            db,
            event_type=EVENT_MEMBERSHIP_EXPIRED,
            person=sub.person,
            subscription=sub,
            plan=sub.plan,
            dedup_key=f"{EVENT_MEMBERSHIP_EXPIRED}:{sub.id}",
        )
        _tally(stats, outcome)
    return stats


def _tally(stats: Dict[str, int], outcome: str) -> None:
    if outcome == "sent":
        stats["sent"] += 1
    elif outcome == "failed":
        stats["failed"] += 1
    elif outcome in ("duplicate", "disabled", "no_template", "no_phone", "opted_out"):
        stats["skipped"] += 1


async def run_all_sweeps() -> Dict[str, Dict[str, int]]:
    """Run both scheduled sweeps in a fresh session. Used by the scheduler and the
    manual ``runNotificationSweep`` mutation."""
    async with async_session_factory() as db:
        renewal = await run_renewal_sweep(db)
        expired = await run_expired_sweep(db)
    return {"renewal_reminder": renewal, "membership_expired": expired}
