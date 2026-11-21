"""Things I can do from the review queue. Each action is recorded in match_decisions."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PlaidTransaction, Purchase, ReviewItem, WalletEvent
from app.services import ledger, review
from app.services.matching import run_matching
from app.services.purchases import ensure_purchase_for_event, link, purchase_for_event, purchase_for_txn, unlink
from app.timeutil import now


class ReviewError(ValueError):
    pass


def _event(session: Session, event_id: int | None) -> WalletEvent:
    ev = session.get(WalletEvent, event_id) if event_id else None
    if ev is None:
        raise ReviewError("Apple Pay event not found")
    return ev


def _txn(session: Session, txn_id: int | None) -> PlaidTransaction:
    t = session.get(PlaidTransaction, txn_id) if txn_id else None
    if t is None:
        raise ReviewError("Plaid transaction not found")
    return t


def confirm_match(session: Session, item: ReviewItem, wallet_event_id: int | None = None, txn_id: int | None = None) -> Purchase:
    """Confirm that an Apple Pay event and a Plaid transaction are the same purchase."""
    ev = _event(session, wallet_event_id or item.wallet_event_id)
    t = _txn(session, txn_id or item.plaid_transaction_id)
    if t.is_removed:
        raise ReviewError("That Plaid transaction was removed by the bank")
    holder = purchase_for_txn(session, t)
    if holder is not None and holder.wallet_event_id not in (None, ev.id):
        # The transaction belongs to a different tap; separate them first.
        unlink(session, holder, f"I matched transaction #{t.id} to event #{ev.id} instead", actor="user")
    p = link(session, ev, t, confidence="confirmed", score=None, reason="Confirmed by me in the review queue", actor="user")
    review.resolve(session, item, "confirmed", f"event #{ev.id} = transaction #{t.id}")
    run_matching(session)
    return p


def reject_match(session: Session, item: ReviewItem) -> None:
    """These are NOT the same purchase. Never pair them again."""
    pairs: list[tuple[int, int]] = []
    if item.kind == "ambiguous_match":
        pairs = [(c["wallet_event_id"], item.plaid_transaction_id) for c in (item.details or {}).get("candidates", [])
                 if c.get("plaid_transaction_id") == item.plaid_transaction_id]
    elif item.wallet_event_id and item.plaid_transaction_id:
        pairs = [(item.wallet_event_id, item.plaid_transaction_id)]
    for ev_id, t_id in pairs:
        p = session.scalar(select(Purchase).where(Purchase.wallet_event_id == ev_id))
        if p is not None and p.plaid_transaction_id == t_id:
            unlink(session, p, "Rejected by me", actor="user", decision="unlinked")
        review.decide(session, "rejected_by_user", "I said these are different purchases", wallet_event_id=ev_id,
                      plaid_transaction_id=t_id, actor="user")
        t = session.get(PlaidTransaction, t_id)
        if t is not None:
            from app.services.purchases import ensure_purchase_for_txn

            ensure_purchase_for_txn(session, t, "Match rejected by me: counted on its own")
    review.resolve(session, item, "rejected")
    run_matching(session)


def mark_duplicate(session: Session, item: ReviewItem | None, wallet_event_id: int | None = None) -> None:
    """The Apple Pay event is a duplicate of a purchase already counted: stop counting it."""
    ev = _event(session, wallet_event_id or (item.wallet_event_id if item else None))
    p = purchase_for_event(session, ev) or ensure_purchase_for_event(session, ev)
    if p.plaid_transaction is not None:
        # The Plaid charge is real; keep counting it on its own.
        unlink(session, p, "Apple Pay event marked as a duplicate by me", actor="user")
    p.state = "duplicate"
    p.state_reason = "Marked as a duplicate by me"
    p.updated_at = now()
    ev.status = "duplicate"
    ledger.sync_purchase(session, p, "Marked as a duplicate by me")
    review.decide(session, "marked_duplicate", "I marked this Apple Pay event as a duplicate", wallet_event_id=ev.id, actor="user")
    review.resolve_touching(session, wallet_event_ids=[ev.id], kinds=("medium_match", "low_confidence_match", "unmapped_card",
                            "missing_after_5_days", "amount_problem"), resolution="duplicate", note="marked duplicate")
    review.resolve_ambiguous_mentioning(session, ev.id, f"event #{ev.id} marked duplicate")
    if item is not None:
        review.resolve(session, item, "duplicate")
    run_matching(session)


def dismiss_event(session: Session, item: ReviewItem) -> None:
    """The tap never turned into a charge (declined, cancelled, refunded): release it."""
    ev = _event(session, item.wallet_event_id)
    p = purchase_for_event(session, ev)
    if p is not None:
        if p.plaid_transaction is not None:
            unlink(session, p, "Apple Pay event dismissed by me", actor="user")
        p.state = "dismissed"
        p.state_reason = "Dismissed by me (never charged / refunded)"
        p.updated_at = now()
        ledger.sync_purchase(session, p, "Dismissed by me: never charged or refunded")
    review.decide(session, "dismissed_by_user", "I said this tap never became a charge", wallet_event_id=ev.id, actor="user")
    review.resolve(session, item, "dismissed")


def keep_counting(session: Session, item: ReviewItem) -> None:
    review.decide(session, "kept_by_user", f"I chose to keep this as is ({item.kind})", wallet_event_id=item.wallet_event_id,
                  plaid_transaction_id=item.plaid_transaction_id, actor="user")
    review.resolve(session, item, "kept")
