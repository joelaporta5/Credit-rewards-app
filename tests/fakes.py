"""A fake Plaid gateway for tests: no network, scripted responses, real ES256 webhook signing."""

from __future__ import annotations

import hashlib
import json

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from app import timeutil
from app.services.plaid_gateway import PlaidError


class FakePlaid:
    def __init__(self):
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.kid = "fake-key-1"
        self.sync_pages: list = []  # each: dict response or PlaidError
        self.sync_calls: list = []
        self.accounts = [
            {"account_id": "acct_checking", "name": "Checking", "mask": "0001", "type": "depository", "subtype": "checking",
             "balances": {"available": 1500.0, "current": 1500.0, "limit": None, "iso_currency_code": "USD"}},
            {"account_id": "acct_card_a", "name": "Gold Card", "mask": "1001", "type": "credit", "subtype": "credit card",
             "balances": {"available": 9000.0, "current": 1000.0, "limit": 10000.0, "iso_currency_code": "USD"}},
        ]
        self.liabilities = {"credit": [{"account_id": "acct_card_a", "last_statement_balance": 812.33,
                                        "last_statement_issue_date": "2026-10-25", "next_payment_due_date": "2026-11-20",
                                        "minimum_payment_amount": 35.0, "last_payment_amount": 0, "last_payment_date": None,
                                        "is_overdue": False, "aprs": []}]}
        self.item_error = None
        self.refresh_error: PlaidError | None = None
        self.calls: list[str] = []
        self.link_token_kwargs: list[dict] = []

    # -- webhook signing ---------------------------------------------------
    def public_jwk(self) -> dict:
        jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(self.private_key.public_key()))
        return {**jwk, "alg": "ES256", "kid": self.kid, "use": "sig", "created_at": 1560466150, "expired_at": None}

    def sign(self, body: bytes, *, key=None, iat=None, kid=None, body_hash=None, alg="ES256") -> str:
        claims = {"iat": int(iat if iat is not None else timeutil.now().timestamp()),
                  "request_body_sha256": body_hash or hashlib.sha256(body).hexdigest()}
        if alg == "HS256":
            return jwt.encode(claims, "x" * 32, algorithm="HS256", headers={"kid": kid or self.kid})
        return jwt.encode(claims, key or self.private_key, algorithm="ES256", headers={"kid": kid or self.kid})

    # -- Gateway protocol ----------------------------------------------------
    def link_token_create(self, **kw):
        self.calls.append("link_token_create")
        self.link_token_kwargs.append(kw)
        return {"link_token": "link-sandbox-fake", "expiration": "2026-11-10T21:00:00Z"}

    def public_token_exchange(self, public_token):
        self.calls.append("public_token_exchange")
        return {"access_token": "access-sandbox-11111111-2222", "item_id": "item_fake_1"}

    def transactions_sync(self, access_token, cursor, count=500):
        self.calls.append("transactions_sync")
        self.sync_calls.append(cursor)
        if not self.sync_pages:
            return {"added": [], "modified": [], "removed": [], "next_cursor": cursor or "c0", "has_more": False,
                    "accounts": [], "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE"}
        page = self.sync_pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return {"accounts": [], "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE", "modified": [], "removed": [], **page}

    def transactions_refresh(self, access_token):
        self.calls.append("transactions_refresh")
        if self.refresh_error:
            raise self.refresh_error
        return {"request_id": "r"}

    def accounts_get(self, access_token):
        self.calls.append("accounts_get")
        return {"accounts": self.accounts, "item": {"institution_name": "Fake Bank"}}

    def accounts_balance_get(self, access_token, account_ids=None):
        self.calls.append("accounts_balance_get")
        return {"accounts": self.accounts, "item": {}}

    def liabilities_get(self, access_token):
        self.calls.append("liabilities_get")
        return {"accounts": self.accounts, "liabilities": self.liabilities}

    def item_get(self, access_token):
        self.calls.append("item_get")
        return {"item": {"error": self.item_error, "consent_expiration_time": None},
                "status": {"transactions": {"last_successful_update": timeutil.now().isoformat()}}}

    def item_webhook_update(self, access_token, webhook):
        self.calls.append("item_webhook_update")
        return {}

    def webhook_verification_key_get(self, key_id):
        self.calls.append("webhook_verification_key_get")
        if key_id != self.kid:
            raise PlaidError("INVALID_KEY_ID", "INVALID_INPUT", "unknown key")
        return {"key": self.public_jwk()}
