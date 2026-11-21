"""HTTP-level tests: auth, the Apple Pay endpoint, Plaid webhooks (incl. required test 13),
Link/exchange, sync orchestration and the dashboard page. All Plaid calls go to a fake."""

from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import timeutil
from app.crypto import decrypt_token
from app.main import app
from app.models import (
    Account,
    BalanceSnapshot,
    CardLiabilitySnapshot,
    LedgerEntry,
    PlaidItem,
    PlaidTransaction,
    ReviewItem,
    SyncRun,
    WalletEvent,
    WebhookLog,
)
from app.services import plaid_sync
from app.services.plaid_gateway import PlaidError, set_gateway
from tests.conftest import ADMIN, WALLET, txn
from tests.fakes import FakePlaid

BROWSER = {"Accept": "text/html", "Origin": "https://testserver"}


@pytest.fixture
def fake():
    f = FakePlaid()
    set_gateway(f)
    return f


@pytest.fixture
def client(fake):
    with TestClient(app, base_url="https://testserver") as c:
        yield c


def login(client):
    r = client.post("/login", data={"api_key": ADMIN["X-API-Key"], "next": "/"}, headers=BROWSER, follow_redirects=False)
    assert r.status_code == 303
    return r


# --- auth -------------------------------------------------------------------------


def test_every_non_webhook_endpoint_requires_auth(client):
    assert client.get("/healthz").status_code == 200
    r = client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    for path in ("/api/dashboard", "/review", "/accounts", "/ledger", "/activity", "/settings", "/bills", "/connect"):
        assert client.get(path).status_code == 401, path
    for path in ("/plaid/link-token", "/plaid/exchange", "/refresh", "/transfers", "/settings", "/bills"):
        assert client.post(path, json={}).status_code == 401, path
    # The wallet key can't read the dashboard, and the admin key isn't accepted on the wallet endpoint.
    assert client.get("/api/dashboard", headers=WALLET).status_code == 401
    assert client.post("/events/wallet", json={"amount": "1"}, headers=ADMIN).status_code == 401
    assert client.post("/events/wallet", json={"amount": "1"}).status_code == 401
    assert client.get("/api/dashboard", headers=ADMIN).status_code == 200


