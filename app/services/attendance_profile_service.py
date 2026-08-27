"""Resolve each member's favourite class, for the whole audience in two queries.

A campaign that segments by class is only half useful if the message cannot name the class.
This module answers "which class does this member book most, and when does it run?" for a
list of people at once — deliberately set-based, because the alternative (one aggregate per
recipient) is an N+1 across an audience that may be thousands of rows.

Two independent answers are returned, because they are genuinely different questions:

* **favourite class type** — the activity (Spinning), regardless of slot. A member who
  spreads bookings across four Spinning slots still has Spinning as their type.
* **favourite class template** — the *scheduled* class (Spinning, Mondays 07:00).

Resolving the template is a priority chain, not a single formula:

1. An **active standing booking** wins outright — it is an explicit recurring commitment, not
   an inference, so it does not need to "dominate" anything. A member with more than one active
   standing booking gets whichever they actually attend most (weighted the same as below).
2. Otherwise, only when one scheduled slot truly dominates the booking history.
3. Otherwise the template stays unresolved (only the class type is known).

The weighting (``checked_in`` counts more than ``reserved``) is not redefined here: it comes
from :func:`segmentation_service.favorite_class_subquery` /
:func:`segmentation_service.attendance_weight_for_templates`, so what a campaign *targets* and
what its message *says* can never drift apart.

Postgres runs in ``America/Mexico_City`` and the app does not override the session timezone
(see ``app/crud/time_filters.py``), so ``class_templates.start_time_local`` is already local
wall-clock time and needs no conversion.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import time
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ClassTemplate, ClassType, StandingBooking
from app.services import segmentation_service

logger = logging.getLogger(__name__)

# weekday is stored 0=Sunday .. 6=Saturday (see classModel.ClassTemplate).
_WEEKDAY_NAMES = (
    "domingo", "lunes", "martes", "miércoles", "jueves", "viernes", "sábado",
)


def weekday_name(weekday: Optional[int]) -> str:
    """Spanish day name for a stored weekday, or '' when unknown."""
    if weekday is None:
        return ""
    try:
        return _WEEKDAY_NAMES[int(weekday) % 7]
    except (TypeError, ValueError):
        return ""


def format_time(value: Optional[time]) -> str:
    """'7:00 a. m.' — es-MX style, no leading zero on the hour."""
    if value is None:
        return ""
    hour = value.hour % 12 or 12
    suffix = "a. m." if value.hour < 12 else "p. m."
    return f"{hour}:{value.minute:02d} {suffix}"


@dataclass(frozen=True)
class FavoriteClass:
    """A member's most-booked class, as far as it could be resolved."""

    class_type_id: Optional[int] = None
    class_type_name: str = ""
    class_template_id: Optional[int] = None
    weekday: Optional[int] = None
    start_time_local: Optional[time] = None

    @property
    def day_label(self) -> str:
        return weekday_name(self.weekday)

    @property
    def time_label(self) -> str:
        return format_time(self.start_time_local)

    @property
    def schedule_text(self) -> str:
        """'lunes a las 7:00 a. m.' — one placeholder for the whole usual slot.

        Only a single day/time — not "lunes y miércoles" — whether it came from a standing
        booking or (lacking one) the single dominant slot in booking history.
        """
        day, time_str = self.day_label, self.time_label
        if day and time_str:
            return f"{day} a las {time_str}"
        return day or time_str


def _favorite_from_row(row) -> "FavoriteClass":
    _, template_id, weekday, start_time, class_type_id, name = row
    return FavoriteClass(
        class_type_id=class_type_id,
        class_type_name=(name or "").strip(),
        class_template_id=template_id,
        weekday=weekday,
        start_time_local=start_time,
    )


