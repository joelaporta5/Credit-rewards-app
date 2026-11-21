"""Reserve transfers that *I* make in my banking app. This app never moves money.

Flow:
1. I click "I moved $X". A pending transfer is recorded. Nothing changes in the
   ledger yet: the money is still in verified checking cash and still counted as
   uncovered card spend, so safe-to-spend is unchanged (no double count). The
   suggested transfer drops by $X so I'm not told to move it twice.
2. When Plaid shows the matching outflow on checking AND the stored checking balance
   was fetched after Plaid first showed that outflow, the transfer "left checking":
   one ledger entry of -$X reduces uncovered card spend, exactly when verified cash
   drops by $X.
3. When both sides are posted (outflow on checking, inflow on reserve) it's confirmed.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, AuditLog, BalanceSnapshot, PlaidTransaction, ReserveTransfer
from app.money import fmt
from app.services import ledger
from app.timeutil import local_date, now

LOOKBACK_DAYS = 3


def role_account(session: Session, role: str) -> Account | None:
    return session.scalar(select(Account).where(Account.role == role, Account.is_active.is_(True)))


def latest_balance(session: Session, account_id: int) -> BalanceSnapshot | None:
    return session.scalar(
        select(BalanceSnapshot).where(BalanceSnapshot.account_id == account_id).order_by(BalanceSnapshot.fetched_at.desc(), BalanceSnapshot.id.desc())
    )


def create(session: Session, amount_cents: int, note: str | None = None) -> ReserveTransfer:
    if amount_cents <= 0:
        raise ValueError("Transfer amount must be positive")
    tr = ReserveTransfer(amount_cents=amount_cents, note=note, created_at=now(), status="pending")
    session.add(tr)
    session.flush()
    session.add(AuditLog(action="reserve_transfer_marked", details={"transfer_id": tr.id, "amount_cents": amount_cents}))
    reconcile_transfers(session)
    return tr


def cancel(session: Session, tr: ReserveTransfer) -> None:
    tr.status = "cancelled"
    tr.cancelled_at = now()
    ledger.sync_transfer(session, tr, 0, "Transfer cancelled by me")
    session.add(AuditLog(action="reserve_transfer_cancelled", details={"transfer_id": tr.id}))


def confirm_manually(session: Session, tr: ReserveTransfer) -> None:
    tr.manually_confirmed = True
    session.add(AuditLog(action="reserve_transfer_confirmed_manually", details={"transfer_id": tr.id}))
    reconcile_transfers(session)


def _used_ids(session: Session, col) -> set[int]:
    return {i for i in session.scalars(select(col).where(col.is_not(None), ReserveTransfer.status != "cancelled"))}


def _find(session: Session, account: Account | None, amount: int, tr: ReserveTransfer, used: set[int]) -> PlaidTransaction | None:
    if account is None:
        return None
    since = local_date(tr.created_at) - timedelta(days=LOOKBACK_DAYS)
    rows = session.scalars(
        select(PlaidTransaction)
        .where(
            PlaidTransaction.account_id == account.id,
            PlaidTransaction.amount_cents == amount,
            PlaidTransaction.is_removed.is_(False),
            PlaidTransaction.txn_date >= since,
        )
        .order_by(PlaidTransaction.txn_date, PlaidTransaction.id)
    ).all()
    for r in rows:
        if r.id not in used:
            return r
    return None


def _live(t: PlaidTransaction | None) -> bool:
    return t is not None and not t.is_removed


def reconcile_transfers(session: Session) -> None:
    checking = role_account(session, "primary_checking")
    reserve = role_account(session, "reserve")
    active = session.scalars(
        select(ReserveTransfer).where(ReserveTransfer.status.in_(["pending", "left_checking", "confirmed"])).order_by(ReserveTransfer.id)
    ).all()
    for tr in active:
        if not _live(tr.checking_txn):
            used = _used_ids(session, ReserveTransfer.checking_txn_id) - ({tr.checking_txn_id} if tr.checking_txn_id else set())
            found = _find(session, checking, tr.amount_cents, tr, used)  # money out of checking = positive
            tr.checking_txn_id = found.id if found else None
        if not _live(tr.reserve_txn):
            used = _used_ids(session, ReserveTransfer.reserve_txn_id) - ({tr.reserve_txn_id} if tr.reserve_txn_id else set())
            found = _find(session, reserve, -tr.amount_cents, tr, used)  # money into reserve = negative
            tr.reserve_txn_id = found.id if found else None
        session.flush()
        session.refresh(tr, ["checking_txn", "reserve_txn"])

        left = tr.manually_confirmed
        why = "Marked confirmed by me"
        if not left and _live(tr.checking_txn) and checking is not None:
            bal = latest_balance(session, checking.id)
            # Only count it as gone once the balance we use for verified cash reflects it.
            if bal is not None and bal.fetched_at >= tr.checking_txn.first_seen_at:
                left = True
                why = f"Plaid shows {fmt(tr.amount_cents)} leaving checking and the checking balance reflects it"
        target = -tr.amount_cents if left else 0
        if not left:
            why = "Checking-side transfer no longer visible in Plaid" if ledger.transfer_sum(session, tr.id) else "Waiting for Plaid"
        ledger.sync_transfer(session, tr, target, why)

        both_posted = _live(tr.checking_txn) and not tr.checking_txn.pending and _live(tr.reserve_txn) and not tr.reserve_txn.pending
        if tr.manually_confirmed or both_posted:
            if tr.status != "confirmed":
                tr.status, tr.confirmed_at = "confirmed", now()
        elif left:
            tr.status = "left_checking"
            tr.left_checking_at = tr.left_checking_at or now()
            tr.confirmed_at = None
        else:
            tr.status = "pending"
            tr.left_checking_at = None
            tr.confirmed_at = None
    session.flush()


def pending_total(session: Session) -> int:
    """Transfers I've marked that haven't left checking yet (still counted as uncovered)."""
    rows = session.scalars(select(ReserveTransfer).where(ReserveTransfer.status == "pending")).all()
    return sum(r.amount_cents for r in rows)


def in_flight_to_reserve(session: Session) -> int:
    """Left checking but not yet posted into the reserve account."""
    rows = session.scalars(select(ReserveTransfer).where(ReserveTransfer.status == "left_checking")).all()
    return sum(r.amount_cents for r in rows if not (_live(r.reserve_txn) and not r.reserve_txn.pending))
