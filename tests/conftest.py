"""Test setup. Tests run against a real PostgreSQL database (the production engine), with
the schema created by the Alembic migrations, a frozen clock and a fake Plaid gateway.
No network access and no real bank data are used.

Set TEST_DATABASE_URL to point somewhere else (default: local rewards_test database).
The test database is wiped between tests — never point it at real data.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres@localhost:5432/rewards_test")
os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": TEST_DB,
        "ADMIN_API_KEY": "test-admin-key-" + "a" * 40,
        "WALLET_API_KEY": "test-wallet-key-" + "b" * 40,
        "SESSION_SECRET": "test-session-secret-" + "c" * 40,
        "TOKEN_ENCRYPTION_KEY": "test-encryption-key-" + "d" * 40,
        "PLAID_CLIENT_ID": "test-client",
        "PLAID_SECRET": "test-secret",
        "PLAID_ENV": "sandbox",
        "PUBLIC_BASE_URL": "https://testserver",
        "TIMEZONE": "America/New_York",
        "SCHEDULER_ENABLED": "false",
    }
)
if "test" not in TEST_DB.rsplit("/", 1)[-1]:
    raise SystemExit("Refusing to run tests: TEST_DATABASE_URL's database name must contain 'test'")

from sqlalchemy import select, text  # noqa: E402

from app import timeutil  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.crypto import encrypt_token  # noqa: E402
from app.db import SessionLocal, get_engine, normalize_db_url, reset_engine  # noqa: E402
from app.models import Account, Card, PlaidItem, WalletCardAlias  # noqa: E402
from app.services import plaid_sync, settings_store, wallet  # noqa: E402
from app.services.plaid_gateway import set_gateway  # noqa: E402
from app.services.safe_to_spend import compute  # noqa: E402

ADMIN = {"X-API-Key": os.environ["ADMIN_API_KEY"]}
WALLET = {"X-API-Key": os.environ["WALLET_API_KEY"]}
T0 = datetime(2026, 11, 10, 17, 0, tzinfo=timezone.utc)  # Tue Nov 10 2026, 12:00 New York


@pytest.fixture(scope="session", autouse=True)
def _migrated_db():
    get_settings.cache_clear()
    reset_engine()
    from sqlalchemy import create_engine

    eng = create_engine(normalize_db_url(TEST_DB))
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    eng.dispose()
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = TEST_DB
    command.upgrade(cfg, "head")
    yield
    reset_engine()


TABLES = [
    "ledger_entries", "review_items", "match_decisions", "purchases", "reserve_transfers", "wallet_events",
    "wallet_card_aliases", "plaid_transaction_history", "plaid_transactions", "card_liability_snapshots",
    "balance_snapshots", "cards", "sync_runs", "accounts", "plaid_items", "merchants", "bills", "app_settings",
    "audit_log", "webhook_log",
]


@pytest.fixture(autouse=True)
def _clean_db():
    with get_engine().begin() as c:
        c.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    timeutil.freeze(T0)
    from app.services import webhooks

    webhooks.clear_key_cache()
    yield
    timeutil.freeze(None)
    set_gateway(None)


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.rollback()
    s.close()


def txn(tid: str, amount: str, name: str, *, account: str = "acct_card_a", day: date | str | None = None,
        pending: bool = True, pending_id: str | None = None, pfc: str | None = None, merchant_name: str | None = None,
        authorized_date: date | str | None = None, authorized_datetime: str | None = None) -> dict:
    """A Plaid Transaction dict shaped like /transactions/sync output."""
    day = day or timeutil.today()
    primary = pfc.split("_")[0] if pfc else None
    if pfc and pfc.startswith("FOOD_AND_DRINK"):
        primary = "FOOD_AND_DRINK"
    if pfc and pfc.startswith("LOAN_PAYMENTS"):
        primary = "LOAN_PAYMENTS"
    if pfc and pfc.startswith("TRANSFER_IN"):
        primary = "TRANSFER_IN"
    if pfc and pfc.startswith("TRANSFER_OUT"):
        primary = "TRANSFER_OUT"
    return {
        "transaction_id": tid,
        "account_id": account,
        "amount": float(amount),  # Plaid sends JSON numbers
        "iso_currency_code": "USD",
        "unofficial_currency_code": None,
        "date": str(day),
        "authorized_date": str(authorized_date) if authorized_date else None,
        "authorized_datetime": authorized_datetime,
        "datetime": None,
        "name": name,
        "merchant_name": merchant_name,
        "merchant_entity_id": None,
        "merchant_category_code": None,
        "pending": pending,
        "pending_transaction_id": pending_id,
        "payment_channel": "in store",
        "personal_finance_category": {"primary": primary, "detailed": pfc, "confidence_level": "HIGH"} if pfc else None,
        "counterparties": [],
    }


class World:
    """A fake household: one item with checking, reserve and two credit cards."""

    def __init__(self, session):
        self.s = session
        self.item = PlaidItem(plaid_item_id="item_1", plaid_env="sandbox", institution_id="ins_1", institution_name="Test Bank",
                              access_token_encrypted=encrypt_token("access-sandbox-00000000-test"), status="active",
                              last_sync_completed_at=timeutil.now())
        session.add(self.item)
        session.flush()
        plaid_sync.upsert_accounts(session, self.item, [
            {"account_id": "acct_checking", "name": "Checking", "mask": "0001", "type": "depository", "subtype": "checking"},
            {"account_id": "acct_reserve", "name": "Card Reserve", "mask": "0002", "type": "depository", "subtype": "checking"},
            {"account_id": "acct_card_a", "name": "Gold Card", "mask": "1001", "type": "credit", "subtype": "credit card"},
            {"account_id": "acct_card_b", "name": "Cash Card", "mask": "2002", "type": "credit", "subtype": "credit card"},
        ])
        self.acct = {a.plaid_account_id: a for a in session.scalars(select(Account))}
        self.acct["acct_checking"].role = "primary_checking"
        self.acct["acct_reserve"].role = "reserve"
        self.card_a = session.scalar(select(Card).where(Card.account_id == self.acct["acct_card_a"].id))
        self.card_b = session.scalar(select(Card).where(Card.account_id == self.acct["acct_card_b"].id))
        session.add(WalletCardAlias(normalized_name="AMEX GOLD", display_name="Amex Gold", card_id=self.card_a.id))
        session.add(WalletCardAlias(normalized_name="CASH CARD", display_name="Cash Card", card_id=self.card_b.id))
        settings_store.set_value(session, "tracking_start_date", (timeutil.today() - timedelta(days=30)).isoformat())
        settings_store.set_value(session, "minimum_buffer_cents", "0")
        settings_store.set_value(session, "uncertainty_holdback_cents", "25000")
        session.commit()

    # -- inputs ---------------------------------------------------------------
    def tap(self, amount, merchant, card="Amex Gold", **extra):
        payload = {"amount": amount, "merchant": merchant, "card": card, "timestamp": timeutil.now().isoformat(), **extra}
        ev = wallet.ingest(self.s, payload)
        self.s.commit()
        wallet.process(self.s, ev.id)
        self.s.commit()
        return ev

    def plaid(self, added=(), modified=(), removed=()):
        out = plaid_sync.apply_changes(self.s, list(added), list(modified), [{"transaction_id": r, "account_id": "x"} for r in removed])
        self.s.commit()
        return out

    def balance(self, account: str, available: str):
        plaid_sync.store_balances(self.s, [{"account_id": account, "balances": {"available": float(available), "current": float(available)}}], "test")
        self.s.commit()

    def dash(self):
        d = compute(self.s)
        self.s.commit()
        return d


@pytest.fixture
def world(db):
    return World(db)
