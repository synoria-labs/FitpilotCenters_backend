"""Sargable date-range filters for timestamptz columns.

``func.date(column) >= some_date`` wraps the column in a function, so Postgres
cannot use an index on the column (e.g. ``idx_sessions_time``/``idx_sessions_template``)
and falls back to scanning. These helpers express the same predicate as a plain
range over the column, which IS index-usable.

Exactness: ``func.date(timestamptz)`` buckets by the DB **session** timezone
(this deployment runs Postgres in ``America/Mexico_City``, and the app does not
override it on connect). So the day boundary here is built as midnight of the
target date IN THE SESSION TIMEZONE via ``AT TIME ZONE current_setting('TimeZone')``
— NOT UTC — so the range is byte-for-byte equivalent to the old ``func.date()``
predicate regardless of what the session timezone is. The RHS is a stable
constant (no reference to the column), so the predicate stays sargable.
"""
from datetime import date, timedelta

from sqlalchemy import and_, cast, func
from sqlalchemy.types import TIMESTAMP


def _local_day_start(d: date):
    """Midnight of ``d`` in the DB session timezone, as a timestamptz constant.

    ``timezone(current_setting('TimeZone'), d::timestamp)`` interprets ``d 00:00``
    as local wall-clock time in the session zone and yields the corresponding
    instant — matching how ``func.date`` derives the calendar day.
    """
    return func.timezone(
        func.current_setting("TimeZone"),
        cast(d, TIMESTAMP(timezone=False)),
    )


def from_date(column, d: date):
    """``func.date(column) >= d`` — sargable form."""
    return column >= _local_day_start(d)


def until_date(column, d: date):
    """``func.date(column) <= d`` — sargable form (half-open upper bound)."""
    return column < _local_day_start(d + timedelta(days=1))


def between_dates(column, start: date, end: date):
    """``func.date(column) BETWEEN start AND end`` — sargable form."""
    return and_(from_date(column, start), until_date(column, end))


def on_date(column, d: date):
    """``func.date(column) == d`` — sargable form."""
    return between_dates(column, d, d)
