"""Local-timezone date helpers for the chatbot.

The DB stores timestamps as TIMESTAMPTZ (UTC). The gym operates in America/Mexico_City, and the
LLM has no notion of "today" unless told — so the agent must (a) be given the current local date
and (b) receive dates already formatted in local time with the weekday name, to avoid off-by-one
day-of-week mistakes.
"""
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/Mexico_City")
except Exception:  # pragma: no cover - falls back if tzdata missing
    LOCAL_TZ = timezone.utc

# datetime.weekday(): 0=Monday .. 6=Sunday
_DAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MONTHS_ES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


def to_local(dt):
    """Convert a tz-aware datetime to local time; leave naive datetimes/values untouched."""
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is not None:
        try:
            return dt.astimezone(LOCAL_TZ)
        except Exception:  # noqa: BLE001
            return dt
    return dt


def local_now() -> datetime:
    return datetime.now(LOCAL_TZ)


def fmt_dt(dt) -> str:
    """Format a datetime in local time with weekday, e.g. 'lunes 15/06/2026 08:00'."""
    if not dt:
        return "?"
    d = to_local(dt)
    try:
        wd = _DAYS_ES[d.weekday()]
    except Exception:  # noqa: BLE001
        wd = ""
    return f"{wd} {d.day:02d}/{d.month:02d}/{d.year} {d.hour:02d}:{d.minute:02d}".strip()


def fmt_now_es() -> str:
    """Current local date/time in Spanish, e.g. 'lunes 15 de junio de 2026, 10:30'."""
    n = local_now()
    return f"{_DAYS_ES[n.weekday()]} {n.day} de {_MONTHS_ES[n.month - 1]} de {n.year}, {n.hour:02d}:{n.minute:02d}"
