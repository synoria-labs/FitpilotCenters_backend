"""Unit tests for the group-wide seat resolver.

``_resolve_group_seat`` is the crux of the "reassign an available bike before refunding" fix:
a fixed-slot package reserves the SAME seat for every template of a timeslot group across the
whole membership window, so the seat must be free everywhere. When the preferred bike is taken,
the resolver must auto-pick another free one instead of letting ``create_standing_booking`` raise
"Seat already reserved" -> 0 bookings -> refund.

DB-free: a fake AsyncSession returns the two query results the resolver consumes (the active seats
for the venue, then the seat_ids already taken across the group/window). The local test DB is not
available in every environment and the seat-selection decision is the part worth pinning down.
"""
import datetime
from unittest.mock import AsyncMock, MagicMock

from app.crud.standing_bookings.utils import _resolve_group_seat

_START = datetime.date(2026, 6, 1)
_END = datetime.date(2026, 6, 30)
_PERSON = 389


def _seat(seat_id: int, label: str):
    s = MagicMock()
    s.id = seat_id
    s.label = label
    return s


def _template(template_id: int, venue_id: int = 20):
    t = MagicMock()
    t.id = template_id
    t.venue_id = venue_id
    return t


_GROUP = [_template(t) for t in (10, 15, 20, 25, 30)]


def _db(seats, taken_ids):
    """First execute() -> active venue seats; second -> rows of (seat_id,) already taken."""
    db = AsyncMock()
    seats_res = MagicMock()
    seats_res.scalars.return_value.all.return_value = seats
    taken_res = MagicMock()
    taken_res.all.return_value = [(sid,) for sid in taken_ids]
    db.execute = AsyncMock(side_effect=[seats_res, taken_res])
    return db


async def test_keeps_preferred_seat_when_free():
    db = _db([_seat(7, "B07"), _seat(13, "B13")], taken_ids=set())
    seat_id, label = await _resolve_group_seat(db, _GROUP, _START, _END, _PERSON, preferred_seat_id=13)
    assert (seat_id, label) == (13, "B13")


async def test_reassigns_when_preferred_seat_taken():
    # Seat 13 is taken somewhere in the group/window -> swap to the first free bike (B07).
    db = _db([_seat(7, "B07"), _seat(13, "B13")], taken_ids={13})
    seat_id, label = await _resolve_group_seat(db, _GROUP, _START, _END, _PERSON, preferred_seat_id=13)
    assert (seat_id, label) == (7, "B07")


async def test_auto_assigns_when_no_preferred_seat():
    db = _db([_seat(7, "B07"), _seat(13, "B13")], taken_ids={7})
    seat_id, label = await _resolve_group_seat(db, _GROUP, _START, _END, _PERSON, preferred_seat_id=None)
    assert (seat_id, label) == (13, "B13")


async def test_keeps_preferred_when_all_taken_so_refund_path_applies():
    # Genuinely full: every bike taken -> return the preferred id unchanged (no label) so the
    # caller's existing "Seat already reserved" -> refund path still triggers.
    db = _db([_seat(7, "B07"), _seat(13, "B13")], taken_ids={7, 13})
    seat_id, label = await _resolve_group_seat(db, _GROUP, _START, _END, _PERSON, preferred_seat_id=13)
    assert (seat_id, label) == (13, None)


async def test_seatless_venue_returns_none():
    # Venue defines no seats -> capacity-based booking, no seat to assign.
    db = _db([], taken_ids=set())
    seat_id, label = await _resolve_group_seat(db, _GROUP, _START, _END, _PERSON, preferred_seat_id=13)
    assert (seat_id, label) == (None, None)