def test_browser_login_cookie_and_csrf_check(client):
    r = client.post("/login", data={"api_key": "wrong", "next": "/"}, headers=BROWSER)
    assert r.status_code == 401
    login(client)
    assert client.get("/", headers={"Accept": "text/html"}).status_code == 200
    # A cross-site form post with the cookie is refused.
    r = client.post("/bills", data={"name": "x", "amount": "1", "due_date": "2026-11-20"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/bills", data={"name": "Rent", "amount": "1,200.00", "due_date": "2026-11-20", "recurrence": "monthly"},
                    headers=BROWSER, follow_redirects=False)
    assert r.status_code == 303


# --- Apple Pay endpoint --------------------------------------------------------------


def test_wallet_endpoint_accepts_messy_input_and_dedupes(client, world):
    r = client.post("/events/wallet", headers=WALLET, json={"Amount": "$12,34", "Merchant": " Blue  Bottle ", "Card": "Amex Gold",
                                                             "Date": "Nov 10, 2026 at 11:58 AM"})
    assert r.status_code == 202 and r.json()["status"] == "accepted" and r.json()["amount_cents"] == 1234
    r2 = client.post("/events/wallet", headers={"Authorization": "Bearer " + WALLET["X-API-Key"]},
                     json={"amount": "12.34", "merchant": "Blue Bottle", "card": "Amex Gold"})
    assert r2.json()["status"] == "duplicate"
    # Missing everything still accepted (recorded, sent to review), never a 500.
    r3 = client.post("/events/wallet", headers=WALLET, content=b"not json at all")
    assert r3.status_code == 202
    s = world.s
    s.expire_all()
    assert s.scalar(select(func.count()).select_from(WalletEvent)) == 3
    assert s.scalar(select(func.count()).select_from(ReviewItem).where(ReviewItem.kind == "amount_problem")) == 1
    d = client.get("/api/dashboard", headers=ADMIN).json()
    assert d["card_spend_not_yet_covered_cents"] == 1234
    assert d["guaranteed"] is False


# --- Webhooks (required test 13) --------------------------------------------------------


def _hook_body(**kw):
    body = {"webhook_type": "TRANSACTIONS", "webhook_code": "SYNC_UPDATES_AVAILABLE", "item_id": "item_1",
            "initial_update_complete": True, "historical_update_complete": True, "environment": "sandbox", **kw}
    return json.dumps(body, indent=2).encode()


def test_13_webhook_with_invalid_signature_is_rejected(client, fake, world):
    body = _hook_body()
    other_key = ec.generate_private_key(ec.SECP256R1())
    cases = {
        "missing header": None,
        "signed by another key": fake.sign(body, key=other_key),
        "body tampered": fake.sign(_hook_body(webhook_code="DEFAULT_UPDATE")),
        "replayed (6 min old)": fake.sign(body, iat=timeutil.now().timestamp() - 360),
        "wrong algorithm": fake.sign(body, alg="HS256"),
        "unknown key id": fake.sign(body, kid="nope"),
        "garbage": "not.a.jwt",
    }
    for name, sig in cases.items():
        headers = {"Content-Type": "application/json"}
        if sig:
            headers["Plaid-Verification"] = sig
        r = client.post("/webhooks/plaid", content=body, headers=headers)
        assert r.status_code == 401, name
    assert fake.calls.count("transactions_sync") == 0  # nothing was acted on
    world.s.expire_all()
    logs = world.s.scalars(select(WebhookLog)).all()
    assert len(logs) == len(cases) and not any(l.verified for l in logs)

    # A correctly signed webhook is accepted and triggers a sync.
    r = client.post("/webhooks/plaid", content=body, headers={"Plaid-Verification": fake.sign(body), "Content-Type": "application/json"})
    assert r.status_code == 200
    assert "transactions_sync" in fake.calls


def test_item_error_webhook_flags_reauth_on_dashboard(client, fake, world):
    body = _hook_body(webhook_type="ITEM", webhook_code="ERROR",
                      error={"error_type": "ITEM_ERROR", "error_code": "ITEM_LOGIN_REQUIRED", "error_message": "login changed",
                             "display_message": "Please log in again."})
    r = client.post("/webhooks/plaid", content=body, headers={"Plaid-Verification": fake.sign(body)})
    assert r.status_code == 200
    world.s.expire_all()
    assert world.s.get(PlaidItem, world.item.id).status == "needs_reauth"
    login(client)
    html = client.get("/", headers={"Accept": "text/html"}).text
    assert "needs attention" in html and "Reconnect" in html
    # Reconnecting through update mode clears it.
    r = client.post(f"/plaid/items/{world.item.id}/reconnected", headers=BROWSER)
    assert r.status_code == 200
    world.s.expire_all()
    assert world.s.get(PlaidItem, world.item.id).status == "active"


def test_login_repaired_and_pending_disconnect(client, fake, world):
    for code, status in (("PENDING_DISCONNECT", "expiring"), ("LOGIN_REPAIRED", "active"), ("USER_PERMISSION_REVOKED", "revoked")):
        body = _hook_body(webhook_type="ITEM", webhook_code=code)
        assert client.post("/webhooks/plaid", content=body, headers={"Plaid-Verification": fake.sign(body)}).status_code == 200
        world.s.expire_all()
        assert world.s.get(PlaidItem, world.item.id).status == status


# --- Link + sync ------------------------------------------------------------------------


def test_link_exchange_stores_encrypted_token_and_syncs(client, fake, db):
    login(client)
    r = client.post("/plaid/link-token", json={}, headers=BROWSER)
    assert r.status_code == 200 and r.json()["link_token"] == "link-sandbox-fake"
    assert fake.link_token_kwargs[0]["webhook"] == "https://testserver/webhooks/plaid"
    fake.sync_pages = [{"added": [txn("t1", "20.00", "CHIPOTLE", pending=False)], "next_cursor": "c1", "has_more": False}]
    r = client.post("/plaid/exchange", json={"public_token": "public-sandbox-x", "institution_id": "ins_9", "institution_name": "Fake Bank"},
                    headers=BROWSER)
    assert r.status_code == 200, r.text
    item = db.scalar(select(PlaidItem))
    assert "access-sandbox" not in item.access_token_encrypted  # encrypted at rest
    assert decrypt_token(item.access_token_encrypted) == "access-sandbox-11111111-2222"
    assert item.transactions_cursor == "c1"
    assert db.scalar(select(func.count()).select_from(BalanceSnapshot)) == 2
    assert db.scalar(select(func.count()).select_from(CardLiabilitySnapshot)) == 1
    # The token never appears in any page or API response.
    for path in ("/", "/connect", "/accounts", "/activity", "/api/dashboard"):
        assert "access-sandbox-11111111" not in client.get(path, headers={"Accept": "text/html"}).text
    # Connecting the same bank twice needs explicit confirmation (Trial slots).
    r = client.post("/plaid/exchange", json={"public_token": "p2", "institution_id": "ins_9"}, headers=BROWSER)
    assert r.status_code == 409


def test_sync_restarts_pagination_after_mutation_error(world, fake):
    fake.sync_pages = [
        {"added": [txn("a1", "5.00", "A")], "next_cursor": "p1", "has_more": True},
        PlaidError("TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION", "TRANSACTIONS_ERROR", "mutated"),
        {"added": [txn("a1", "5.00", "A")], "next_cursor": "p1", "has_more": True},
        {"added": [txn("a2", "6.00", "B")], "next_cursor": "p2", "has_more": False},
    ]
    world.item.transactions_cursor = "start"
    world.s.commit()
    res = plaid_sync.sync_item(world.item.id, "manual", gw=fake)
    assert res.status == "ok", res.messages
    assert fake.sync_calls == ["start", "p1", "start", "p1"]
    world.s.expire_all()
    assert world.s.get(PlaidItem, world.item.id).transactions_cursor == "p2"
    assert world.s.scalar(select(func.count()).select_from(PlaidTransaction)) == 2
    assert world.s.scalar(select(func.sum(LedgerEntry.amount_cents))) == 1100


def test_sync_error_marks_reauth_and_keeps_cursor(world, fake):
    world.item.transactions_cursor = "keep"
    world.s.commit()
    fake.sync_pages = [PlaidError("ITEM_LOGIN_REQUIRED", "ITEM_ERROR", "login required", "Log in again")]
    res = plaid_sync.sync_item(world.item.id, "manual", gw=fake)
    assert res.status == "error"
    world.s.expire_all()
    item = world.s.get(PlaidItem, world.item.id)
    assert item.status == "needs_reauth" and item.transactions_cursor == "keep"
    assert world.s.scalar(select(SyncRun.status).order_by(SyncRun.id.desc())) == "error"


def test_refresh_falls_back_to_sync_when_not_on_plan(client, fake, world):
    fake.refresh_error = PlaidError("PRODUCTS_NOT_SUPPORTED", "INVALID_REQUEST", "refresh not enabled")
    login(client)
    r = client.post("/refresh", headers=BROWSER, follow_redirects=False)
    assert r.status_code == 303
    assert "ran a normal sync instead" in r.headers["location"].replace("%20", " ")
    assert "transactions_sync" in fake.calls and "accounts_balance_get" in fake.calls


def test_dashboard_page_renders_breakdown(client, world):
    world.balance("acct_checking", "1000.00")
    world.tap("25.00", "Chipotle")
    login(client)
    html = client.get("/", headers={"Accept": "text/html"}).text
    for text_ in ("Safe to spend", "$975.00", "Card spend not yet covered", "Not guaranteed", "Refresh now", "Suggested transfer"):
        assert text_ in html, text_
    timeutil.advance(hours=13)
    html = client.get("/", headers={"Accept": "text/html"}).text
    assert "Checking balance is from 13 hours ago" in html and "$725.00" in html  # holdback $250 applied


def test_review_page_actions(client, world):
    world.tap("5.25", "Starbucks")
    timeutil.advance(minutes=20)
    world.tap("5.25", "Starbucks")
    world.plaid(added=[txn("t1", "5.25", "STARBUCKS"), txn("t2", "5.25", "STARBUCKS")])
    login(client)
    html = client.get("/review", headers={"Accept": "text/html"}).text
    assert "Which tap is this charge?" in html
    item = world.s.scalar(select(ReviewItem).where(ReviewItem.kind == "ambiguous_match").order_by(ReviewItem.id))
    ev_id = item.details["candidates"][0]["wallet_event_id"]
    r = client.post(f"/review/{item.id}/confirm", data={"wallet_event_id": ev_id}, headers=BROWSER, follow_redirects=False)
    assert r.status_code == 303
    world.s.expire_all()
    assert world.s.scalar(select(func.count()).select_from(ReviewItem).where(ReviewItem.status == "open")) == 0
    assert client.get("/api/dashboard", headers=ADMIN).json()["card_spend_not_yet_covered_cents"] == 1050


def test_account_roles_and_wallet_mapping_pages(client, world):
    login(client)
    assert client.get("/accounts", headers={"Accept": "text/html"}).status_code == 200
    checking = world.acct["acct_checking"]
    reserve = world.acct["acct_reserve"]
    r = client.post(f"/accounts/{reserve.id}/role", data={"role": "primary_checking"}, headers=BROWSER, follow_redirects=False)
    assert r.status_code == 303
    world.s.expire_all()
    assert world.s.get(Account, reserve.id).role == "primary_checking"
    assert world.s.get(Account, checking.id).role == "ignored"  # only one primary checking
    r = client.post("/wallet-cards", data={"name": "Sapphire", "card_id": str(world.card_b.id)}, headers=BROWSER, follow_redirects=False)
    assert r.status_code == 303
    for page in ("/settings", "/bills", "/ledger", "/activity", "/connect", "/review?show=all"):
        assert client.get(page, headers={"Accept": "text/html"}).status_code == 200, page
