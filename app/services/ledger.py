"""Append-only ledger for 'card spend not yet covered'.

Pattern: every purchase and every reserve transfer has a *target* contribution that
is derived from the current evidence. When evidence changes we append one entry for
the difference between the target and the sum of that object's existing entries.
History is never edited; the current value is always the sum of entries.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import LedgerEntry, Purchase, ReserveTransfer
from app.money import fmt
from app.timeutil import now


def post(
    session: Session,
    entry_type: str,
    amount_cents: int,
    reason: str,
    *,
    purchase: Purchase | None = None,
    transfer: ReserveTransfer | None = None,
    card_id: int | None = None,
    details: dict | None = None,
) -> LedgerEntry | None:
    if amount_cents == 0:
        return None
    entry = LedgerEntry(
        created_at=now(),
        entry_type=entry_type,
        amount_cents=int(amount_cents),
        purchase_id=purchase.id if purchase else None,
        reserve_transfer_id=transfer.id if transfer else None,
        card_id=card_id if card_id is not None else (purchase.card_id if purchase else None),
        reason=reason,
        details=details,
    )
    session.add(entry)
    session.flush()
    return entry


def purchase_sum(session: Session, purchase_id: int) -> int:
    return int(session.scalar(select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0)).where(LedgerEntry.purchase_id == purchase_id)))


def transfer_sum(session: Session, transfer_id: int) -> int:
    return int(
        session.scalar(
            select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0)).where(LedgerEntry.reserve_transfer_id == transfer_id)
        )
    )


def total(session: Session) -> int:
    """Raw sum of every ledger entry (can be negative if the reserve is over-funded)."""
    return int(session.scalar(select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0))))


def purchase_target(p: Purchase) -> int:
    """What this purchase should currently contribute, from the evidence we have."""
    if p.state != "active":
        return 0
    t = p.plaid_transaction
    if t is not None:
        # Plaid is authoritative. A removed transaction (pending dropped, reversal) releases it.
        return 0 if t.is_removed else int(t.amount_cents)
    e = p.wallet_event
    if e is not None and e.status == "accepted" and e.amount_cents and e.amount_cents > 0:
        return int(e.amount_cents)
    return 0


def describe_purchase(p: Purchase) -> str:
    t, e = p.plaid_transaction, p.wallet_event
    if t is not None:
        state = "removed by Plaid" if t.is_removed else ("pending" if t.pending else "posted")
        return f"Plaid {state} '{t.display_name}' {fmt(t.amount_cents)}"
    if e is not None:
        return f"Apple Pay tap '{e.merchant_raw or '?'}' {fmt(e.amount_cents)} (provisional)"
    return "purchase"


def sync_purchase(session: Session, p: Purchase, why: str) -> LedgerEntry | None:
    """Append an entry so this purchase's ledger sum equals its target. Returns the entry or None."""
    session.flush()
    current = purchase_sum(session, p.id)
    target = purchase_target(p)
    delta = target - current
    if delta == 0:
        return None
    if target == 0:
        etype = "purchase_release"
    elif current == 0:
        if target < 0:
            etype = "refund"
        elif p.plaid_transaction is not None:
            etype = "purchase_plaid"
        else:
            etype = "purchase_provisional"
    else:
        etype = "purchase_adjustment"
    reason = f"{why} — {describe_purchase(p)}; was {fmt(current)}, now {fmt(target)}"
    return post(
        session,
        etype,
        delta,
        reason,
        purchase=p,
        details={"previous_cents": current, "target_cents": target, "state": p.state},
    )


def sync_transfer(session: Session, tr: ReserveTransfer, target: int, why: str) -> LedgerEntry | None:
    session.flush()
    current = transfer_sum(session, tr.id)
    delta = target - current
    if delta == 0:
        return None
    etype = "reserve_transfer_left_checking" if delta < 0 else "reserve_transfer_reversed"
    return post(
        session,
        etype,
        delta,
        f"{why} — transfer #{tr.id} of {fmt(tr.amount_cents)}; was {fmt(current)}, now {fmt(target)}",
        transfer=tr,
        details={"previous_cents": current, "target_cents": target, "status": tr.status},
    )
