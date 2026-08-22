"""Compile a declarative ``audience_spec`` into a member audience.

The spec is a small predicate AST evaluated against ``people`` (members only in the Phase-1
recapture MVP). It is stored on the campaign so the audience is reproducible and auditable.

Example spec::

    {
      "base": "members",
      "predicates": [
        {"type": "membership_status", "in": ["expired"]},
        {"type": "membership_end_at", "op": "between", "days_from_now": [-90, -7]},
        {"type": "plan_id", "in": [3, 4]},
        {"type": "last_activity", "op": "older_than_days", "value": 30},
        {"type": "class_affinity", "level": "class_type", "mode": "favorite", "in": [3]}
      ]
    }

``class_affinity`` answers "which classes does this member actually book?" — at the level of
the activity (``class_type``) or of the scheduled class itself (``class_template``: Spinning,
Mondays 07:00). ``favorite`` keeps only members whose single most-booked class is in the
selection; ``attended`` keeps anyone who booked it at least ``min_reservations`` times.

Consent, recency-blocking, phone reachability and variant assignment are NOT applied here —
they belong to the build phase in ``campaign_service`` (which records skips honestly). This
module answers a single question: *which members match the membership predicates?*
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only, selectinload

from app.models import (
    ClassSession,
    MembershipSubscription,
    People,
    PersonRole,
    Reservation,
    Role,
)

logger = logging.getLogger(__name__)


class SegmentationError(ValueError):
    """Raised when an audience_spec is malformed or uses an unsupported base."""


@dataclass
class CandidateRow:
    """A matched member plus the reference subscription used for variable context."""

    person: People
    subscription: Optional[MembershipSubscription]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Correlated EXISTS helpers (correlate on People.id)
# ---------------------------------------------------------------------------
def _sub_exists(*conditions):
    return (
        select(MembershipSubscription.id)
        .where(MembershipSubscription.person_id == People.id, *conditions)
        .exists()
    )


def _membership_status_condition(values: List[str], now: datetime):
    """OR of the requested membership states, computed from subscriptions."""
    clauses = []
    has_active = _sub_exists(
        MembershipSubscription.status == "active",
        MembershipSubscription.end_at > now,
    )
    for raw in values:
        value = str(raw).lower()
        if value == "active":
            clauses.append(has_active)
        elif value in ("expired", "lapsed"):
            # No current active subscription, but a past/expired one exists.
            had_membership = _sub_exists(
                or_(
                    MembershipSubscription.status == "expired",
                    MembershipSubscription.end_at < now,
                )
            )
            clauses.append(and_(~has_active, had_membership))
        elif value == "pending":
            clauses.append(_sub_exists(MembershipSubscription.status == "pending"))
        elif value == "canceled":
            clauses.append(_sub_exists(MembershipSubscription.status == "canceled"))
        else:
            raise SegmentationError(f"Estado de membresía no soportado: {raw}")
    if not clauses:
        return None
    return or_(*clauses) if len(clauses) > 1 else clauses[0]


def _membership_end_at_condition(predicate: Dict[str, Any], now: datetime):
    op = str(predicate.get("op") or "between").lower()
    if op == "between":
        bounds = predicate.get("days_from_now") or []
        if not isinstance(bounds, list) or len(bounds) != 2:
            raise SegmentationError("membership_end_at 'between' requiere days_from_now=[min,max].")
        lo, hi = sorted(int(b) for b in bounds)
        start = now + timedelta(days=lo)
        end = now + timedelta(days=hi)
        return _sub_exists(MembershipSubscription.end_at.between(start, end))
    if op == "expired_within_days":
        days = int(predicate.get("value", 0))
        floor = now - timedelta(days=days)
        return _sub_exists(
            MembershipSubscription.end_at < now,
            MembershipSubscription.end_at >= floor,
        )
    raise SegmentationError(f"Operador no soportado para membership_end_at: {op}")


def _plan_condition(predicate: Dict[str, Any]):
    plan_ids = predicate.get("in") or []
    plan_ids = [int(p) for p in plan_ids]
    if not plan_ids:
        return None
    return _sub_exists(MembershipSubscription.plan_id.in_(plan_ids))


def _last_activity_condition(predicate: Dict[str, Any], now: datetime):
    op = str(predicate.get("op") or "older_than_days").lower()
    days = int(predicate.get("value", 0))
    floor = now - timedelta(days=days)
    recent_reservation = (
        select(Reservation.id)
        .where(Reservation.person_id == People.id, Reservation.reserved_at >= floor)
        .exists()
    )
    if op == "older_than_days":
        # Inactive: no reservation within the last N days.
        return ~recent_reservation
    if op == "within_days":
        return recent_reservation
    raise SegmentationError(f"Operador no soportado para last_activity: {op}")


# ---------------------------------------------------------------------------
# Class affinity — "which classes does this member actually book?"
# ---------------------------------------------------------------------------
# A booking is not the same evidence as showing up: ``checked_in`` is attendance,
# ``reserved`` is only intent. Weighting them differently keeps a member who books and
# attends Spinning ahead of one who booked Yoga once and never came. ``canceled`` and
# ``no_show`` are not evidence of preference at all, so they are excluded outright.
_ATTENDANCE_STATUSES = ("checked_in", "reserved")
_CHECKED_IN_WEIGHT = 3
_RESERVED_WEIGHT = 1

CLASS_AFFINITY_LEVELS = ("class_type", "class_template")
CLASS_AFFINITY_MODES = ("favorite", "attended")
DEFAULT_AFFINITY_LOOKBACK_DAYS = 180


def _affinity_key_column(level: str):
    """The column that identifies 'a class' at the requested granularity.

    ``class_template`` is the "class with a schedule" (Spinning, Mondays 07:00);
    ``class_type`` is the activity regardless of slot.
    """
    if level == "class_template":
        return ClassSession.template_id
    return ClassSession.class_type_id


def _affinity_weight():
    return func.sum(
        case(
            (Reservation.status == "checked_in", _CHECKED_IN_WEIGHT),
            else_=_RESERVED_WEIGHT,
        )
    )


def _affinity_base(
    level: str,
    now: datetime,
    lookback_days: int,
    person_ids: Optional[List[int]] = None,
):
    """Per (person, class) booking weight over the lookback window.

    ``person_ids`` narrows the scan to a known audience; omit it when the query is itself
    the thing selecting people.
    """
    key = _affinity_key_column(level)
    floor = now - timedelta(days=lookback_days)
    stmt = (
        select(
            Reservation.person_id.label("person_id"),
            key.label("class_key"),
            _affinity_weight().label("weight"),
            func.max(ClassSession.start_at).label("last_seen"),
        )
        .join(ClassSession, ClassSession.id == Reservation.session_id)
        .where(
            Reservation.status.in_(_ATTENDANCE_STATUSES),
            ClassSession.start_at >= floor,
        )
        .group_by(Reservation.person_id, key)
    )
    if person_ids is not None:
        stmt = stmt.where(Reservation.person_id.in_(person_ids))
    if level == "class_template":
        # Ad-hoc sessions have no template; they cannot be attributed to a scheduled class.
        stmt = stmt.where(ClassSession.template_id.isnot(None))
    return stmt


def favorite_class_subquery(
    level: str,
    now: Optional[datetime] = None,
    lookback_days: int = DEFAULT_AFFINITY_LOOKBACK_DAYS,
    person_ids: Optional[List[int]] = None,
):
    """``(person_id, class_key)`` keeping only each member's single top class.

    Public because the campaign build freezes the same answer onto the audience snapshot —
    segmentation and personalization must agree on what "favourite" means.
    """
    now = now or _now()
    ranked = (
        _affinity_base(level, now, lookback_days, person_ids)
        .add_columns(
            func.row_number()
            .over(
                partition_by=Reservation.person_id,
                # Most booked wins; the most recent one breaks a tie, so a member whose
                # habits changed is segmented by what they do now, not what they used to do.
                order_by=(
                    _affinity_weight().desc(),
                    func.max(ClassSession.start_at).desc(),
                ),
            )
            .label("rn")
        )
        .subquery()
    )
    return select(ranked.c.person_id, ranked.c.class_key).where(ranked.c.rn == 1).subquery()


def _class_affinity_condition(predicate: Dict[str, Any], now: datetime):
    level = str(predicate.get("level") or "class_type").lower()
    if level not in CLASS_AFFINITY_LEVELS:
        raise SegmentationError(f"Nivel de afinidad de clase no soportado: {level}")
    mode = str(predicate.get("mode") or "favorite").lower()
    if mode not in CLASS_AFFINITY_MODES:
        raise SegmentationError(f"Modo de afinidad de clase no soportado: {mode}")

    try:
        class_ids = [int(v) for v in (predicate.get("in") or [])]
    except (TypeError, ValueError) as exc:
        raise SegmentationError("class_affinity 'in' debe ser una lista de ids.") from exc
    if not class_ids:
        return None  # nothing selected == no constraint, same as the other predicates

    # `or` would swallow an explicit 0 into the default; a caller asking for a zero-day
    # window has made a mistake and should hear about it, not get 180 days silently.
    raw_lookback = predicate.get("lookback_days")
    lookback = (
        DEFAULT_AFFINITY_LOOKBACK_DAYS if raw_lookback is None else int(raw_lookback)
    )
    if lookback <= 0:
        raise SegmentationError("class_affinity 'lookback_days' debe ser mayor que cero.")

    if mode == "favorite":
        favorites = favorite_class_subquery(level, now, lookback)
        return People.id.in_(
            select(favorites.c.person_id).where(favorites.c.class_key.in_(class_ids))
        )

    # mode == "attended": booked the selected classes at least N times.
    raw_min = predicate.get("min_reservations")
    min_reservations = 1 if raw_min is None else int(raw_min)
    if min_reservations < 1:
        raise SegmentationError("class_affinity 'min_reservations' debe ser al menos 1.")
    key = _affinity_key_column(level)
    floor = now - timedelta(days=lookback)
    attended = (
        select(Reservation.person_id)
        .join(ClassSession, ClassSession.id == Reservation.session_id)
        .where(
            Reservation.status.in_(_ATTENDANCE_STATUSES),
            ClassSession.start_at >= floor,
            key.in_(class_ids),
        )
        .group_by(Reservation.person_id)
        .having(func.count() >= min_reservations)
    )
    return People.id.in_(attended)


_PREDICATE_BUILDERS = {
    "membership_status": lambda p, now: _membership_status_condition(p.get("in") or [], now),
    "membership_end_at": lambda p, now: _membership_end_at_condition(p, now),
    "plan_id": lambda p, now: _plan_condition(p),
    "last_activity": lambda p, now: _last_activity_condition(p, now),
    "class_affinity": lambda p, now: _class_affinity_condition(p, now),
}


# ---------------------------------------------------------------------------
# Query assembly
# ---------------------------------------------------------------------------
def _member_base_query(now: datetime):
    """Base: real members (member role, not soft-deleted, with at least one subscription)."""
    has_any_sub = _sub_exists()
    return (
        select(People.id)
        .join(PersonRole, PersonRole.person_id == People.id)
        .join(Role, Role.id == PersonRole.role_id)
        .where(Role.code == "member")
        .where(People.deleted_at.is_(None))
        .where(has_any_sub)
    )


def build_member_id_query(spec: Optional[Dict[str, Any]], now: Optional[datetime] = None):
    """Compile ``spec`` to a ``select(People.id)`` query. Members-only base in Phase 1."""
    now = now or _now()
    spec = spec or {}
    base = str(spec.get("base") or "members").lower()
    if base != "members":
        raise SegmentationError(
            f"La base de audiencia '{base}' aún no está disponible (la captación es Fase 2)."
        )

    stmt = _member_base_query(now)
    for predicate in spec.get("predicates") or []:
        if not isinstance(predicate, dict):
            continue
        ptype = str(predicate.get("type") or "").lower()
        builder = _PREDICATE_BUILDERS.get(ptype)
        if builder is None:
            raise SegmentationError(f"Predicado de audiencia desconocido: {ptype}")
        condition = builder(predicate, now)
        if condition is not None:
            stmt = stmt.where(condition)
    return stmt.distinct()


async def matching_person_ids(db: AsyncSession, spec: Optional[Dict[str, Any]]) -> List[int]:
    stmt = build_member_id_query(spec)
    return [row for row in (await db.execute(stmt)).scalars().all()]


async def _unreachable_breakdown(
    db: AsyncSession, person_ids: List[int], *, recency_block_days: int
) -> Dict[str, int]:
    """Count, in sets, the members the build will skip and why.

    The segment size is not the audience size: the build drops anyone without a usable
    number, anyone who revoked WhatsApp consent, and anyone contacted too recently. Showing
    only the segment count promises an audience that will not materialise.

    Each member is attributed to a single reason, in the same order the build applies them,
    so the numbers add up instead of double-counting.
    """
    empty = {"no_phone": 0, "no_consent": 0, "recency_block": 0}
    if not person_ids:
        return empty

    # Imported here: these belong to the campaign/WhatsApp side, and segmentation is
    # otherwise free of them.
    from app.crud import campaignsCrud as campaigns_crud
    from app.models import CommunicationOptIn

    reachable = set(person_ids)

    rows = (
        await db.execute(
            select(People.id, People.phone_number, People.wa_id).where(
                People.id.in_(person_ids)
            )
        )
    ).all()
    no_phone = {
        pid
        for pid, phone, wa_id in rows
        if not re.sub(r"\D", "", (phone or wa_id or "").strip())
    }
    reachable -= no_phone

    opted_out = set()
    if reachable:
        # Mirrors notification_service._is_opted_out: only the LATEST whatsapp consent record
        # counts, and only a revocation at or after the grant means opted out. DISTINCT ON
        # gets that latest row per person in one pass instead of one query each.
        latest_consent = (
            select(
                CommunicationOptIn.person_id,
                CommunicationOptIn.granted_at,
                CommunicationOptIn.revoked_at,
            )
            .where(
                CommunicationOptIn.person_id.in_(reachable),
                CommunicationOptIn.channel == "whatsapp",
            )
            .distinct(CommunicationOptIn.person_id)
            .order_by(
                CommunicationOptIn.person_id, CommunicationOptIn.created_at.desc()
            )
        )
        opted_out = {
            person_id
            for person_id, granted_at, revoked_at in (
                await db.execute(latest_consent)
            ).all()
            if revoked_at is not None
            and (granted_at is None or revoked_at >= granted_at)
        }
        reachable -= opted_out

    blocked = set()
    if reachable and recency_block_days > 0:
        recent = await campaigns_crud.recently_targeted_person_ids(
            db, days=recency_block_days
        )
        blocked = reachable & recent
        reachable -= blocked

    return {
        "no_phone": len(no_phone),
        "no_consent": len(opted_out),
        "recency_block": len(blocked),
    }


async def preview_audience(
    db: AsyncSession,
    spec: Optional[Dict[str, Any]],
    *,
    sample_size: int = 10,
    recency_block_days: int = 30,
) -> Dict[str, Any]:
    """Return ``{count, reachable, skipped, sample}`` for the wizard. Persists nothing."""
    ids = await matching_person_ids(db, spec)
    sample: List[str] = []
    if ids:
        sample_ids = ids[:sample_size]
        rows = (
            await db.execute(
                select(People.full_name).where(People.id.in_(sample_ids))
            )
        ).scalars().all()
        sample = [(name or "").strip() or "(sin nombre)" for name in rows]

    skipped = await _unreachable_breakdown(
        db, ids, recency_block_days=recency_block_days
    )
    return {
        "count": len(ids),
        "reachable": len(ids) - sum(skipped.values()),
        "skipped": skipped,
        "sample": sample,
    }


async def resolve_candidates(
    db: AsyncSession, spec: Optional[Dict[str, Any]]
) -> List[CandidateRow]:
    """Return matched members with their reference subscription (latest by end_at, plan loaded)."""
    person_ids = await matching_person_ids(db, spec)
    if not person_ids:
        return []

    # Only the columns the build actually reads. Materialising every People column for a
    # multi-thousand-member audience is a lot of PII moved for three fields.
    people = {
        p.id: p
        for p in (
            await db.execute(
                select(People)
                .options(
                    load_only(
                        People.id,
                        People.full_name,
                        People.phone_number,
                        People.wa_id,
                    )
                )
                .where(People.id.in_(person_ids))
            )
        ).scalars().all()
    }

    subs = (
        await db.execute(
            select(MembershipSubscription)
            .options(selectinload(MembershipSubscription.plan))
            .where(MembershipSubscription.person_id.in_(person_ids))
            .order_by(
                MembershipSubscription.person_id,
                MembershipSubscription.end_at.desc(),
            )
        )
    ).scalars().all()
    reference: Dict[int, MembershipSubscription] = {}
    for sub in subs:
        if sub.person_id not in reference:  # first row per person = latest end_at
            reference[sub.person_id] = sub

    return [
        CandidateRow(person=people[pid], subscription=reference.get(pid))
        for pid in person_ids
        if pid in people
    ]
