"""Time helpers. All timestamps are stored timezone-aware in UTC.

`now()` can be frozen in tests with `freeze(...)`.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import get_settings

_frozen: datetime | None = None


def now() -> datetime:
    return _frozen if _frozen is not None else datetime.now(timezone.utc)


def freeze(dt: datetime | None) -> None:
    """Freeze the clock (tests only). Pass None to unfreeze."""
    global _frozen
    if dt is not None and dt.tzinfo is None:
        raise ValueError("freeze() needs a timezone-aware datetime")
    _frozen = dt.astimezone(timezone.utc) if dt else None


def advance(**kwargs) -> None:
    global _frozen
    if _frozen is None:
        raise RuntimeError("clock is not frozen")
    _frozen = _frozen + timedelta(**kwargs)


def local_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def to_local(dt: datetime) -> datetime:
    return dt.astimezone(local_tz())


def local_date(dt: datetime) -> date:
    return to_local(dt).date()


def today() -> date:
    return local_date(now())


def add_months(d: date, months: int) -> date:
    """Same day-of-month `months` later, clamped to the month's last day."""
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last))


def humanize_age(dt: datetime | None, ref: datetime | None = None) -> str:
    if dt is None:
        return "never"
    ref = ref or now()
    secs = int((ref - dt).total_seconds())
    if secs < 0:
        return "just now"
    if secs < 90:
        return "just now"
    mins = secs // 60
    if mins < 90:
        return f"{mins} minutes ago"
    hours = mins // 60
    if hours < 48:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} days ago"


def parse_iso_datetime(value) -> datetime | None:
    """Parse an ISO-8601 string (or datetime) into an aware UTC datetime; None if impossible."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz())
    return dt.astimezone(timezone.utc)


def parse_iso_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
