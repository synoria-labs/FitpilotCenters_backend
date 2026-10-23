"""Put template-generated classes back at their local wall-clock time.

What happened
-------------
``generate_sessions_from_template`` built each class start with
``datetime.combine(day, template.start_time_local)`` — a *naive* datetime — and stored it in a
TIMESTAMPTZ column. asyncpg encodes a naive value by calling ``astimezone(utc)`` on it, which
Python interprets in the host's local zone. While the backend ran on a machine in Mexico that
was correct by accident. Since the move to the Docker container (UTC, early June 2026) every
class the generator created was stored six hours early: "Spinning 7:00 pm" at 13:00, "Spinning
9:00 am" at 03:00. The chatbot, which converts to local time, has been announcing those times.

Measured on production before writing this (2026-10-21): 2,341 sessions; 1,619 with a local
start equal to their template's ``start_time_local``; **221** (ids 5410-5630, created
2026-06-08 -> 2026-10-21, dated 2026-06-22 -> 2026-11-20) whose start equals the template time
*in UTC* instead. No session fit neither pattern, and no template/day had both a correct and a
shifted session, so the transformation below cannot create duplicates. The code fix lives in
``app/core/timezones.py`` and ``classSessionCrud.generate_sessions_from_template``.

The transformation
------------------
For each template-generated session whose UTC wall-clock time equals the template's local time
while its local wall-clock time does not, reinterpret the stored wall-clock as local time:

    start_at := (start_at AT TIME ZONE 'UTC') AT TIME ZONE 'America/Mexico_City'

and the same for ``end_at``. The calendar day never changes (08:00-21:00 local maps to
02:00-15:00 of the same day), so standing-booking exceptions keyed by day keep matching.
Reservations, seats and history are untouched: only the two timestamps move.

It is idempotent: once a row is corrected its local time equals the template time and the
predicate no longer matches. A row is skipped if correcting it would land exactly on another
session of the same template (none exist today; the guard keeps a re-run safe).

Downgrade is a no-op: shifting the rows back would reintroduce the bug, and after the fix
nothing distinguishes a corrected row from one that was always right.

Revision ID: b2e5f8a1c4d7
Revises: f5a8b1c6e4d7
Create Date: 2026-10-21 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "b2e5f8a1c4d7"
down_revision: Union[str, None] = "f5a8b1c6e4d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STUDIO_TZ = "America/Mexico_City"

_FIX_SQL = text(
    """
    UPDATE app.class_sessions AS cs
       SET start_at = (cs.start_at AT TIME ZONE 'UTC') AT TIME ZONE :tz,
           end_at = (cs.end_at AT TIME ZONE 'UTC') AT TIME ZONE :tz,
           updated_at = now()
      FROM app.class_templates AS ct
     WHERE ct.id = cs.template_id
       AND (cs.start_at AT TIME ZONE 'UTC')::time = ct.start_time_local
       AND (cs.start_at AT TIME ZONE :tz)::time <> ct.start_time_local
       AND NOT EXISTS (
            SELECT 1
              FROM app.class_sessions AS other
             WHERE other.template_id = cs.template_id
               AND other.id <> cs.id
               AND other.start_at = (cs.start_at AT TIME ZONE 'UTC') AT TIME ZONE :tz
       )
    """
)


def upgrade() -> None:
    result = op.get_bind().execute(_FIX_SQL, {"tz": STUDIO_TZ})
    print(f"[b2e5f8a1c4d7] class sessions moved to local time: {result.rowcount}")


def downgrade() -> None:
    # Intentionally irreversible: see the module docstring.
    pass
