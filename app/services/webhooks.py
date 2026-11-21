"""Plaid webhook verification and handling.

Verification follows https://plaid.com/docs/api/webhooks/webhook-verification/ :
1. Read the JWT in the `Plaid-Verification` header; its header `alg` must be ES256.
2. Fetch the JWK for the header's `kid` with /webhook_verification_key/get (cached).
3. Verify the signature with that key.
4. Reject if `iat` is more than 5 minutes old (replay protection).
5. SHA-256 the raw request body and compare (constant time) with `request_body_sha256`.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PlaidItem
from app.services.plaid_gateway import REAUTH_ERROR_CODES, Gateway, PlaidError
from app.services.plaid_sync import set_item_status
from app.timeutil import now

log = logging.getLogger(__name__)

MAX_AGE_SECONDS = 5 * 60
_key_cache: dict[str, dict] = {}
_lock = threading.Lock()


class WebhookVerificationError(Exception):
    pass


def clear_key_cache() -> None:
    with _lock:
        _key_cache.clear()


def _get_key(gw: Gateway, kid: str) -> dict:
    with _lock:
        cached = _key_cache.get(kid)
    if cached is not None and not cached.get("expired_at"):
        return cached
    try:
        key = gw.webhook_verification_key_get(kid)["key"]
    except PlaidError as e:
        raise WebhookVerificationError(f"could not fetch verification key ({e.error_code})") from None
    with _lock:
        _key_cache[kid] = key
    return key


def verify(body: bytes, signed_jwt: str | None, gw: Gateway) -> None:
    """Raise WebhookVerificationError unless the webhook is authentic and fresh."""
    if not signed_jwt:
        raise WebhookVerificationError("missing Plaid-Verification header")
    try:
        header = jwt.get_unverified_header(signed_jwt)
    except jwt.PyJWTError:
        raise WebhookVerificationError("malformed JWT") from None
    if header.get("alg") != "ES256":
        raise WebhookVerificationError("JWT alg is not ES256")
    kid = header.get("kid")
    if not kid:
        raise WebhookVerificationError("JWT has no kid")
    key = _get_key(gw, kid)
    if key.get("expired_at"):
        raise WebhookVerificationError("verification key has expired")
    try:
        jwk = jwt.PyJWK({k: key[k] for k in ("kty", "crv", "x", "y", "kid") if k in key}, algorithm="ES256")
        claims = jwt.decode(
            signed_jwt,
            jwk.key,
            algorithms=["ES256"],
            options={"verify_iat": False, "verify_exp": False, "verify_nbf": False, "verify_aud": False, "require": ["iat"]},
        )
    except jwt.PyJWTError as e:
        raise WebhookVerificationError(f"bad signature ({type(e).__name__})") from None
    iat = claims.get("iat")
    if not isinstance(iat, (int, float)):
        raise WebhookVerificationError("iat missing")
    age = now().timestamp() - float(iat)
    if age > MAX_AGE_SECONDS:
        raise WebhookVerificationError("webhook is older than 5 minutes")
    if age < -60:
        raise WebhookVerificationError("webhook iat is in the future")
    expected = claims.get("request_body_sha256")
    if not isinstance(expected, str):
        raise WebhookVerificationError("request_body_sha256 missing")
    actual = hashlib.sha256(body).hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise WebhookVerificationError("body hash does not match")


def handle(session: Session, payload: dict) -> str | None:
    """Update item state for a verified webhook. Returns an action for the caller to run in
    the background: 'sync', 'liabilities', or None."""
    wtype, code = payload.get("webhook_type"), payload.get("webhook_code")
    item = session.scalar(select(PlaidItem).where(PlaidItem.plaid_item_id == payload.get("item_id")))
    if item is None:
        log.info("webhook %s/%s for unknown item ignored", wtype, code)
        return None
    item.last_webhook_at = now()
    if wtype == "TRANSACTIONS":
        # SYNC_UPDATES_AVAILABLE is the one that matters with /transactions/sync; the legacy
        # codes are also sent and simply trigger the same (idempotent) sync.
        return "sync"
    if wtype == "LIABILITIES" and code == "DEFAULT_UPDATE":
        return "liabilities"
    if wtype == "ITEM":
        err = payload.get("error") or {}
        if code == "ERROR":
            ec = err.get("error_code")
            status = "needs_reauth" if ec in REAUTH_ERROR_CODES else "error"
            set_item_status(session, item, status, ec, err.get("display_message") or err.get("error_message"))
        elif code in ("PENDING_DISCONNECT", "PENDING_EXPIRATION"):
            set_item_status(session, item, "expiring", code, "Plaid will disconnect this account soon unless you reconnect it.")
        elif code in ("USER_PERMISSION_REVOKED", "USER_ACCOUNT_REVOKED"):
            set_item_status(session, item, "revoked", code, "Access was revoked at the bank or through Plaid. Reconnect to resume.")
        elif code == "LOGIN_REPAIRED":
            set_item_status(session, item, "active")
            return "sync"
        elif code == "NEW_ACCOUNTS_AVAILABLE":
            item.new_accounts_available = True
        return None
    return None