async def favorite_classes_for(
    db: AsyncSession,
    person_ids: Sequence[int],
    *,
    lookback_days: int = segmentation_service.DEFAULT_AFFINITY_LOOKBACK_DAYS,
) -> Dict[int, FavoriteClass]:
    """Map ``person_id -> FavoriteClass`` for everyone with a booking history or an active
    standing booking.

    People with neither are simply absent from the result; callers treat a missing entry as
    "no favourite class" rather than inventing one.
    """
    ids: List[int] = [int(p) for p in person_ids if p is not None]
    if not ids:
        return {}

    resolved: Dict[int, FavoriteClass] = {}

    # 0) Standing bookings win outright — an explicit recurring commitment, not an
    #    inference, so it does not need to "dominate" anything.
    standing_rows = (
        await db.execute(
            select(
                StandingBooking.person_id,
                StandingBooking.template_id,
                ClassTemplate.weekday,
                ClassTemplate.start_time_local,
                ClassTemplate.class_type_id,
                ClassType.name,
            )
            .join(ClassTemplate, ClassTemplate.id == StandingBooking.template_id)
            .join(ClassType, ClassType.id == ClassTemplate.class_type_id)
            .where(StandingBooking.person_id.in_(ids), StandingBooking.status == "active")
        )
    ).all()

    standing_by_person: Dict[int, List[Any]] = {}
    for row in standing_rows:
        standing_by_person.setdefault(row[0], []).append(row)

    for person_id, rows in standing_by_person.items():
        if len(rows) == 1:
            resolved[person_id] = _favorite_from_row(rows[0])

    # A member with more than one active standing booking: pick whichever they actually
    # attend most, weighted the same way as everywhere else (checked_in > reserved).
    multi_person_ids = [pid for pid, rows in standing_by_person.items() if len(rows) > 1]
    if multi_person_ids:
        own_templates = {
            pid: {row[1] for row in standing_by_person[pid]} for pid in multi_person_ids
        }
        template_ids = list({tid for tids in own_templates.values() for tid in tids})
        weight_rows = (
            await db.execute(
                segmentation_service.attendance_weight_for_templates(
                    multi_person_ids, template_ids, lookback_days=lookback_days
                )
            )
        ).all()
        best: Dict[int, tuple] = {}  # person_id -> ((weight, last_seen), template_id)
        for person_id, template_id, weight, last_seen in weight_rows:
            # The weight query filters people and templates independently, so it also
            # returns the classes this member attends that are *someone else's* standing
            # booking. Ranking those would choose a template they have no standing booking
            # on, which is not an answer this branch can give.
            if template_id not in own_templates.get(person_id, ()):
                continue
            key = (weight or 0, last_seen)
            if person_id not in best or key > best[person_id][0]:
                best[person_id] = (key, template_id)
        for person_id in multi_person_ids:
            rows = standing_by_person[person_id]
            # No attendance evidence yet for any of them (e.g. brand new standing
            # bookings): fall back to the first rather than drop the person entirely.
            chosen_template_id = best.get(person_id, (None, rows[0][1]))[1]
            match = next((row for row in rows if row[1] == chosen_template_id), rows[0])
            resolved[person_id] = _favorite_from_row(match)

    remaining_ids = [pid for pid in ids if pid not in resolved]
    if not remaining_ids:
        return resolved

    # 1) Favourite activity, inferred from booking history (no standing booking to go on).
    type_favorites = segmentation_service.favorite_class_subquery(
        "class_type", lookback_days=lookback_days, person_ids=remaining_ids
    )
    type_rows = (
        await db.execute(
            select(
                type_favorites.c.person_id,
                type_favorites.c.class_key,
                ClassType.name,
            ).join(ClassType, ClassType.id == type_favorites.c.class_key)
        )
    ).all()
    for person_id, class_type_id, name in type_rows:
        resolved[person_id] = FavoriteClass(
            class_type_id=class_type_id, class_type_name=(name or "").strip()
        )

    # 2) Favourite scheduled slot, when one truly dominates. Its own type wins over the
    #    aggregate above: if a member's single most-booked class is Yoga Tuesdays, the
    #    message should say Yoga even if their bookings are spread across more Spinning
    #    slots in total.
    template_favorites = segmentation_service.favorite_class_subquery(
        "class_template", lookback_days=lookback_days, person_ids=remaining_ids
    )
    template_rows = (
        await db.execute(
            select(
                template_favorites.c.person_id,
                template_favorites.c.class_key,
                ClassTemplate.weekday,
                ClassTemplate.start_time_local,
                ClassTemplate.class_type_id,
                ClassType.name,
            )
            .join(ClassTemplate, ClassTemplate.id == template_favorites.c.class_key)
            .join(ClassType, ClassType.id == ClassTemplate.class_type_id)
        )
    ).all()
    for person_id, template_id, weekday, start_time, class_type_id, name in template_rows:
        resolved[person_id] = FavoriteClass(
            class_type_id=class_type_id,
            class_type_name=(name or "").strip(),
            class_template_id=template_id,
            weekday=weekday,
            start_time_local=start_time,
        )

    return resolved


async def favorite_class_for_recipients(
    db: AsyncSession, recipients: Sequence[object]
) -> Dict[int, FavoriteClass]:
    """Re-hydrate the frozen favourite class of already-built recipients.

    The build stores ids, not labels, so a dispatch batch resolves the display strings once
    for the whole batch instead of joining per message.
    """
    template_ids = {
        r.favorite_class_template_id
        for r in recipients
        if getattr(r, "favorite_class_template_id", None)
    }
    type_ids = {
        r.favorite_class_type_id
        for r in recipients
        if getattr(r, "favorite_class_type_id", None)
    }
    if not template_ids and not type_ids:
        return {}

    type_names: Dict[int, str] = {}
    if type_ids:
        rows = (
            await db.execute(
                select(ClassType.id, ClassType.name).where(ClassType.id.in_(type_ids))
            )
        ).all()
        type_names = {cid: (name or "").strip() for cid, name in rows}

    templates: Dict[int, tuple] = {}
    if template_ids:
        rows = (
            await db.execute(
                select(
                    ClassTemplate.id,
                    ClassTemplate.weekday,
                    ClassTemplate.start_time_local,
                    ClassTemplate.class_type_id,
                ).where(ClassTemplate.id.in_(template_ids))
            )
        ).all()
        templates = {row[0]: (row[1], row[2], row[3]) for row in rows}
        missing = {t[2] for t in templates.values()} - set(type_names)
        if missing:
            rows = (
                await db.execute(
                    select(ClassType.id, ClassType.name).where(ClassType.id.in_(missing))
                )
            ).all()
            type_names.update({cid: (name or "").strip() for cid, name in rows})

    out: Dict[int, FavoriteClass] = {}
    for recipient in recipients:
        rid = getattr(recipient, "id", None)
        if rid is None:
            continue
        template_id = getattr(recipient, "favorite_class_template_id", None)
        type_id = getattr(recipient, "favorite_class_type_id", None)
        if template_id and template_id in templates:
            weekday, start_time, tpl_type_id = templates[template_id]
            out[rid] = FavoriteClass(
                class_type_id=tpl_type_id,
                class_type_name=type_names.get(tpl_type_id, ""),
                class_template_id=template_id,
                weekday=weekday,
                start_time_local=start_time,
            )
        elif type_id:
            out[rid] = FavoriteClass(
                class_type_id=type_id, class_type_name=type_names.get(type_id, "")
            )
    return out
