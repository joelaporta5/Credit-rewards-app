"""End-to-end scenario against the real Plaid **Sandbox** (test data only).

What it does, using a separate throwaway database:
  1. Creates a Sandbox Item (First Platypus Bank, user_transactions_dynamic) and stores the
     encrypted access token exactly like the Connect page does.
  2. Marks checking / reserve / credit card roles.
  3. Runs /transactions/sync until Plaid's initial data is ready, plus balances and liabilities.
  4. Sends an Apple Pay event, creates the matching card transaction in Sandbox
     (/sandbox/transactions/create), refreshes, syncs, and checks they were matched once.
  5. Sends a duplicate Apple Pay delivery and checks it's counted once.
  6. Marks a reserve transfer and creates both sides in Sandbox; checks no double count.
  7. Calls /transactions/refresh ("Refresh now").
  8. Forces ITEM_LOGIN_REQUIRED (/sandbox/item/reset_login) and checks the connection is
     flagged for re-authentication.
  9. Checks ledger integrity (dashboard totals == sum of ledger entries).

It can't receive Plaid webhooks (no public URL); test those after deploying with the
"Send a test webhook" button on the Connect page.

Usage (PowerShell):
  $env:PLAID_CLIENT_ID="..."; $env:PLAID_SECRET="<sandbox secret>"
  python scripts/sandbox_e2e.py
Optional: E2E_DATABASE_URL (default postgresql://postgres@localhost:5432/rewards_e2e)
"""

from __future__ import annotations

import os
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

E2E_DB = os.environ.get("E2E_DATABASE_URL", "postgresql://postgres@localhost:5432/rewards_e2e")
if "e2e" not in E2E_DB.rsplit("/", 1)[-1]:
    raise SystemExit("E2E_DATABASE_URL's database name must contain 'e2e' (it gets wiped).")
os.environ["DATABASE_URL"] = E2E_DB
os.environ["PLAID_ENV"] = "sandbox"
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "e2e-only-encryption-key-" + "x" * 32)
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.invalid")

from sqlalchemy import create_engine, func, select, text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, normalize_db_url  # noqa: E402
from app.models import Account, LedgerEntry, PlaidItem, PlaidTransaction, Purchase, ReserveTransfer, WalletCardAlias, Card  # noqa: E402
from app.services import ledger, plaid_sync, settings_store, transfers, wallet  # noqa: E402
from app.services.plaid_gateway import PlaidError, PlaidGateway  # noqa: E402
from app.services.safe_to_spend import compute  # noqa: E402
from app.timeutil import today  # noqa: E402
from app.money import fmt  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}", flush=True)


def reset_db() -> None:
    admin_url = normalize_db_url(E2E_DB.rsplit("/", 1)[0] + "/postgres")
    name = E2E_DB.rsplit("/", 1)[-1]
    eng = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        c.execute(text(f'CREATE DATABASE "{name}"'))
    eng.dispose()
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    cfg.attributes["database_url"] = E2E_DB
    command.upgrade(cfg, "head")


def sync_until(item_id: int, gw: PlaidGateway, predicate, label: str, tries: int = 12, wait: float = 5.0):
    for i in range(tries):
        res = plaid_sync.sync_item(item_id, "e2e", force_balance=True, gw=gw)
        with SessionLocal() as s:
            if predicate(s):
                return res, i + 1
        time.sleep(wait)
    return res, tries


