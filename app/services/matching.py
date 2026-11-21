"""Matching engine: pairs Apple Pay (Wallet) events with Plaid transactions.

Rules (from the Sprint 1 spec):
- Candidate: same mapped card, Plaid date 0-3 days after the tap, similar merchant name.
- Amount tolerance by merchant type:
    default         exact or within $0.01
    restaurant/bar  posted may be up to 35% higher than tapped (tip)
    gas             any amount (pending holds); match on merchant + timing
    hotel/car rental any amount (holds); match on merchant + timing
- Score each candidate. High: auto-link. Medium: link + review. Low or tied: no link, review.
- Every decision is recorded with its reason (match_decisions).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db import LOCK_MATCHING, advisory_xact_lock
from app.models import Account, MatchDecision, PlaidTransaction, Purchase, WalletEvent, WalletCardAlias
from app.money import fmt
from app.services import review
from app.services.merchants import CAR_RENTAL, GAS, LODGING, RESTAURANT, best_similarity, category_for_txn
from app.services.purchases import card_for_account, ensure_purchase_for_txn, link
from app.timeutil import local_date, now

HIGH = 0.85
MEDIUM = 0.70
MIN_MERCHANT = 0.55
TIE_EPSILON = 0.015
MAX_DAYS_AFTER = 3
TIP_MAX_PERCENT = 35
LOOKBACK_DAYS = 30

W_MERCHANT, W_AMOUNT, W_TIME = 0.45, 0.35, 0.20


@dataclass
class Candidate:
    event: WalletEvent
    txn: PlaidTransaction
    score: float
    merchant_score: float
    amount_rule: str
    amount_score: float
    time_score: float
    day_offset: int
    category: str
    notes: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> str:
        if self.score >= HIGH:
            return "high"
        if self.score >= MEDIUM:
            return "medium"
        return "low"

    def explain(self) -> str:
        return (
            f"score {self.score:.3f} = merchant {self.merchant_score:.2f}×{W_MERCHANT} + amount "
            f"{self.amount_score:.2f}×{W_AMOUNT} ({self.amount_rule}, category {self.category}) + timing "
            f"{self.time_score:.2f}×{W_TIME} ({self.day_offset} day(s) after tap); tapped {fmt(self.event.amount_cents)}, "
            f"Plaid {fmt(self.txn.amount_cents)} {'pending' if self.txn.pending else 'posted'}"
        )

    def as_dict(self) -> dict:
        return {
            "wallet_event_id": self.event.id,
            "plaid_transaction_id": self.txn.id,
            "score": round(self.score, 4),
            "merchant_score": round(self.merchant_score, 3),
            "amount_rule": self.amount_rule,
            "category": self.category,
            "day_offset": self.day_offset,
            "tapped_cents": self.event.amount_cents,
            "plaid_cents": self.txn.amount_cents,
        }


def amount_rule(category: str, tapped: int, plaid: int) -> tuple[str, float] | None:
    """Return (rule, score) if the amounts are compatible for this merchant category, else None."""
    diff = plaid - tapped
    if abs(diff) <= 1:
        return "exact", 1.0
    if category == RESTAURANT and diff > 0:
        allowed = (tapped * TIP_MAX_PERCENT + 99) // 100 + 1
        if diff <= allowed:
            return "tip", 0.8
    if category in (GAS, LODGING, CAR_RENTAL):
        return "hold", 0.7
    return None


def _time_score(ev: WalletEvent, txn: PlaidTransaction) -> tuple[float, int] | None:
    ev_day = local_date(ev.event_at)
    offsets = [(d - ev_day).days for d in {txn.authorized_date, txn.txn_date} if d is not None]
    valid = [o for o in offsets if 0 <= o <= MAX_DAYS_AFTER]
    if not valid:
        return None
    off = min(valid)
    score = {0: 1.0, 1: 0.9, 2: 0.75, 3: 0.6}[off]
    if txn.authorized_datetime is not None:
        delta = abs((txn.authorized_datetime - ev.event_at).total_seconds())
        if delta <= 15 * 60:
            score = 1.0
        elif delta <= 2 * 3600:
            score = max(score, 0.97)
        elif delta > 36 * 3600:
            score = min(score, 0.6)
    return score, off


def score_pair(ev: WalletEvent, txn: PlaidTransaction) -> Candidate | None:
    if ev.amount_cents is None or ev.amount_cents <= 0 or txn.amount_cents <= 0:
        return None
    t = _time_score(ev, txn)
    if t is None:
        return None
    time_score, offset = t
    merchant_score = best_similarity(ev.merchant_raw, txn)
    if merchant_score < MIN_MERCHANT:
        return None
    category, _src = category_for_txn(txn, txn.merchant)
    ar = amount_rule(category, ev.amount_cents, txn.amount_cents)
    if ar is None:
        return None
    rule, amount_score = ar
    if rule == "exact":
        amount_score = 1.0
    score = W_MERCHANT * merchant_score + W_AMOUNT * amount_score + W_TIME * time_score
    return Candidate(ev, txn, score, merchant_score, rule, amount_score, time_score, offset, category)


def _rejected_pairs(session: Session) -> set[tuple[int, int]]:
    rows = session.execute(
        select(MatchDecision.wallet_event_id, MatchDecision.plaid_transaction_id).where(
            MatchDecision.decision == "rejected_by_user"
        )
    ).all()
    return {(a, b) for a, b in rows if a is not None and b is not None}


def open_events(session: Session) -> list[tuple[WalletEvent, Purchase]]:
    """Wallet events still looking for a Plaid transaction."""
    since = now() - timedelta(days=LOOKBACK_DAYS)
    rows = session.execute(
        select(WalletEvent, Purchase)
        .join(Purchase, Purchase.wallet_event_id == WalletEvent.id)
        .outerjoin(PlaidTransaction, PlaidTransaction.id == Purchase.plaid_transaction_id)
        .where(
            WalletEvent.status == "accepted",
            WalletEvent.event_at >= since,
            WalletEvent.amount_cents.is_not(None),
            Purchase.state == "active",
            (Purchase.plaid_transaction_id.is_(None)) | (PlaidTransaction.is_removed.is_(True)),
        )
        .options(joinedload(WalletEvent.card_alias))
    ).all()
    return [(e, p) for e, p in rows if e.card_id is not None]


def open_txns(session: Session, card_ids: set[int], since_date) -> list[PlaidTransaction]:
    """Live card charges not already linked to a wallet event."""
    linked = select(Purchase.plaid_transaction_id).where(
        Purchase.wallet_event_id.is_not(None), Purchase.plaid_transaction_id.is_not(None)
    )
    txns = session.scalars(
        select(PlaidTransaction)
        .join(Account, Account.id == PlaidTransaction.account_id)
        .where(
            Account.role == "credit_card",
            PlaidTransaction.is_removed.is_(False),
            PlaidTransaction.amount_cents > 0,
            PlaidTransaction.txn_date >= since_date,
            PlaidTransaction.id.not_in(linked),
        )
        .options(joinedload(PlaidTransaction.account), joinedload(PlaidTransaction.merchant))
    ).all()
    out = []
    for t in txns:
        card = card_for_account(session, t.account)
        if card is not None and card.id in card_ids:
            out.append(t)
    return out


def run_matching(session: Session) -> dict:
    """Match all open wallet events against all open Plaid transactions. Safe to run repeatedly."""
    advisory_xact_lock(session, LOCK_MATCHING, 0)
    session.flush()
    events = open_events(session)
    summary = {"linked_high": 0, "linked_medium": 0, "ambiguous": 0, "low": 0}
    if not events:
        return summary
    card_ids = {e.card_id for e, _ in events}
    min_day = min(local_date(e.event_at) for e, _ in events)
    txns = open_txns(session, card_ids, min_day)
    rejected = _rejected_pairs(session)
    txn_card = {t.id: card_for_account(session, t.account).id for t in txns}

    pairs: list[Candidate] = []
    for ev, _p in events:
        for t in txns:
            if txn_card[t.id] != ev.card_id or (ev.id, t.id) in rejected:
                continue
            c = score_pair(ev, t)
            if c is not None:
                pairs.append(c)
    pairs.sort(key=lambda c: (-c.score, c.event.id, c.txn.id))

    done_e: set[int] = set()
    done_t: set[int] = set()
    for c in pairs:
        if c.event.id in done_e or c.txn.id in done_t:
            continue
        rivals = [
            q
            for q in pairs
            if q is not c
            and (q.event.id == c.event.id or q.txn.id == c.txn.id)
            and q.event.id not in done_e
            and q.txn.id not in done_t
            and abs(q.score - c.score) <= TIE_EPSILON
        ]
        if rivals:
            _record_ambiguous(session, c, rivals)
            cluster = [c] + rivals
            done_e.update(q.event.id for q in cluster)
            done_t.update(q.txn.id for q in cluster)
            summary["ambiguous"] += 1
            continue
        done_e.add(c.event.id)
        done_t.add(c.txn.id)
        if c.confidence == "low":
            _record_low(session, c)
            summary["low"] += 1
            continue
        p = link(
            session,
            c.event,
            c.txn,
            confidence=c.confidence,
            score=c.score,
            reason=f"{'Auto-linked' if c.confidence == 'high' else 'Linked, needs review'}: {c.explain()}",
            details=c.as_dict(),
        )
        if c.confidence == "medium":
            review.open_item(
                session,
                "medium_match",
                f"Medium-confidence match: tapped {fmt(c.event.amount_cents)} at '{c.event.merchant_raw}' ↔ "
                f"Plaid {fmt(c.txn.amount_cents)} '{c.txn.display_name}'",
                wallet_event_id=c.event.id,
                plaid_transaction_id=c.txn.id,
                details={**c.as_dict(), "purchase_id": p.id, "explain": c.explain()},
            )
            summary["linked_medium"] += 1
        else:
            summary["linked_high"] += 1
    return summary


def _record_ambiguous(session: Session, c: Candidate, rivals: list[Candidate]) -> None:
    cluster = [c] + rivals
    for t_id in sorted({q.txn.id for q in cluster}):
        t_cands = [q for q in cluster if q.txn.id == t_id]
        t = t_cands[0].txn
        # The transaction must be counted while we wait for a decision.
        ensure_purchase_for_txn(session, t, "Ambiguous match: counted on its own until reviewed")
        events = {q.event.id for q in cluster}
        item, created = review.open_item(
            session,
            "ambiguous_match",
            f"Plaid {fmt(t.amount_cents)} '{t.display_name}' on {t.effective_date} matches {len(events)} Apple Pay "
            f"taps equally well. Until you choose, both are counted (possible double count).",
            plaid_transaction_id=t_id,
            details={"candidates": [q.as_dict() for q in cluster]},
        )
        if created:
            review.decide(
                session,
                "ambiguous",
                f"Not linked: {len(cluster)} candidate pairs within {TIE_EPSILON} of score {c.score:.3f}",
                plaid_transaction_id=t_id,
                score=c.score,
                details={"candidates": [q.as_dict() for q in cluster]},
            )


def _record_low(session: Session, c: Candidate) -> None:
    item, created = review.open_item(
        session,
        "low_confidence_match",
        f"Possible match (low confidence): tapped {fmt(c.event.amount_cents)} at '{c.event.merchant_raw}' ↔ "
        f"Plaid {fmt(c.txn.amount_cents)} '{c.txn.display_name}'. Not linked; both are counted until you decide.",
        wallet_event_id=c.event.id,
        plaid_transaction_id=c.txn.id,
        details={**c.as_dict(), "explain": c.explain()},
    )
    ensure_purchase_for_txn(session, c.txn, "Low-confidence match: counted on its own until reviewed")
    if created:
        review.decide(
            session,
            "low_confidence",
            f"Not linked (score below {MEDIUM}): {c.explain()}",
            wallet_event_id=c.event.id,
            plaid_transaction_id=c.txn.id,
            confidence="low",
            score=c.score,
            details=c.as_dict(),
        )


def candidates_for_event(session: Session, ev: WalletEvent) -> list[Candidate]:
    """For the review UI: plausible transactions for an event (ignores ties and prior links)."""
    if ev.card_id is None:
        return []
    out = []
    for t in open_txns(session, {ev.card_id}, local_date(ev.event_at)):
        c = score_pair(ev, t)
        if c is not None:
            out.append(c)
    return sorted(out, key=lambda c: -c.score)


def mark_missing_events(session: Session) -> int:
    """Apple Pay taps that never matched after N days go to review as 'possibly missing or refunded'."""
    from app.services.settings_store import get_int

    days = get_int(session, "missing_after_days")
    cutoff = now() - timedelta(days=days)
    rows = session.execute(
        select(WalletEvent, Purchase)
        .join(Purchase, Purchase.wallet_event_id == WalletEvent.id)
        .where(
            WalletEvent.status == "accepted",
            WalletEvent.event_at < cutoff,
            Purchase.state == "active",
            Purchase.plaid_transaction_id.is_(None),
        )
    ).all()
    from app.models import ReviewItem

    n = 0
    for ev, p in rows:
        seen = session.scalar(
            select(ReviewItem.id).where(ReviewItem.kind == "missing_after_5_days", ReviewItem.wallet_event_id == ev.id)
        )
        if seen:
            continue
        review.open_item(
            session,
            "missing_after_5_days",
            f"Apple Pay tap {fmt(ev.amount_cents)} at '{ev.merchant_raw}' on {local_date(ev.event_at)} hasn't shown up "
            f"in Plaid after {days} days. Possibly missing, declined or refunded. Still counted until you decide.",
            wallet_event_id=ev.id,
            details={"purchase_id": p.id, "days": days},
        )
        review.decide(
            session,
            "missing_after_5_days",
            f"No Plaid match {days} days after the tap; sent to review (amount still reserved)",
            wallet_event_id=ev.id,
        )
        n += 1
    return n


def reassign_alias(session: Session, alias: WalletCardAlias) -> None:
    """After a Wallet card name is (re)mapped, update its events' purchases and re-run matching."""
    from app.services.purchases import unlink

    events = session.scalars(select(WalletEvent).where(WalletEvent.card_alias_id == alias.id)).all()
    for ev in events:
        p = session.scalar(select(Purchase).where(Purchase.wallet_event_id == ev.id))
        if p is None:
            continue
        if p.plaid_transaction is not None:
            card = card_for_account(session, p.plaid_transaction.account)
            if card is None or card.id != alias.card_id:
                unlink(session, p, "Wallet card mapping changed", actor="user")
        p.card_id = alias.card_id
        if alias.card_id is not None:
            review.resolve_touching(
                session, wallet_event_ids=[ev.id], kinds=("unmapped_card",), resolution="auto_resolved",
                note=f"Wallet card '{alias.display_name}' mapped",
            )
    session.flush()
    run_matching(session)
