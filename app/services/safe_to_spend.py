"""Safe-to-spend computation and everything the dashboard shows.

safe_to_spend = verified_cash
              - card_spend_not_yet_covered
              - committed_bills_due_in_next_N_days
              - minimum_buffer
              - uncertainty_holdback

Every number is traceable: card spend is the sum of ledger entries, cash comes from a
specific balance snapshot, bills are listed one by one. It is an estimate, never a guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    BalanceSnapshot,
    Card,
    CardLiabilitySnapshot,
    LedgerEntry,
    PlaidItem,
    Purchase,
    ReserveTransfer,
    ReviewItem,
)
from app.services import bills as bills_svc
from app.services import ledger, review, transfers
from app.services.settings_store import get_int, tracking_start_date
from app.timeutil import add_months, humanize_age, local_date, now, today


@dataclass
class CardSummary:
    card: Card
    today_provisional: int = 0
    today_posted: int = 0
    cycle_provisional: int = 0
    cycle_posted: int = 0
    cycle_start: date | None = None
    cycle_start_is_estimate: bool = True
    statement_balance: int | None = None
    statement_remaining: int | None = None
    last_statement_date: date | None = None
    due_date: date | None = None
    minimum_payment: int | None = None
    next_close: date | None = None
    next_close_source: str | None = None  # manual | estimate | None
    liabilities_as_of: datetime | None = None
    reserve_covers: bool | None = None
    reserve_shortfall: int = 0
    uncovered_ledger: int = 0


@dataclass
class Dashboard:
    generated_at: datetime
    verified_cash: int | None
    verified_cash_snapshot: BalanceSnapshot | None
    verified_cash_age: str
    verified_cash_used_current: bool
    reserve_cash: int | None
    reserve_snapshot: BalanceSnapshot | None
    ledger_total: int
    card_spend_uncovered: int
    reserve_overfunded: int
    bills_window_days: int
    bills_due: list
    bills_total: int
    minimum_buffer: int
    holdback: int
    holdback_reasons: list[str]
    safe_to_spend: int | None
    suggested_transfer: int
    pending_transfers: list[ReserveTransfer]
    pending_transfer_total: int
    in_flight_to_reserve: int
    possible_double_count: int
    review_count: int
    cards: list[CardSummary]
    unmapped_today: int
    unmapped_cycle: int
    reauth_items: list[PlaidItem]
    items: list[PlaidItem]
    warnings: list[str] = field(default_factory=list)
    tracking_start: date | None = None


def latest_snapshot(session: Session, account_id: int) -> BalanceSnapshot | None:
    return transfers.latest_balance(session, account_id)


def latest_liability(session: Session, card_id: int) -> CardLiabilitySnapshot | None:
    return session.scalar(
        select(CardLiabilitySnapshot)
        .where(CardLiabilitySnapshot.card_id == card_id)
        .order_by(CardLiabilitySnapshot.fetched_at.desc(), CardLiabilitySnapshot.id.desc())
    )


def estimate_next_close(card: Card, liab: CardLiabilitySnapshot | None, ref: date) -> tuple[date | None, str | None]:
    if card.manual_next_statement_close_date and card.manual_next_statement_close_date >= ref:
        return card.manual_next_statement_close_date, "manual"
    if liab and liab.last_statement_issue_date:
        d, k = liab.last_statement_issue_date, 1
        while add_months(liab.last_statement_issue_date, k) <= ref and k < 36:
            k += 1
        d = add_months(liab.last_statement_issue_date, k)
        return d, "estimate"
    return None, None


def possible_double_count(session: Session) -> int:
    """Plaid transactions counted on their own while an ambiguous/low match is under review."""
    items = session.scalars(
        select(ReviewItem).where(ReviewItem.status == "open", ReviewItem.kind.in_(["ambiguous_match", "low_confidence_match"]))
    ).all()
    total = 0
    seen: set[int] = set()
    for it in items:
        if it.plaid_transaction_id in seen or it.plaid_transaction_id is None:
            continue
        seen.add(it.plaid_transaction_id)
        p = session.scalar(select(Purchase).where(Purchase.plaid_transaction_id == it.plaid_transaction_id, Purchase.wallet_event_id.is_(None)))
        if p is not None:
            total += max(0, ledger.purchase_sum(session, p.id))
    return total


def _purchase_day(p: Purchase) -> date | None:
    if p.wallet_event is not None:
        return local_date(p.wallet_event.event_at)
    if p.plaid_transaction is not None:
        return p.plaid_transaction.effective_date
    return None


def compute(session: Session) -> Dashboard:
    ref_now, ref_today = now(), today()
    warnings: list[str] = []
    holdback_reasons: list[str] = []

    checking = transfers.role_account(session, "primary_checking")
    reserve = transfers.role_account(session, "reserve")
    snap = latest_snapshot(session, checking.id) if checking else None
    used_current = False
    verified_cash = None
    if snap is not None:
        if snap.available_cents is not None:
            verified_cash = snap.available_cents
        elif snap.current_cents is not None:
            verified_cash, used_current = snap.current_cents, True
            warnings.append("Your bank didn't report an 'available' balance; using the 'current' balance instead.")
    stale_hours = get_int(session, "stale_threshold_hours")
    if checking is None:
        warnings.append("No account is marked as your primary checking account yet (Accounts page).")
    elif snap is None:
        warnings.append("No checking balance has been fetched yet.")
    if snap is not None and ref_now - snap.as_of > timedelta(hours=stale_hours):
        holdback_reasons.append(f"Checking balance is {humanize_age(snap.as_of)} old (limit {stale_hours}h)")
    reserve_snap = latest_snapshot(session, reserve.id) if reserve else None
    reserve_cash = None
    if reserve_snap is not None:
        reserve_cash = reserve_snap.available_cents if reserve_snap.available_cents is not None else reserve_snap.current_cents

    items = session.scalars(select(PlaidItem).where(PlaidItem.status != "removed").order_by(PlaidItem.id)).all()
    reauth = [i for i in items if i.status in ("needs_reauth", "expiring", "revoked", "error")]
    for i in reauth:
        holdback_reasons.append(f"{i.institution_name or 'A connection'} needs attention ({i.status})")
    card_stale = timedelta(hours=get_int(session, "card_stale_threshold_hours"))
    for i in items:
        if any(a.role == "credit_card" for a in i.accounts) and i.status == "active":
            if i.last_sync_completed_at is None or ref_now - i.last_sync_completed_at > card_stale:
                holdback_reasons.append(f"{i.institution_name or 'Card'} data last synced {humanize_age(i.last_sync_completed_at)}")

    total = ledger.total(session)
    uncovered = max(0, total)
    overfunded = max(0, -total)
    pending = session.scalars(select(ReserveTransfer).where(ReserveTransfer.status.in_(["pending", "left_checking"])).order_by(ReserveTransfer.id)).all()
    pending_total = transfers.pending_total(session)
    suggested = max(0, uncovered - pending_total)

    window = get_int(session, "bills_window_days")
    due = bills_svc.due_within(session, ref_today, window)
    bills_total = sum(o.amount_cents for o in due)
    buffer = get_int(session, "minimum_buffer_cents")
    holdback = get_int(session, "uncertainty_holdback_cents") if holdback_reasons else 0
    if holdback_reasons:
        warnings.append("Data may be out of date, so an extra cushion is being held back.")

    safe = None
    if verified_cash is not None:
        safe = verified_cash - uncovered - bills_total - buffer - holdback

    # Per-card spend (from ledger sums per purchase, so it ties to the ledger)
    cards = session.scalars(select(Card).order_by(Card.id)).all()
    summaries = {c.id: CardSummary(card=c) for c in cards}
    for c in cards:
        s = summaries[c.id]
        liab = latest_liability(session, c.id)
        if liab is not None:
            s.statement_balance = liab.last_statement_balance_cents
            s.last_statement_date = liab.last_statement_issue_date
            s.due_date = liab.next_payment_due_date
            s.minimum_payment = liab.minimum_payment_amount_cents
            s.liabilities_as_of = liab.fetched_at
            rem = liab.last_statement_balance_cents
            if rem is not None and liab.last_payment_amount_cents and liab.last_payment_date and liab.last_statement_issue_date \
                    and liab.last_payment_date >= liab.last_statement_issue_date:
                rem = rem - liab.last_payment_amount_cents
            s.statement_remaining = max(0, rem) if rem is not None else None
            if liab.last_statement_issue_date:
                s.cycle_start, s.cycle_start_is_estimate = liab.last_statement_issue_date + timedelta(days=1), False
        if s.cycle_start is None:
            s.cycle_start = ref_today.replace(day=1)
        s.next_close, s.next_close_source = estimate_next_close(c, liab, ref_today)

    unmapped_today = unmapped_cycle = 0
    sums = dict(
        session.execute(
            select(LedgerEntry.purchase_id, func.sum(LedgerEntry.amount_cents)).where(LedgerEntry.purchase_id.is_not(None)).group_by(LedgerEntry.purchase_id)
        ).all()
    )
    for p in session.scalars(select(Purchase).where(Purchase.id.in_(list(sums.keys())))) if sums else []:
        amt = int(sums.get(p.id) or 0)
        if amt == 0:
            continue
        day = _purchase_day(p)
        provisional = p.plaid_transaction is None or p.plaid_transaction.pending
        if p.card_id is None:
            unmapped_cycle += amt
            if day == ref_today:
                unmapped_today += amt
            continue
        s = summaries.get(p.card_id)
        if s is None:
            continue
        s.uncovered_ledger += amt
        if day is not None and day >= s.cycle_start:
            if provisional:
                s.cycle_provisional += amt
            else:
                s.cycle_posted += amt
        if day == ref_today:
            if provisional:
                s.today_provisional += amt
            else:
                s.today_posted += amt

    # Does the reserve cover upcoming statement balances, in due-date order?
    cumulative = 0
    for s in sorted(summaries.values(), key=lambda x: (x.due_date or date.max, x.card.id)):
        if s.statement_remaining is None:
            continue
        cumulative += s.statement_remaining
        if reserve_cash is not None:
            s.reserve_covers = reserve_cash >= cumulative
            s.reserve_shortfall = max(0, cumulative - reserve_cash)

    dbl = possible_double_count(session)
    if dbl:
        warnings.append("Some charges may be counted twice until you review them (that makes safe-to-spend lower, not higher).")

    return Dashboard(
        generated_at=ref_now,
        verified_cash=verified_cash,
        verified_cash_snapshot=snap,
        verified_cash_age=humanize_age(snap.as_of) if snap else "never",
        verified_cash_used_current=used_current,
        reserve_cash=reserve_cash,
        reserve_snapshot=reserve_snap,
        ledger_total=total,
        card_spend_uncovered=uncovered,
        reserve_overfunded=overfunded,
        bills_window_days=window,
        bills_due=due,
        bills_total=bills_total,
        minimum_buffer=buffer,
        holdback=holdback,
        holdback_reasons=holdback_reasons,
        safe_to_spend=safe,
        suggested_transfer=suggested,
        pending_transfers=list(pending),
        pending_transfer_total=pending_total,
        in_flight_to_reserve=transfers.in_flight_to_reserve(session),
        possible_double_count=dbl,
        review_count=review.open_count(session),
        cards=list(summaries.values()),
        unmapped_today=unmapped_today,
        unmapped_cycle=unmapped_cycle,
        reauth_items=reauth,
        items=list(items),
        warnings=warnings,
        tracking_start=tracking_start_date(session),
    )
