"""Jinja2 templates and filters."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from app import timeutil
from app.money import fmt

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _local(dt, f="%b %d, %I:%M %p"):
    if dt is None:
        return "—"
    return timeutil.to_local(dt).strftime(f)


def _date(d, f="%a %b %d"):
    return d.strftime(f) if d else "—"


templates.env.filters["money"] = fmt
# "1234.56" for form fields (integer math, no floats)
templates.env.filters["plain"] = lambda c: f"{int(c) // 100}.{int(c) % 100:02d}" if c is not None and c >= 0 else ""
templates.env.filters["smoney"] = lambda c: fmt(c, signed=True)
templates.env.filters["age"] = timeutil.humanize_age
templates.env.filters["local"] = _local
templates.env.filters["day"] = _date
templates.env.globals["app_name"] = "Rewards Autopilot"
