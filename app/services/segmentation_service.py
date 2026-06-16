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
        {"type": "last_activity", "op": "older_than_days", "value": 30}
      ]
    }

Consent, recency-blocking, phone reachability and variant assignment are NOT applied here —
they belong to the build phase in ``campaign_service`` (which records skips honestly). This
module answers a single question: *which members match the membership predicates?*
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import (
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


_PREDICATE_BUILDERS = {
    "membership_status": lambda p, now: _membership_status_condition(p.get("in") or [], now),
    "membership_end_at": lambda p, now: _membership_end_at_condition(p, now),
    "plan_id": lambda p, now: _plan_condition(p),
    "last_activity": lambda p, now: _last_activity_condition(p, now),
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


async def preview_audience(
    db: AsyncSession, spec: Optional[Dict[str, Any]], *, sample_size: int = 10
) -> Dict[str, Any]:
    """Return ``{count, sample}`` for the wizard. Does not persist anything."""
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
    return {"count": len(ids), "sample": sample}


async def resolve_candidates(
    db: AsyncSession, spec: Optional[Dict[str, Any]]
) -> List[CandidateRow]:
    """Return matched members with their reference subscription (latest by end_at, plan loaded)."""
    person_ids = await matching_person_ids(db, spec)
    if not person_ids:
        return []

    people = {
        p.id: p
        for p in (
            await db.execute(select(People).where(People.id.in_(person_ids)))
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
