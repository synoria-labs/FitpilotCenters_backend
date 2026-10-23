"""Seed "Halloween · 2.º aniversario de Love Fitness" as a DRAFT special event.

What it inserts (only if the studio's data is there, and only once):

* Friday 2026-10-30 from 19:00 to 21:00 America/Mexico_City (= 2026-10-31 01:00-03:00 UTC),
  $500 MXN per participant, 14 participants with one bike each, at the venue whose address is
  Las Rosas #101 (checked on production 2026-10-21: venue 1, "Love fitness Bugambilias", 14
  active bikes), with the instructor whose name starts with "Laura Torres" (people 343, role
  instructor), class type ``spinning``. Duration and capacity confirmed by the studio on
  2026-10-22.
* The description, the address as given for the event, and what the ticket includes.
* Every active bike of that venue as the event's seat inventory.

What it deliberately leaves to staff: the event stays a ``draft`` (invisible to the chatbot).
19:00-21:00 overlaps the regular 19:00 and 20:00 classes; taking them over changes real
clients' bookings, so it must be previewed and confirmed in the app ("Horario regular"), and
only then can the event be published. A migration never displaces anyone by itself.

Idempotent: nothing is inserted when an event with the same name already starts at that
instant. On a fresh database (CI) the venue/instructor do not exist and nothing is inserted.
Downgrade removes the event only while it is still an untouched draft (no orders, takeovers or
campaign links), so it can never delete real sales.

Revision ID: d4a7b0c3e6f9
Revises: c3f6a9b2d5e8
Create Date: 2026-10-21 00:00:00.000000
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "d4a7b0c3e6f9"
down_revision: Union[str, None] = "c3f6a9b2d5e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EVENT_NAME = "Halloween · 2.º aniversario de Love Fitness"
# Local wall-clock start/end; converted to instants in SQL with AT TIME ZONE.
LOCAL_START = datetime(2026, 10, 30, 19, 0, 0)
LOCAL_END = datetime(2026, 10, 30, 21, 0, 0)
TIMEZONE = "America/Mexico_City"
PRICE = Decimal("500.00")
CAPACITY = 14  # one bike per participant
DESCRIPTION = (
    "Celebra el 2.º aniversario de Love Fitness con una noche especial de spinning, música y "
    "disfraces. ¡Prepara tu mejor disfraz!"
)
ADDRESS = (
    "Las Rosas #101, Col. Rinconada Bugambilias, C.P. 35010, Gómez Palacio, Durango."
)
INCLUDES = "\n".join(
    [
        "Kit de Electrolit y toalla",
        "Mesa de snacks",
        "DJ con mezclas en vivo",
        "Video profesional de recuerdo para compartir con los participantes",
        "Premios a los tres mejores disfraces",
    ]
)

_INSERT_EVENT = text(
    """
    WITH venue AS (
        SELECT id FROM app.venues
         WHERE address ILIKE '%las rosas%101%' OR name ILIKE '%bugambilias%'
         ORDER BY id
         LIMIT 1
    ), instructor AS (
        SELECT p.id
          FROM app.people p
          JOIN app.person_roles pr ON pr.person_id = p.id
          JOIN app.roles r ON r.id = pr.role_id
         WHERE r.code = 'instructor'
           AND p.deleted_at IS NULL
           AND p.full_name ILIKE 'laura torres%'
         ORDER BY p.id
         LIMIT 1
    ), class_type AS (
        SELECT id FROM app.class_types WHERE code = 'spinning' ORDER BY id LIMIT 1
    )
    INSERT INTO app.special_events (
        name, description, includes, address, venue_id, class_type_id, instructor_id,
        timezone, start_at, end_at, price, currency, capacity, status, hold_minutes
    )
    SELECT CAST(:name AS text), CAST(:description AS text), CAST(:includes AS text),
           CAST(:address AS text), venue.id,
           (SELECT id FROM class_type), (SELECT id FROM instructor),
           CAST(:tz AS text),
           CAST(:local_start AS timestamp) AT TIME ZONE CAST(:tz AS text),
           CAST(:local_end AS timestamp) AT TIME ZONE CAST(:tz AS text),
           CAST(:price AS numeric), 'MXN', CAST(:capacity AS integer), 'draft', 30
      FROM venue
     WHERE NOT EXISTS (
            SELECT 1 FROM app.special_events e
             WHERE e.name = CAST(:name AS text)
               AND e.start_at = CAST(:local_start AS timestamp) AT TIME ZONE CAST(:tz AS text)
     )
    RETURNING id, venue_id
    """
)
# Explicit CASTs everywhere: asyncpg (production, CI) prepares the statement and lets the server
# infer parameter types, and an untyped parameter in an INSERT ... SELECT list resolves to text.


def upgrade() -> None:
    bind = op.get_bind()
    row = bind.execute(
        _INSERT_EVENT,
        {
            "name": EVENT_NAME,
            "description": DESCRIPTION,
            "includes": INCLUDES,
            "address": ADDRESS,
            "tz": TIMEZONE,
            "local_start": LOCAL_START,
            "local_end": LOCAL_END,
            "price": PRICE,
            "capacity": CAPACITY,
        },
    ).first()
    if row is None:
        print("[d4a7b0c3e6f9] Halloween event not seeded (already present or no matching venue).")
        return

    event_id, venue_id = row
    seats = bind.execute(
        text(
            """
            INSERT INTO app.special_event_seats (event_id, seat_id)
            SELECT CAST(:event_id AS bigint), s.id FROM app.seats s
             WHERE s.venue_id = CAST(:venue_id AS bigint) AND s.is_active
            ON CONFLICT DO NOTHING
            """
        ),
        {"event_id": event_id, "venue_id": venue_id},
    )
    print(
        f"[d4a7b0c3e6f9] Halloween event {event_id} seeded as draft (19:00-21:00, capacity "
        f"{CAPACITY}) with {seats.rowcount} bikes; takeover and publication pending in the app."
    )


def downgrade() -> None:
    op.get_bind().execute(
        text(
            """
            DELETE FROM app.special_events e
             WHERE e.name = CAST(:name AS text)
               AND e.status = 'draft'
               AND NOT EXISTS (SELECT 1 FROM app.special_event_orders o WHERE o.event_id = e.id)
               AND NOT EXISTS (SELECT 1 FROM app.special_event_takeovers t WHERE t.event_id = e.id)
               AND NOT EXISTS (SELECT 1 FROM app.campaigns c WHERE c.special_event_id = e.id)
            """
        ),
        {"name": EVENT_NAME},
    )
