"""Background jobs (run after the HTTP response is sent, or by the scheduler).

Each job opens its own DB session and never raises: failures are logged and shown on
the dashboard via the connection's last_sync_error.
"""

from __future__ import annotations

import logging

from app.db import session_scope
from app.services import matching, plaid_sync, wallet

log = logging.getLogger(__name__)


def process_wallet_event(event_id: int) -> None:
    try:
        with session_scope() as s:
            wallet.process(s, event_id)
    except Exception:  # noqa: BLE001
        log.exception("processing wallet event %s failed", event_id)


def sync_item(item_id: int, trigger: str, force_liabilities: bool = False, force_balance: bool = False) -> None:
    try:
        plaid_sync.sync_item(item_id, trigger, force_liabilities=force_liabilities, force_balance=force_balance)
    except Exception:  # noqa: BLE001
        log.exception("sync of item %s failed", item_id)


def sync_all(trigger: str = "schedule") -> None:
    try:
        plaid_sync.sync_all(trigger)
    except Exception:  # noqa: BLE001
        log.exception("sync_all failed")


def mark_missing() -> None:
    try:
        with session_scope() as s:
            n = matching.mark_missing_events(s)
        if n:
            log.info("%s Apple Pay events sent to review as possibly missing", n)
    except Exception:  # noqa: BLE001
        log.exception("missing-event check failed")
