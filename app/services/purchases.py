"""Purchases: one real-world charge with its evidence (wallet event and/or Plaid transaction)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, Card, PlaidTransaction, Purchase, WalletEvent
from app.money import fmt
from app.services import ledger, review
from app.services.merchants import classify_card_txn
from app.services.settings_store import tracking_start_date
from app.timeutil import now


def purchase_for_txn(session: Session, txn: PlaidTransaction) -> Purchase | None:
    return session.scalar(select(Purchase).where(Purchase.plaid_transaction_id == txn.id))


def purchase_for_event(session: Session, ev: WalletEvent) -> Purchase | None:
    return session.scalar(select(Purchase).where(Purchase.wallet_event_id == ev.id))


def card_for_account(session: Session, account: Account) -> Card | None:
    if account.role != "credit_card":
        return None
    return session.scalar(select(Card).where(Card.account_id == account.id))


def ensure_purchase_for_txn(session: Session, txn: PlaidTransaction, why: str) -> Purchase | None:
    """Make sure a live card transaction is counted exactly once, and keep its ledger in sync."""
    card = card_for_account(session, txn.account)
    existing = purchase_for_txn(session, txn)
    cls = classify_card_txn(txn)
    if existing is not None:
        existing.kind = "refund" if cls == "refund" else "purchase"
        if cls == "payment" and existing.wallet_event_id is None and existing.state == "active":
            existing.state = "excluded"
            existing.state_reason = "Plaid now classifies this as a card payment, which doesn't add card spend"
        existing.updated_at = now()
        ledger.sync_purchase(session, existing, why)
        return existing
    if card is None or txn.is_removed or cls == "payment":
        return None
    if txn.effective_date < tracking_start_date(session):
        return None
    p = Purchase(
        card_id=card.id,
        merchant_id=txn.merchant_id,
        plaid_transaction_id=txn.id,
        kind="refund" if cls == "refund" else "purchase",
        state="active",
        created_at=now(),
        updated_at=now(),
    )
    session.add(p)
    session.flush()
    ledger.sync_purchase(session, p, why)
    return p


def ensure_purchase_for_event(session: Session, ev: WalletEvent) -> Purchase:
    p = purchase_for_event(session, ev)
    if p is None:
        p = Purchase(
            card_id=ev.card_id,
            merchant_id=ev.merchant_id,
            wallet_event_id=ev.id,
            kind="purchase",
            state="active" if ev.status == "accepted" else "duplicate",
            created_at=now(),
            updated_at=now(),
        )
        session.add(p)
        session.flush()
    ledger.sync_purchase(session, p, f"Apple Pay event #{ev.id} received")
    return p


def link(
    session: Session,
    ev: WalletEvent,
    txn: PlaidTransaction,
    *,
    confidence: str,
    score: float | None,
    reason: str,
    actor: str = "system",
    details: dict | None = None,
) -> Purchase:
    """Attach a Plaid transaction to a wallet event's purchase. The Plaid amount replaces the
    provisional amount and the difference (e.g. a tip) is recorded."""
    p_e = ensure_purchase_for_event(session, ev)
    p_t = purchase_for_txn(session, txn)
    if p_t is not None and p_t.id != p_e.id:
        if p_t.wallet_event_id is not None:
            raise ValueError(f"transaction {txn.id} is already linked to Apple Pay event {p_t.wallet_event_id}")
        # The transaction was being counted on its own; fold it into the event's purchase.
        p_t.plaid_transaction_id = None
        p_t.state = "merged"
        p_t.merged_into_id = p_e.id
        p_t.state_reason = f"merged into purchase #{p_e.id} (matched to Apple Pay event #{ev.id})"
        p_t.updated_at = now()
        session.flush()
        ledger.sync_purchase(session, p_t, f"Merged into purchase #{p_e.id}")
    old = p_e.plaid_transaction
    if old is not None and old.id != txn.id:
        p_e.plaid_transaction_id = None
        session.flush()
        ensure_purchase_for_txn(session, old, f"Detached from Apple Pay event #{ev.id}")

    p_e.plaid_transaction_id = txn.id
    session.flush()
    session.refresh(p_e, ["plaid_transaction"])
    card = card_for_account(session, txn.account)
    p_e.card_id = card.id if card else p_e.card_id
    p_e.merchant_id = p_e.merchant_id or txn.merchant_id
    p_e.match_confidence = confidence
    p_e.match_score = score
    if p_e.state != "active" and p_e.state != "duplicate":
        p_e.state = "active"
    p_e.amount_difference_cents = (txn.amount_cents - ev.amount_cents) if ev.amount_cents is not None else None
    p_e.updated_at = now()
    diff_note = ""
    if p_e.amount_difference_cents:
        diff_note = f"; difference {fmt(p_e.amount_difference_cents, signed=True)} vs tapped {fmt(ev.amount_cents)}"
    ledger.sync_purchase(session, p_e, f"Matched to Plaid ({confidence}){diff_note}")
    review.decide(
        session,
        "confirmed_by_user" if actor == "user" else ("auto_linked" if confidence == "high" else "linked_needs_review"),
        reason,
        wallet_event_id=ev.id,
        plaid_transaction_id=txn.id,
        confidence=confidence,
        score=score,
        details=details,
        actor=actor,
    )
    review.resolve_touching(
        session,
        wallet_event_ids=[ev.id],
        txn_ids=[txn.id],
        kinds=("ambiguous_match", "low_confidence_match", "missing_after_5_days", "unmapped_card"),
        resolution="auto_resolved" if actor == "system" else "confirmed",
        note=f"Linked event #{ev.id} to transaction #{txn.id}",
    )
    review.resolve_ambiguous_mentioning(session, ev.id, f"Event #{ev.id} linked to transaction #{txn.id}")
    return p_e


def unlink(session: Session, p_e: Purchase, reason: str, actor: str = "system", decision: str = "unlinked") -> PlaidTransaction | None:
    """Separate a wallet event's purchase from its Plaid transaction; each is then counted on its own."""
    txn = p_e.plaid_transaction
    if txn is None:
        return None
    p_e.plaid_transaction_id = None
    p_e.match_confidence = None
    p_e.match_score = None
    p_e.amount_difference_cents = None
    p_e.updated_at = now()
    session.flush()
    session.refresh(p_e, ["plaid_transaction"])
    ledger.sync_purchase(session, p_e, f"Unlinked from Plaid transaction #{txn.id}: {reason}")
    ensure_purchase_for_txn(session, txn, f"Unlinked from Apple Pay event #{p_e.wallet_event_id}")
    review.decide(session, decision, reason, wallet_event_id=p_e.wallet_event_id, plaid_transaction_id=txn.id, actor=actor)
    return txn
