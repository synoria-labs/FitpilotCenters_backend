"""Shared helpers for the special-events CRUD modules."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.core.timezones import to_local

_WEEKDAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MONTHS_ES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


class SpecialEventError(ValueError):
    """A business rule refused the operation. The message is shown to staff/customers as is."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def participant_key(phone: Optional[str]) -> Optional[str]:
    """Normalize a phone so "+52 871...", "521871..." and "871..." are the same participant.

    Mexican numbers arrive with and without the 52/521 prefixes (WhatsApp ids carry "521"), so
    the last ten digits identify the line. Shorter values are kept as typed.
    """
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if not digits:
        return None
    return digits[-10:] if len(digits) >= 10 else digits


def local_date_label(value: datetime, tz_name: Optional[str] = None) -> str:
    """'viernes 30 de octubre de 2026' in the event's zone."""
    local = to_local(value, tz_name)
    return (
        f"{_WEEKDAYS_ES[local.weekday()]} {local.day} de {_MONTHS_ES[local.month - 1]} "
        f"de {local.year}"
    )


def local_time_label(value: datetime, tz_name: Optional[str] = None) -> str:
    """'19:00' in the event's zone."""
    local = to_local(value, tz_name)
    return f"{local.hour:02d}:{local.minute:02d}"


def local_schedule_label(
    start_at: datetime, end_at: Optional[datetime], tz_name: Optional[str] = None
) -> str:
    """'viernes 30 de octubre de 2026, de 19:00 a 21:00' (or 'a las 19:00' without an end)."""
    day = local_date_label(start_at, tz_name)
    start = local_time_label(start_at, tz_name)
    if end_at is None:
        return f"{day}, a las {start}"
    return f"{day}, de {start} a {local_time_label(end_at, tz_name)}"


def money_label(amount, currency: str = "MXN") -> str:
    return f"${float(amount):,.2f} {currency}"
