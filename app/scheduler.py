"""In-process scheduler: a fallback sync every few hours (in case webhooks are missed) and
an hourly check for Apple Pay taps that never matched. Runs in the single web process; the
per-connection advisory lock makes overlapping syncs harmless if more than one process runs."""

from __future__ import annotations

import asyncio
import logging

from app import jobs
from app.config import get_settings

log = logging.getLogger(__name__)


async def _every(seconds: float, fn, first_delay: float) -> None:
    await asyncio.sleep(first_delay)
    while True:
        try:
            await asyncio.to_thread(fn)
        except Exception:  # noqa: BLE001
            log.exception("scheduled job %s failed", getattr(fn, "__name__", fn))
        await asyncio.sleep(seconds)


def start() -> list[asyncio.Task]:
    s = get_settings()
    log.info("scheduler: fallback sync every %sh, missing-event check every %smin", s.fallback_sync_hours, s.missing_event_check_minutes)
    return [
        asyncio.create_task(_every(s.fallback_sync_hours * 3600, jobs.sync_all, first_delay=90)),
        asyncio.create_task(_every(s.missing_event_check_minutes * 60, jobs.mark_missing, first_delay=120)),
    ]
