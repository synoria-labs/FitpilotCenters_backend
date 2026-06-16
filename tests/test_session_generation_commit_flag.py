"""Regression test for the package-enrollment atomicity fix.

``generate_sessions_from_template`` must COMMIT for standalone callers (admin session
generation, rolling-window maintenance) but only FLUSH when ``commit=False``, so the
fixed-slot enrollment flow can fold session generation into its single outer commit and
roll everything back if materialization later fails. Before the fix it always committed
mid-transaction, leaving a paid membership committed even when the class turned out full.

DB-free: a fake AsyncSession records whether commit/flush was called (the local test DB is
not available in every environment, and the commit-vs-flush decision is the crux of the fix).
"""
import datetime
from unittest.mock import AsyncMock, MagicMock

from app.crud import classSessionCrud

# 2026-06-15 is a Monday (date.weekday() == 0), which matches a template whose
# weekday field is 1 (their encoding is 0=Sunday..6=Saturday): (1 - 1) % 7 == 0.
_MONDAY = datetime.date(2026, 6, 15)


def _fake_template():
    t = MagicMock()
    t.is_active = True
    t.weekday = 1
    t.class_type_id = 10
    t.venue_id = 20
    t.instructor_id = None
    t.name = "Spinning"
    t.start_time_local = datetime.time(18, 0)
    t.default_duration_min = 60
    t.default_capacity = 14
    return t


def _fake_db():
    """AsyncSession double: first execute() -> the template, second -> no existing session."""
    db = AsyncMock()
    tmpl_res = MagicMock()
    tmpl_res.scalar_one_or_none.return_value = _fake_template()
    exist_res = MagicMock()
    exist_res.scalar_one_or_none.return_value = None  # nothing exists -> one session is created
    db.execute = AsyncMock(side_effect=[tmpl_res, exist_res])
    db.add_all = MagicMock()  # add_all is synchronous in SQLAlchemy
    return db


async def test_generate_sessions_flush_only_when_commit_false():
    db = _fake_db()
    created = await classSessionCrud.generate_sessions_from_template(
        db, template_id=1, start_date=_MONDAY, end_date=_MONDAY, commit=False
    )
    assert len(created) == 1
    db.flush.assert_awaited_once()
    db.commit.assert_not_called()
    db.refresh.assert_not_called()


async def test_generate_sessions_commits_by_default():
    db = _fake_db()
    created = await classSessionCrud.generate_sessions_from_template(
        db, template_id=1, start_date=_MONDAY, end_date=_MONDAY  # commit defaults to True
    )
    assert len(created) == 1
    db.commit.assert_awaited_once()
    db.flush.assert_not_called()
