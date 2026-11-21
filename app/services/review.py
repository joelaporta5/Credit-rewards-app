"""Review-queue bookkeeping (creating and resolving items). User actions live in review_actions.py."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import MatchDecision, ReviewItem
from app.timeutil import now


def open_item(
    session: Session,
    kind: str,
    summary: str,
    *,
    wallet_event_id: int | None = None,
    plaid_transaction_id: int | None = None,
    details: dict | None = None,
) -> tuple[ReviewItem, bool]:
    """Create an open review item unless an identical open one exists. Returns (item, created)."""
    q = select(ReviewItem).where(ReviewItem.kind == kind, ReviewItem.status == "open")
    q = q.where(ReviewItem.wallet_event_id.is_(None) if wallet_event_id is None else ReviewItem.wallet_event_id == wallet_event_id)
    q = q.where(
        ReviewItem.plaid_transaction_id.is_(None) if plaid_transaction_id is None else ReviewItem.plaid_transaction_id == plaid_transaction_id
    )
    existing = session.scalar(q)
    if existing:
        if details is not None:
            existing.details = details
            existing.summary = summary
        return existing, False
    item = ReviewItem(
        kind=kind,
        summary=summary,
        wallet_event_id=wallet_event_id,
        plaid_transaction_id=plaid_transaction_id,
        details=details,
        created_at=now(),
    )
    session.add(item)
    session.flush()
    return item, True


def resolve(session: Session, item: ReviewItem, resolution: str, note: str | None = None) -> None:
    if item.status == "resolved":
        return
    item.status = "resolved"
    item.resolution = resolution
    item.resolution_note = note
    item.resolved_at = now()


def resolve_touching(
    session: Session,
    *,
    wallet_event_ids: list[int] = (),
    txn_ids: list[int] = (),
    kinds: tuple[str, ...],
    resolution: str,
    note: str,
    except_id: int | None = None,
) -> int:
    conds = []
    if wallet_event_ids:
        conds.append(ReviewItem.wallet_event_id.in_(list(wallet_event_ids)))
    if txn_ids:
        conds.append(ReviewItem.plaid_transaction_id.in_(list(txn_ids)))
    if not conds:
        return 0
    items = session.scalars(
        select(ReviewItem).where(ReviewItem.status == "open", ReviewItem.kind.in_(kinds), or_(*conds))
    ).all()
    n = 0
    for it in items:
        if except_id is not None and it.id == except_id:
            continue
        # ambiguous items list several candidate events in details; also match on those
        resolve(session, it, resolution, note)
        n += 1
    return n


def resolve_ambiguous_mentioning(session: Session, wallet_event_id: int, note: str) -> None:
    """Ambiguous items are keyed by transaction but list candidate events in details."""
    items = session.scalars(select(ReviewItem).where(ReviewItem.status == "open", ReviewItem.kind == "ambiguous_match")).all()
    for it in items:
        cands = [c.get("wallet_event_id") for c in (it.details or {}).get("candidates", [])]
        if wallet_event_id in cands:
            resolve(session, it, "auto_resolved", note)


def decide(
    session: Session,
    decision: str,
    reason: str,
    *,
    wallet_event_id: int | None = None,
    plaid_transaction_id: int | None = None,
    confidence: str | None = None,
    score: float | None = None,
    details: dict | None = None,
    actor: str = "system",
) -> MatchDecision:
    d = MatchDecision(
        at=now(),
        decision=decision,
        reason=reason,
        wallet_event_id=wallet_event_id,
        plaid_transaction_id=plaid_transaction_id,
        confidence=confidence,
        score=round(score, 4) if score is not None else None,
        details=details,
        actor=actor,
    )
    session.add(d)
    session.flush()
    return d


def open_count(session: Session) -> int:
    from sqlalchemy import func

    return int(session.scalar(select(func.count()).select_from(ReviewItem).where(ReviewItem.status == "open")) or 0)
