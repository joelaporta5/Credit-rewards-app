"""Apple Pay (Wallet) events sent by the iPhone Shortcut.

Every field may be missing or oddly formatted. We store the raw payload, parse what we
can, record warnings, and never reject an event just because a field is messy.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import LOCK_WALLET_DEDUPE, advisory_xact_lock
from app.models import WalletCardAlias, WalletEvent
from app.money import fmt, parse_amount
from app.services import review
from app.services.merchants import clean_merchant, get_or_create_merchant
from app.timeutil import local_tz, now, parse_iso_datetime

DUPLICATE_WINDOW_SECONDS = 120
MAX_FIELD = 255

_KEYS = {
    "amount": ["amount", "total", "price", "value", "amt"],
    "merchant": ["merchant", "merchantname", "merchant_name", "store", "payee", "name", "description"],
    "card": ["card", "cardname", "card_name", "cardorpass", "card_or_pass", "pass", "passname", "wallet_card", "account"],
    "timestamp": ["timestamp", "time", "date", "datetime", "event_time", "eventtime", "tapped_at", "currentdate"],
    "id": ["id", "event_id", "eventid", "idempotency_key", "idempotencykey", "uuid"],
}

_DATE_FORMATS = [
    "%b %d, %Y at %I:%M %p",
    "%b %d, %Y at %I:%M:%S %p",
    "%B %d, %Y at %I:%M %p",
    "%B %d, %Y at %I:%M:%S %p",
    "%m/%d/%y, %I:%M %p",
    "%m/%d/%Y, %I:%M %p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%d %b %Y at %H:%M",
]


def _norm_key(k: str) -> str:
    return re.sub(r"[\s_\-]", "", str(k)).lower()


def _pick(payload: dict, field: str):
    normed = {_norm_key(k): v for k, v in payload.items()}
    for cand in _KEYS[field]:
        v = normed.get(_norm_key(cand))
        if v not in (None, ""):
            return v
    return None


def _text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        v = str(v)
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s[:MAX_FIELD] or None


def normalize_card_name(name: str | None) -> str:
    if not name:
        return ""
    s = name.upper().replace("•", " ").replace("*", " ")
    return re.sub(r"\s+", " ", s).strip()


def parse_timestamp(raw, received: datetime) -> tuple[datetime, str, list[str]]:
    """Returns (event_at, source, warnings). Falls back to the time we received it."""
    if raw is None or raw == "":
        return received, "received", ["timestamp missing; used time received"]
    dt = None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        secs = raw / 1000 if raw > 10_000_000_000 else raw
        try:
            dt = datetime.fromtimestamp(secs, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            dt = None
    else:
        s = re.sub(r"\s+", " ", str(raw).replace(" ", " ").replace(" ", " ")).strip()
        dt = parse_iso_datetime(s)
        if dt is None:
            for f in _DATE_FORMATS:
                try:
                    dt = datetime.strptime(s, f).replace(tzinfo=local_tz()).astimezone(timezone.utc)
                    break
                except ValueError:
                    continue
    if dt is None:
        return received, "received", [f"timestamp not understood: {str(raw)[:60]!r}; used time received"]
    if dt > received + timedelta(minutes=10) or dt < received - timedelta(days=3):
        return received, "received", [f"timestamp {dt.isoformat()} is implausible; used time received"]
    return dt, "payload", []


def dedupe_key(amount_cents: int | None, merchant: str | None, card: str | None) -> str:
    basis = f"{amount_cents}|{clean_merchant(merchant)}|{normalize_card_name(card)}"
    return hashlib.sha256(basis.encode()).hexdigest()


def get_or_create_alias(session: Session, card_name: str | None) -> WalletCardAlias | None:
    norm = normalize_card_name(card_name)
    if not norm:
        return None
    alias = session.scalar(select(WalletCardAlias).where(WalletCardAlias.normalized_name == norm))
    if alias is None:
        alias = WalletCardAlias(normalized_name=norm, display_name=card_name.strip()[:MAX_FIELD], first_seen_at=now(), updated_at=now())
        session.add(alias)
        session.flush()
    return alias


def ingest(session: Session, payload) -> WalletEvent:
    """Store an event (fast path for the HTTP request). Matching happens in process()."""
    received = now()
    if not isinstance(payload, dict):
        payload = {"_unstructured": str(payload)[:2000]}
    warnings: list[str] = []
    pa = parse_amount(_pick(payload, "amount"))
    warnings += pa.warnings
    merchant = _text(_pick(payload, "merchant"))
    if merchant is None:
        warnings.append("merchant missing")
    card = _text(_pick(payload, "card"))
    if card is None:
        warnings.append("card name missing")
    event_at, ts_source, ts_warn = parse_timestamp(_pick(payload, "timestamp"), received)
    warnings += ts_warn
    client_id = _text(_pick(payload, "id"))
    key = dedupe_key(pa.cents, merchant, card)

    # Serialize concurrent deliveries of the same purchase (Shortcuts sometimes fires twice at once).
    advisory_xact_lock(session, LOCK_WALLET_DEDUPE, int(key[:8], 16) & 0x7FFFFFFF)
    duplicate_of = None
    if client_id:
        duplicate_of = session.scalar(
            select(WalletEvent).where(WalletEvent.client_event_id == client_id, WalletEvent.status == "accepted")
        )
    if duplicate_of is None:
        window = timedelta(seconds=DUPLICATE_WINDOW_SECONDS)
        duplicate_of = session.scalar(
            select(WalletEvent)
            .where(
                WalletEvent.dedupe_key == key,
                WalletEvent.status == "accepted",
                WalletEvent.received_at >= received - window,
            )
            .order_by(WalletEvent.id)
        )

    alias = get_or_create_alias(session, card)
    ev = WalletEvent(
        received_at=received,
        client_event_id=client_id,
        raw_payload=payload,
        amount_cents=pa.cents,
        currency=pa.currency,
        merchant_raw=merchant,
        card_name_raw=card,
        card_alias_id=alias.id if alias else None,
        event_at=event_at,
        event_at_source=ts_source,
        parse_warnings=warnings or None,
        dedupe_key=key,
        duplicate_of_id=duplicate_of.id if duplicate_of else None,
        status="duplicate" if duplicate_of else "accepted",
    )
    session.add(ev)
    session.flush()
    return ev


def process(session: Session, event_id: int) -> None:
    """Background step: merchant, purchase + ledger entry, review items, matching."""
    from app.services.matching import run_matching
    from app.services.purchases import ensure_purchase_for_event

    ev = session.get(WalletEvent, event_id)
    if ev is None or ev.processed_at is not None:
        return
    if ev.status == "duplicate":
        review.decide(
            session,
            "duplicate_delivery",
            f"Same amount, merchant and card as event #{ev.duplicate_of_id} within {DUPLICATE_WINDOW_SECONDS}s "
            f"(or same client id); counted once",
            wallet_event_id=ev.id,
            details={"duplicate_of": ev.duplicate_of_id},
        )
        ev.processed_at = now()
        return
    m = get_or_create_merchant(session, ev.merchant_raw)
    ev.merchant_id = m.id if m else None
    ensure_purchase_for_event(session, ev)
    if ev.amount_cents is None or ev.amount_cents <= 0:
        review.open_item(
            session,
            "amount_problem",
            f"Apple Pay event from '{ev.merchant_raw or '?'}' has no usable amount "
            f"({(ev.raw_payload or {}).get('amount', 'missing')!r}). Not counted; Plaid will count the real charge.",
            wallet_event_id=ev.id,
            details={"warnings": ev.parse_warnings},
        )
    if ev.card_id is None:
        review.open_item(
            session,
            "unmapped_card",
            f"Wallet card '{ev.card_name_raw or '(none sent)'}' isn't mapped to a Plaid card. "
            f"{fmt(ev.amount_cents)} at '{ev.merchant_raw or '?'}' is counted against safe-to-spend anyway.",
            wallet_event_id=ev.id,
        )
    ev.processed_at = now()
    session.flush()
    run_matching(session)
