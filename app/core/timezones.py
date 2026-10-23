"""Local wall-clock time for the studio, anchored to an explicit zone.

The schedule is defined in local time (``class_templates.start_time_local``) while
``class_sessions.start_at`` is TIMESTAMPTZ. Two traps follow from that, and both bit production:

* **Naive datetimes take the process timezone.** asyncpg encodes a naive ``datetime`` by calling
  ``astimezone(utc)`` on it, which Python interprets as the *host's* local zone. On a developer
  machine in Mexico that happened to be right; in the production container (UTC) every class
  generated from a template landed six hours early ("Spinning 7:00 pm" stored at 13:00).
* **``start_at.date()`` is the UTC day.** asyncpg returns TIMESTAMPTZ values in UTC, so any class
  at 18:00 or later local time reports the *next* calendar day. Day-keyed logic (standing-booking
  exceptions, template/date lookups) must use the local day instead.

Every conversion between a local wall-clock time and an instant goes through this module.
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

# Love Fitness operates in a single zone. Mexico abolished DST in 2022, but going through
# zoneinfo keeps the historic rows (and any future venue elsewhere) correct regardless.
STUDIO_TIMEZONE = "America/Mexico_City"


def zone(tz_name: Optional[str] = None) -> ZoneInfo:
    """The ``ZoneInfo`` for ``tz_name`` (the studio zone when omitted)."""
    return ZoneInfo(tz_name or STUDIO_TIMEZONE)


def local_datetime(day: date, at: time, tz_name: Optional[str] = None) -> datetime:
    """Aware instant for wall-clock ``at`` on calendar ``day`` in the given zone."""
    return datetime.combine(day, at.replace(tzinfo=None), tzinfo=zone(tz_name))


def to_local(value: datetime, tz_name: Optional[str] = None) -> datetime:
    """Convert an aware instant to local time. Naive values are assumed to be UTC."""
    if value.tzinfo is None:
        from datetime import timezone

        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(zone(tz_name))


def local_date(value: datetime, tz_name: Optional[str] = None) -> date:
    """The local calendar day of an instant (never the UTC day)."""
    return to_local(value, tz_name).date()
