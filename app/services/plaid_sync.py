"""Plaid data pipeline: connect, /transactions/sync, balances, liabilities, item status.

Key behaviours (verified against Plaid docs):
- /transactions/sync with a stored cursor per Item. We pull *all* pages first; if Plaid
  answers TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION we restart from the original
  cursor. Changes and the new cursor are saved in one DB transaction.
- Pending -> posted: Plaid removes the pending transaction_id and adds a posted one whose
  pending_transaction_id points at it ("not guaranteed to be in the same page, but should
  happen within the same overall update"). We update the existing row in place, so the
  purchase keeps its identity and is never double counted. If the removal arrives in an
  earlier update, the row is revived when the posted version arrives.
- Balances come from /accounts/balance/get (real-time) and every result is stored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import decrypt_token, encrypt_token
from app.db import LOCK_ITEM_SYNC, SessionLocal, advisory_unlock, try_advisory_lock
from app.models import (
    Account,
    AuditLog,
    BalanceSnapshot,
    Card,
    CardLiabilitySnapshot,
    PlaidItem,
    PlaidTransaction,
    PlaidTransactionHistory,
    SyncRun,
)
from app.money import plaid_amount_to_cents
from app.services.merchants import get_or_create_merchant
from app.services.plaid_gateway import REAUTH_ERROR_CODES, Gateway, PlaidError, get_gateway
from app.timeutil import now, parse_iso_date, parse_iso_datetime

log = logging.getLogger(__name__)

LIABILITIES_MAX_AGE = timedelta(hours=12)


# ---------------------------------------------------------------------------
# Connecting
# ---------------------------------------------------------------------------


def find_duplicate_institution(session: Session, institution_id: str | None) -> PlaidItem | None:
    if not institution_id:
        return None
    return session.scalar(
        select(PlaidItem).where(
            PlaidItem.institution_id == institution_id,
            PlaidItem.status != "removed",
            PlaidItem.plaid_env == get_settings().plaid_env,
        )
    )


def connect_item(session: Session, gw: Gateway, public_token: str, institution_id: str | None, institution_name: str | None) -> PlaidItem:
    """Exchange a Link public_token and store the encrypted access token."""
    ex = gw.public_token_exchange(public_token)
    access_token, plaid_item_id = ex["access_token"], ex["item_id"]
    item = PlaidItem(
        plaid_item_id=plaid_item_id,
        plaid_env=get_settings().plaid_env,
        institution_id=institution_id,
        institution_name=institution_name,
        access_token_encrypted=encrypt_token(access_token),
        status="active",
        created_at=now(),
    )
    session.add(item)
    session.flush()
    accts = gw.accounts_get(access_token)
    upsert_accounts(session, item, accts.get("accounts", []))
    if not item.institution_name:
        item.institution_name = (accts.get("item") or {}).get("institution_name")
    session.add(AuditLog(action="plaid_item_connected", details={"item_id": item.id, "institution": item.institution_name}))
    session.flush()
    return item


def upsert_accounts(session: Session, item: PlaidItem, accounts: list[dict]) -> list[Account]:
    out = []
    for a in accounts:
        acct = session.scalar(select(Account).where(Account.plaid_account_id == a["account_id"]))
        if acct is None:
            acct = Account(
                item_id=item.id,
                plaid_account_id=a["account_id"],
                name=a.get("name") or "Account",
                type=str(a.get("type") or "other"),
                created_at=now(),
            )
            acct.role = "credit_card" if acct.type == "credit" else "ignored"
            session.add(acct)
            session.flush()
        acct.name = a.get("name") or acct.name
        acct.official_name = a.get("official_name")
        acct.mask = a.get("mask")
        acct.subtype = str(a.get("subtype")) if a.get("subtype") else None
        acct.is_active = True
        if acct.role == "credit_card" and session.scalar(select(Card).where(Card.account_id == acct.id)) is None:
            session.add(Card(account_id=acct.id, issuer=item.institution_name, created_at=now()))
            session.flush()
        out.append(acct)
    return out


# ---------------------------------------------------------------------------
# Applying /transactions/sync changes (pure DB; unit-tested with fake data)
# ---------------------------------------------------------------------------


def _history(session: Session, row: PlaidTransaction, change: str, run: SyncRun | None) -> None:
    session.add(
        PlaidTransactionHistory(
            plaid_transaction_row_id=row.id,
            at=now(),
            change=change,
            plaid_transaction_id=row.plaid_transaction_id,
            pending=row.pending,
            amount_cents=row.amount_cents,
            sync_run_id=run.id if run else None,
        )
    )


def _fill(session: Session, row: PlaidTransaction, d: dict) -> None:
    pfc = d.get("personal_finance_category") or {}
    row.pending = bool(d.get("pending"))
    row.amount_cents = plaid_amount_to_cents(d.get("amount")) or 0
    row.iso_currency_code = d.get("iso_currency_code") or d.get("unofficial_currency_code")
    row.txn_date = parse_iso_date(d.get("date")) or now().date()
    row.authorized_date = parse_iso_date(d.get("authorized_date"))
    row.authorized_datetime = parse_iso_datetime(d.get("authorized_datetime"))
    row.posted_datetime = parse_iso_datetime(d.get("datetime"))
    row.name = (d.get("name") or None) and str(d.get("name"))[:512]
    row.merchant_name = (d.get("merchant_name") or None) and str(d.get("merchant_name"))[:512]
    row.merchant_entity_id = d.get("merchant_entity_id")
    row.merchant_category_code = d.get("merchant_category_code")
    row.pfc_primary = pfc.get("primary")
    row.pfc_detailed = pfc.get("detailed")
    row.payment_channel = d.get("payment_channel")
    row.raw = d
    row.updated_at = now()
    m = get_or_create_merchant(session, row.merchant_name or row.name, row.merchant_entity_id)
    row.merchant_id = m.id if m else None


def _account_for(session: Session, plaid_account_id: str) -> Account | None:
    return session.scalar(select(Account).where(Account.plaid_account_id == plaid_account_id))


def upsert_transaction(session: Session, d: dict, run: SyncRun | None, from_modified: bool = False) -> PlaidTransaction | None:
    acct = _account_for(session, d["account_id"])
    if acct is None:
        log.warning("transaction for unknown account %s skipped", d.get("account_id"))
        return None
    tid = d["transaction_id"]
    row = session.scalar(select(PlaidTransaction).where(PlaidTransaction.plaid_transaction_id == tid))
    change = "modified" if row is not None else "added"
    if row is None and d.get("pending_transaction_id"):
        pending_row = session.scalar(
            select(PlaidTransaction).where(PlaidTransaction.plaid_transaction_id == d["pending_transaction_id"])
        )
        if pending_row is not None:
            row = pending_row
            change = "revived_by_posted" if pending_row.is_removed else "posted_replacing_pending"
            row.replaced_pending_transaction_id = pending_row.plaid_transaction_id
            row.plaid_transaction_id = tid
            row.is_removed = False
            row.removed_at = None
    if row is None:
        row = PlaidTransaction(account_id=acct.id, plaid_transaction_id=tid, first_seen_at=now(), pending=False, amount_cents=0)
        session.add(row)
    row.account_id = acct.id
    _fill(session, row, d)
    session.flush()
    _history(session, row, change, run)
    return row


def remove_transaction(session: Session, plaid_txn_id: str, run: SyncRun | None) -> PlaidTransaction | None:
    row = session.scalar(select(PlaidTransaction).where(PlaidTransaction.plaid_transaction_id == plaid_txn_id))
    if row is not None:
        if not row.is_removed:
            row.is_removed = True
            row.removed_at = now()
            row.updated_at = now()
            session.flush()
            _history(session, row, "removed", run)
        return row
    superseded = session.scalar(
        select(PlaidTransaction).where(PlaidTransaction.replaced_pending_transaction_id == plaid_txn_id)
    )
    if superseded is not None:
        # The pending version was already replaced in place by its posted version.
        session.add(
            PlaidTransactionHistory(
                plaid_transaction_row_id=superseded.id, at=now(), change="removal_ignored_superseded",
                plaid_transaction_id=plaid_txn_id, sync_run_id=run.id if run else None,
            )
        )
    return None


def apply_changes(session: Session, added: list[dict], modified: list[dict], removed: list[dict], run: SyncRun | None = None) -> list[PlaidTransaction]:
    """Apply one full sync update, then update purchases, matching and the ledger."""
    touched: list[PlaidTransaction] = []
    for d in added:
        r = upsert_transaction(session, d, run)
        if r is not None:
            touched.append(r)
    for d in modified:
        r = upsert_transaction(session, d, run, from_modified=True)
        if r is not None:
            touched.append(r)
    for d in removed:
        r = remove_transaction(session, d["transaction_id"], run)
        if r is not None:
            touched.append(r)
    post_process(session, touched)
    return touched


def post_process(session: Session, touched: list[PlaidTransaction]) -> None:
    from app.services.matching import run_matching
    from app.services.purchases import ensure_purchase_for_txn

    session.flush()
    run_matching(session)
    seen: set[int] = set()
    for t in touched:
        if t.id in seen:
            continue
        seen.add(t.id)
        if t.account.role == "credit_card":
            ensure_purchase_for_txn(session, t, "Plaid update")
    session.flush()


# ---------------------------------------------------------------------------
# Balances and liabilities
# ---------------------------------------------------------------------------


def store_balances(session: Session, accounts: list[dict], source: str) -> list[BalanceSnapshot]:
    out = []
    for a in accounts:
        acct = _account_for(session, a["account_id"])
        if acct is None:
            continue
        b = a.get("balances") or {}
        fetched = now()
        snap = BalanceSnapshot(
            account_id=acct.id,
            fetched_at=fetched,
            as_of=parse_iso_datetime(b.get("last_updated_datetime")) or fetched,
            available_cents=plaid_amount_to_cents(b.get("available")),
            current_cents=plaid_amount_to_cents(b.get("current")),
            limit_cents=plaid_amount_to_cents(b.get("limit")),
            iso_currency_code=b.get("iso_currency_code") or b.get("unofficial_currency_code"),
            source=source,
        )
        session.add(snap)
        out.append(snap)
    session.flush()
    return out


def store_liabilities(session: Session, resp: dict) -> int:
    credit = ((resp.get("liabilities") or {}).get("credit")) or []
    n = 0
    for c in credit:
        if not c.get("account_id"):
            continue
        acct = _account_for(session, c["account_id"])
        if acct is None:
            continue
        card = session.scalar(select(Card).where(Card.account_id == acct.id))
        if card is None:
            continue
        snap = CardLiabilitySnapshot(
            card_id=card.id,
            fetched_at=now(),
            last_statement_balance_cents=plaid_amount_to_cents(c.get("last_statement_balance")),
            last_statement_issue_date=parse_iso_date(c.get("last_statement_issue_date")),
            next_payment_due_date=parse_iso_date(c.get("next_payment_due_date")),
            minimum_payment_amount_cents=plaid_amount_to_cents(c.get("minimum_payment_amount")),
            last_payment_amount_cents=plaid_amount_to_cents(c.get("last_payment_amount")),
            last_payment_date=parse_iso_date(c.get("last_payment_date")),
            is_overdue=c.get("is_overdue"),
        )
        session.add(snap)
        n += 1
    session.flush()
    return n


# ---------------------------------------------------------------------------
# Item status
# ---------------------------------------------------------------------------


def set_item_status(session: Session, item: PlaidItem, status: str, code: str | None = None, message: str | None = None) -> None:
    if item.status != status or item.error_code != code:
        session.add(AuditLog(actor="system", action="item_status", details={"item_id": item.id, "from": item.status, "to": status, "code": code}))
        item.status_changed_at = now()
    item.status = status
    item.error_code = code
    item.error_message = message


def handle_plaid_error(session: Session, item: PlaidItem, err: PlaidError) -> None:
    if err.error_code in REAUTH_ERROR_CODES:
        set_item_status(session, item, "needs_reauth", err.error_code, err.display_message or err.message)
    item.last_sync_error = f"{err.error_code}: {err.message}"[:2000]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class SyncResult:
    item_id: int
    status: str = "ok"  # ok | busy | error | skipped
    added: int = 0
    modified: int = 0
    removed: int = 0
    messages: list[str] = field(default_factory=list)


def _item_token(item: PlaidItem) -> str:
    return decrypt_token(item.access_token_encrypted)


def pull_all_pages(gw: Gateway, access_token: str, cursor: str | None) -> tuple[list, list, list, str | None, dict]:
    """Fetch every page from `cursor`. Restart from the original cursor if Plaid reports a
    mutation during pagination (per Plaid docs)."""
    for attempt in range(4):
        added, modified, removed = [], [], []
        cur = cursor
        last: dict = {}
        try:
            while True:
                resp = gw.transactions_sync(access_token, cur)
                added += resp.get("added", [])
                modified += resp.get("modified", [])
                removed += resp.get("removed", [])
                cur = resp.get("next_cursor") or cur
                last = resp
                if not resp.get("has_more"):
                    return added, modified, removed, cur, last
        except PlaidError as e:
            if e.error_code == "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION" and attempt < 3:
                continue
            raise
    raise RuntimeError("unreachable")


def sync_item(item_id: int, trigger: str = "manual", *, force_balance: bool = False, force_liabilities: bool = False,
              gw: Gateway | None = None) -> SyncResult:
    """Sync one connection. Only one sync per connection runs at a time; a request that
    arrives during a sync makes the running sync go around once more."""
    gw = gw or get_gateway()
    lock_session = SessionLocal()
    try:
        if not try_advisory_lock(lock_session, LOCK_ITEM_SYNC, item_id):
            with SessionLocal() as s:
                it = s.get(PlaidItem, item_id)
                if it is not None:
                    it.sync_requested = True
                    s.commit()
            return SyncResult(item_id, status="busy", messages=["a sync is already running; it will run again"])
        result = SyncResult(item_id)
        for _ in range(5):
            _sync_once(item_id, trigger, force_balance, force_liabilities, gw, result)
            with SessionLocal() as s:
                it = s.get(PlaidItem, item_id)
                if it is None or not it.sync_requested:
                    break
                it.sync_requested = False
                s.commit()
        return result
    finally:
        try:
            advisory_unlock(lock_session, LOCK_ITEM_SYNC, item_id)
            lock_session.commit()
        finally:
            lock_session.close()


def _sync_once(item_id: int, trigger: str, force_balance: bool, force_liabilities: bool, gw: Gateway, result: SyncResult) -> None:
    from app.services.transfers import reconcile_transfers

    with SessionLocal() as s:
        item = s.get(PlaidItem, item_id)
        if item is None or item.status in ("removed", "revoked"):
            result.status = "skipped"
            return
        token = _item_token(item)
        cursor = item.transactions_cursor
        item.sync_requested = False
        item.last_sync_started_at = now()
        run = SyncRun(item_id=item.id, trigger=trigger, started_at=now())
        s.add(run)
        s.commit()
        run_id = run.id

    def fail(err: Exception, stage: str) -> None:
        with SessionLocal() as s2:
            it = s2.get(PlaidItem, item_id)
            r = s2.get(SyncRun, run_id)
            if isinstance(err, PlaidError):
                handle_plaid_error(s2, it, err)
                msg = f"{stage}: {err.error_code}"
            else:
                it.last_sync_error = f"{stage}: {type(err).__name__}"
                msg = f"{stage}: {type(err).__name__}: {err}"
            r.status, r.finished_at, r.message = "error", now(), msg[:2000]
            s2.commit()
        result.status = "error"
        result.messages.append(msg)
        log.warning("sync item %s failed at %s: %s", item_id, stage, msg)

    # 1) transactions
    try:
        added, modified, removed, next_cursor, last = pull_all_pages(gw, token, cursor)
    except Exception as e:  # noqa: BLE001
        fail(e, "transactions_sync")
        return
    with SessionLocal() as s:
        item = s.get(PlaidItem, item_id)
        run = s.get(SyncRun, run_id)
        if last.get("accounts"):
            upsert_accounts(s, item, last["accounts"])
        missing = {d["account_id"] for d in added + modified} - {a.plaid_account_id for a in s.scalars(select(Account))}
        if missing:
            upsert_accounts(s, item, gw.accounts_get(token).get("accounts", []))
        apply_changes(s, added, modified, removed, run)
        item.transactions_cursor = next_cursor
        item.transactions_update_status = last.get("transactions_update_status")
        run.added, run.modified, run.removed = len(added), len(modified), len(removed)
        s.commit()
    result.added += len(added)
    result.modified += len(modified)
    result.removed += len(removed)

    # 2) balances (real time) for any item with accounts we use
    with SessionLocal() as s:
        item = s.get(PlaidItem, item_id)
        used = [a for a in item.accounts if a.role in ("primary_checking", "reserve", "credit_card")]
        min_gap = timedelta(minutes=get_settings().balance_min_interval_minutes)
        due = force_balance or item.balances_fetched_at is None or now() - item.balances_fetched_at >= min_gap
    if used and due:
        try:
            resp = gw.accounts_balance_get(token)
            with SessionLocal() as s:
                item = s.get(PlaidItem, item_id)
                upsert_accounts(s, item, resp.get("accounts", []))
                store_balances(s, resp.get("accounts", []), "accounts_balance_get")
                item.balances_fetched_at = now()
                reconcile_transfers(s)
                s.commit()
        except Exception as e:  # noqa: BLE001
            fail(e, "accounts_balance_get")
            return

    # 3) liabilities for credit cards
    with SessionLocal() as s:
        item = s.get(PlaidItem, item_id)
        has_cards = any(a.role == "credit_card" for a in item.accounts)
        liab_due = force_liabilities or item.liabilities_fetched_at is None or now() - item.liabilities_fetched_at >= LIABILITIES_MAX_AGE
    if has_cards and liab_due:
        try:
            resp = gw.liabilities_get(token)
            with SessionLocal() as s:
                store_liabilities(s, resp)
                s.get(PlaidItem, item_id).liabilities_fetched_at = now()
                s.commit()
        except PlaidError as e:
            if e.error_code in ("PRODUCTS_NOT_SUPPORTED", "NO_LIABILITY_ACCOUNTS", "PRODUCT_NOT_ENABLED", "INVALID_PRODUCT"):
                result.messages.append(f"liabilities unavailable for this institution ({e.error_code})")
            else:
                fail(e, "liabilities_get")
                return

    # 4) item status (Plaid's last successful contact with the bank, consent expiry)
    try:
        info = gw.item_get(token)
        with SessionLocal() as s:
            item = s.get(PlaidItem, item_id)
            st = ((info.get("status") or {}).get("transactions") or {})
            item.institution_last_successful_update = parse_iso_datetime(st.get("last_successful_update"))
            it = info.get("item") or {}
            item.consent_expiration_time = parse_iso_datetime(it.get("consent_expiration_time"))
            err = it.get("error")
            if err and err.get("error_code") in REAUTH_ERROR_CODES:
                set_item_status(s, item, "needs_reauth", err.get("error_code"), err.get("display_message") or err.get("error_message"))
            elif not err and item.status in ("needs_reauth", "error"):
                set_item_status(s, item, "active")
            s.commit()
    except PlaidError as e:
        result.messages.append(f"item_get failed: {e.error_code}")

    with SessionLocal() as s:
        item = s.get(PlaidItem, item_id)
        run = s.get(SyncRun, run_id)
        item.last_sync_completed_at = now()
        item.last_sync_error = None
        run.status, run.finished_at = "ok", now()
        s.commit()


def refresh_item(item_id: int, gw: Gateway | None = None) -> tuple[SyncResult, str]:
    """'Refresh now': ask Plaid to contact the bank (if enabled on the plan), then sync."""
    gw = gw or get_gateway()
    with SessionLocal() as s:
        item = s.get(PlaidItem, item_id)
        token = _item_token(item)
    note = "Asked Plaid to check the bank now (/transactions/refresh). New transactions arrive by webhook within minutes."
    try:
        gw.transactions_refresh(token)
    except PlaidError as e:
        if e.error_code in ("PRODUCTS_NOT_SUPPORTED", "PRODUCT_NOT_ENABLED", "INVALID_PRODUCT", "ADDITION_LIMIT", "INVALID_API_KEYS") or (e.status == 400 and "refresh" in (e.message or "").lower()):
            note = (f"Plaid's on-demand refresh isn't available on this plan ({e.error_code}); ran a normal sync instead. "
                    "Transactions are only as fresh as Plaid's last scheduled check of the bank.")
        else:
            note = f"Refresh request failed ({e.error_code}); ran a normal sync instead."
    res = sync_item(item_id, "refresh", force_balance=True, force_liabilities=True, gw=gw)
    return res, note


def sync_all(trigger: str = "schedule", gw: Gateway | None = None) -> list[SyncResult]:
    with SessionLocal() as s:
        # Only connections made in the current Plaid environment (Sandbox items can't be synced with Production keys).
        ids = [i for i in s.scalars(select(PlaidItem.id).where(PlaidItem.status.not_in(["removed", "revoked"]),
                                                              PlaidItem.plaid_env == get_settings().plaid_env))]
    return [sync_item(i, trigger, gw=gw) for i in ids]


def update_all_webhooks(gw: Gateway | None = None) -> list[str]:
    """Point every connection's webhook at this deployment (e.g. after moving from a tunnel)."""
    gw = gw or get_gateway()
    out = []
    with SessionLocal() as s:
        for item in s.scalars(select(PlaidItem).where(PlaidItem.status != "removed")):
            try:
                gw.item_webhook_update(_item_token(item), get_settings().webhook_url)
                out.append(f"{item.institution_name}: updated")
            except PlaidError as e:
                out.append(f"{item.institution_name}: failed ({e.error_code})")
    return out
