"""User-adjustable settings stored in the database (with defaults)."""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from app.models import AppSetting, AuditLog
from app.timeutil import now, parse_iso_date, today

DEFAULTS: dict[str, str] = {
    "minimum_buffer_cents": "10000",  # $100
    "uncertainty_holdback_cents": "25000",  # $250
    "stale_threshold_hours": "12",
    "card_stale_threshold_hours": "24",
    "bills_window_days": "30",
    "missing_after_days": "5",
    "tracking_start_date": "",  # set automatically the first time it's needed
}

INT_KEYS = {
    "minimum_buffer_cents",
    "uncertainty_holdback_cents",
    "stale_threshold_hours",
    "card_stale_threshold_hours",
    "bills_window_days",
    "missing_after_days",
}


def get(session: Session, key: str) -> str:
    row = session.get(AppSetting, key)
    return row.value if row else DEFAULTS.get(key, "")


def get_int(session: Session, key: str) -> int:
    return int(get(session, key) or DEFAULTS[key] or 0)


def set_value(session: Session, key: str, value: str, actor: str = "user") -> None:
    if key not in DEFAULTS:
        raise KeyError(key)
    if key in INT_KEYS:
        if int(value) < 0:
            raise ValueError(f"{key} can't be negative")
    row = session.get(AppSetting, key)
    old = row.value if row else DEFAULTS[key]
    if row is None:
        session.add(AppSetting(key=key, value=value, updated_at=now()))
    else:
        row.value, row.updated_at = value, now()
    if old != value:
        session.add(AuditLog(actor=actor, action="setting_changed", details={"key": key, "old": old, "new": value}))
    session.flush()


def tracking_start_date(session: Session) -> date:
    """Purchases dated before this day are not counted (they belong to statements you
    already planned for). Defaults to the first day the app needed it."""
    v = parse_iso_date(get(session, "tracking_start_date"))
    if v is None:
        v = today()
        set_value(session, "tracking_start_date", v.isoformat(), actor="system")
    return v