def main() -> int:
    s = get_settings()
    if not s.plaid_client_id or not s.plaid_secret:
        raise SystemExit("Set PLAID_CLIENT_ID and PLAID_SECRET (Sandbox) first.")
    print(f"Database: {E2E_DB}\nPlaid: sandbox\n")
    reset_db()
    gw = PlaidGateway()

    # 1. Create a Sandbox Item ---------------------------------------------------
    try:
        pt = gw.sandbox_public_token_create("ins_109508", ["transactions", "liabilities"], webhook=s.webhook_url,
                                            override_username="user_transactions_dynamic")
        products = "transactions+liabilities"
    except PlaidError as e:
        print(f"  (liabilities not available for this test user: {e.error_code}; retrying with transactions only)")
        pt = gw.sandbox_public_token_create("ins_109508", ["transactions"], webhook=s.webhook_url,
                                            override_username="user_transactions_dynamic")
        products = "transactions"
    with SessionLocal() as db:
        item = plaid_sync.connect_item(db, gw, pt["public_token"], "ins_109508", "First Platypus Bank")
        db.commit()
        item_id = item.id
        enc = item.access_token_encrypted
    check("Sandbox Item created and token stored encrypted", "access-sandbox" not in enc, f"products: {products}")

    # The dynamic test user has a single bank account, so the reserve account comes from a
    # second Sandbox bank login (its checking account plays the reserve).
    pt2 = gw.sandbox_public_token_create("ins_109508", ["transactions"], webhook=s.webhook_url,
                                         override_username="user_transactions_dynamic")
    with SessionLocal() as db:
        item2 = plaid_sync.connect_item(db, gw, pt2["public_token"], "ins_109508", "First Platypus Bank (reserve)")
        db.commit()
        item2_id = item2.id

    # 2. Roles ----------------------------------------------------------------------
    with SessionLocal() as db:
        accts = db.scalars(select(Account).order_by(Account.id)).all()
        print("  accounts:", ", ".join(f"{a.name} ({a.type}/{a.subtype}, item {a.item_id})" for a in accts))
        dep = [a for a in accts if a.type == "depository" and a.item_id == item_id]
        checking = next((a for a in dep if a.subtype == "checking"), dep[0] if dep else None)
        dep2 = [a for a in accts if a.type == "depository" and a.item_id == item2_id]
        reserve = next((a for a in dep2 if a.subtype == "checking"), dep2[0] if dep2 else None)
        if checking:
            checking.role = "primary_checking"
        if reserve:
            reserve.role = "reserve"
        cards = db.scalars(select(Card)).all()
        card = cards[0] if cards else None
        if card:
            db.add(WalletCardAlias(normalized_name="SANDBOX CREDIT", display_name="Sandbox Credit", card_id=card.id))
        settings_store.set_value(db, "tracking_start_date", (today() - timedelta(days=2)).isoformat())
        settings_store.set_value(db, "minimum_buffer_cents", "10000")
        db.commit()
        card_acct_plaid_id = card.account.plaid_account_id if card else None
        checking_plaid_id = checking.plaid_account_id if checking else None
        reserve_plaid_id = reserve.plaid_account_id if reserve else None
    check("Checking, reserve and credit card accounts found", bool(checking and reserve and card),
          f"checking={bool(checking)} reserve={bool(reserve)} card={bool(card)}")
    if not card or not checking:
        return summary()

    # 3. Initial sync ------------------------------------------------------------------
    res, n = sync_until(item_id, gw, lambda db: db.scalar(select(func.count()).select_from(PlaidTransaction)) > 0, "initial")
    with SessionLocal() as db:
        n_tx = db.scalar(select(func.count()).select_from(PlaidTransaction))
        it = db.get(PlaidItem, item_id)
        bal = transfers.latest_balance(db, db.scalar(select(Account.id).where(Account.role == "primary_checking")))
        liab = db.scalar(select(func.count()).select_from(__import__("app.models", fromlist=["CardLiabilitySnapshot"]).CardLiabilitySnapshot))
    check("/transactions/sync returned transactions and saved a cursor", n_tx > 0 and bool(it.transactions_cursor),
          f"{n_tx} transactions after {n} sync(s), status {it.transactions_update_status}, last messages {res.messages}")
    check("/accounts/balance/get stored a checking balance", bal is not None and bal.available_cents is not None,
          f"available {fmt(bal.available_cents) if bal else None}")
    check("/liabilities/get stored card statement data", products == "transactions" or liab > 0, f"{liab} snapshot(s)")

    # 4. Apple Pay event + matching Sandbox transaction -------------------------------
    with SessionLocal() as db:
        ev = wallet.ingest(db, {"amount": "$12.34", "merchant": "Blue Bottle Coffee", "card": "Sandbox Credit"})
        db.commit()
        wallet.process(db, ev.id)
        db.commit()
        ev_id = ev.id
        before = compute(db).card_spend_uncovered
    gw.sandbox_transactions_create(plaid_sync._item_token(_item(item_id)), [
        {"date_transacted": today(), "date_posted": today(), "amount": 12.34, "description": "SQ *BLUE BOTTLE COFFEE",
         "account_id": card_acct_plaid_id},
    ])
    try:
        gw.transactions_refresh(plaid_sync._item_token(_item(item_id)))
    except PlaidError as e:
        print(f"  refresh: {e.error_code}")

    def matched(db):
        p = db.scalar(select(Purchase).where(Purchase.wallet_event_id == ev_id))
        return p is not None and p.plaid_transaction_id is not None

    res, n = sync_until(item_id, gw, matched, "match", tries=18)
    with SessionLocal() as db:
        p = db.scalar(select(Purchase).where(Purchase.wallet_event_id == ev_id))
        after = compute(db).card_spend_uncovered
        n_blue = db.scalar(select(func.count()).select_from(PlaidTransaction).where(PlaidTransaction.name.ilike("%BLUE BOTTLE%")))
    check("Apple Pay event matched to the Sandbox transaction", p.plaid_transaction_id is not None,
          f"confidence {p.match_confidence}, after {n} sync(s); Plaid rows for it: {n_blue}")
    with SessionLocal() as db:
        n_purchases = db.scalar(select(func.count()).select_from(Purchase).join(
            PlaidTransaction, PlaidTransaction.id == Purchase.plaid_transaction_id).where(
            PlaidTransaction.name.ilike("%BLUE BOTTLE%"), Purchase.state == "active"))
        counted = ledger.purchase_sum(db, p.id)
    check("Matched purchase counted once", n_purchases == 1 and counted == 1234 and n_blue == 1,
          f"1 Plaid row, {n_purchases} active purchase, ledger total for it {fmt(counted)} "
          f"(overall card spend moved {fmt(before)} -> {fmt(after)} because Sandbox also added unrelated charges)")

    # 5. Duplicate delivery --------------------------------------------------------
    with SessionLocal() as db:
        dup = wallet.ingest(db, {"amount": "12.34", "merchant": "Blue Bottle Coffee", "card": "Sandbox Credit"})
        db.commit()
        wallet.process(db, dup.id)
        db.commit()
        check("Duplicate Shortcut delivery counted once", dup.status == "duplicate" and compute(db).card_spend_uncovered == after)

    # 6. Reserve transfer -------------------------------------------------------------
    if reserve_plaid_id:
        with SessionLocal() as db:
            d0 = compute(db)
            tr = transfers.create(db, 30000, "e2e")
            db.commit()
            tr_id = tr.id
            d1 = compute(db)
        gw.sandbox_transactions_create(plaid_sync._item_token(_item(item_id)), [
            {"date_transacted": today(), "date_posted": today(), "amount": 300.00, "description": "ONLINE TRANSFER TO RESERVE",
             "account_id": checking_plaid_id},
        ])
        try:
            gw.sandbox_transactions_create(plaid_sync._item_token(_item(item2_id)), [
                {"date_transacted": today(), "date_posted": today(), "amount": -300.00, "description": "ONLINE TRANSFER FROM CHECKING",
                 "account_id": reserve_plaid_id},
            ])
        except PlaidError as e:
            print(f"  (custom transactions not supported for the reserve test user: {e.error_code}; reserve side skipped)")
        for iid in (item_id, item2_id):
            try:
                gw.transactions_refresh(plaid_sync._item_token(_item(iid)))
            except PlaidError:
                pass

        def moved(db):
            plaid_sync.sync_item(item2_id, "e2e", force_balance=True, gw=gw)
            return db.get(ReserveTransfer, tr_id).status == "confirmed"

        sync_until(item_id, gw, moved, "transfer", tries=18)
        with SessionLocal() as db:
            tr = db.get(ReserveTransfer, tr_id)
            d2 = compute(db)
            entries = db.scalars(select(LedgerEntry).where(LedgerEntry.reserve_transfer_id == tr_id)).all()
        check("Marked transfer doesn't change safe-to-spend while pending", d1.safe_to_spend == d0.safe_to_spend,
              f"{fmt(d0.safe_to_spend)} -> {fmt(d1.safe_to_spend)}")
        check("Transfer seen leaving checking, recorded once in the ledger", tr.status in ("left_checking", "confirmed") and len(entries) == 1,
              f"status {tr.status}, ledger entries {[e.amount_cents for e in entries]}")

        check("Transfer confirmed on both accounts", tr.status == "confirmed", f"status {tr.status}")

    # 7. Refresh now ---------------------------------------------------------------------
    r, note = plaid_sync.refresh_item(item_id, gw=gw)
    check("'Refresh now' ran", r.status == "ok", note)

    # 8. Integrity --------------------------------------------------------------------------
    with SessionLocal() as db:
        d = compute(db)
        raw = db.execute(text("SELECT COALESCE(SUM(amount_cents),0) FROM ledger_entries")).scalar()
        bad = [p.id for p in db.scalars(select(Purchase)) if ledger.purchase_sum(db, p.id) != ledger.purchase_target(p)]
        check("Ledger integrity: dashboard total == sum of entries; every purchase reconciles",
              d.ledger_total == raw and not bad, f"sum {fmt(raw)}; safe-to-spend {fmt(d.safe_to_spend)}")
        print(f"\n  Dashboard: verified cash {fmt(d.verified_cash)} − card spend {fmt(d.card_spend_uncovered)} − bills "
              f"{fmt(d.bills_total)} − buffer {fmt(d.minimum_buffer)} − holdback {fmt(d.holdback)} = {fmt(d.safe_to_spend)}\n")

    # 9. Re-authentication ------------------------------------------------------------------
    gw.sandbox_reset_login(plaid_sync._item_token(_item(item_id)))
    plaid_sync.sync_item(item_id, "e2e", force_balance=True, gw=gw)
    with SessionLocal() as db:
        it = db.get(PlaidItem, item_id)
        check("ITEM_LOGIN_REQUIRED flags the connection for re-authentication", it.status == "needs_reauth",
              f"status {it.status} ({it.error_code})")
    return summary()


def _item(item_id: int) -> PlaidItem:
    with SessionLocal() as db:
        return db.get(PlaidItem, item_id)


def summary() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
