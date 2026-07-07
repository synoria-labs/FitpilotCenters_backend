"""time_filters must render a sargable range that is exactly equivalent to
func.date(col) OP d — bucketing by the DB SESSION timezone, not UTC.

Regression guard for the tz bug: an earlier version built the boundary at UTC
midnight, which shifts the day by the session's UTC offset (this deployment runs
Postgres in America/Mexico_City) and silently miscounts sessions/reservations
near midnight. The correct form applies ``AT TIME ZONE current_setting('TimeZone')``.
"""
from datetime import date

from sqlalchemy import Column, DateTime
from sqlalchemy.dialects import postgresql

from app.crud.time_filters import between_dates, from_date, on_date, until_date

_COL = Column("start_at", DateTime(timezone=True))


def _sql(expr):
    return str(
        expr.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_from_date_is_session_tz_lower_bound():
    sql = _sql(from_date(_COL, date(2026, 7, 6)))
    assert "start_at >=" in sql
    assert "current_setting('TimeZone')" in sql
    assert "'2026-07-06'" in sql
    # must NOT wrap the column (that would defeat the index)
    assert "date(start_at)" not in sql.lower()


def test_until_date_is_half_open_next_day():
    sql = _sql(until_date(_COL, date(2026, 7, 6)))
    assert "start_at <" in sql
    # half-open: upper bound is the NEXT day's midnight
    assert "'2026-07-07'" in sql


def test_on_date_is_single_day_range():
    sql = _sql(on_date(_COL, date(2026, 7, 6)))
    assert "start_at >=" in sql and "start_at <" in sql
    assert "'2026-07-06'" in sql and "'2026-07-07'" in sql


def test_between_dates_spans_inclusive():
    sql = _sql(between_dates(_COL, date(2026, 7, 1), date(2026, 7, 31)))
    assert "'2026-07-01'" in sql
    # inclusive end -> half-open at Aug 1
    assert "'2026-08-01'" in sql


def test_no_utc_midnight_regression():
    """The boundary must go through the session timezone, never a bare UTC cast."""
    sql = _sql(from_date(_COL, date(2026, 7, 6))).lower()
    assert "current_setting('timezone')" in sql, sql
