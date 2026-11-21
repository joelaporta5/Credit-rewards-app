"""Authentication.

- Dashboard/admin: header `X-API-Key: <ADMIN_API_KEY>`, or log in once in the browser with
  that key; the browser then holds a signed, HttpOnly session cookie.
- iPhone Shortcut: header `X-API-Key: <WALLET_API_KEY>` (or `Authorization: Bearer ...`).
  That key can only submit wallet events, so a leaked phone key can't read anything.
- Plaid webhooks: verified by signature (see services/webhooks.py), no key.

Browser form posts additionally require a same-origin Origin/Referer header (CSRF defence),
on top of the SameSite=Lax cookie.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

from fastapi import HTTPException, Request

from app.config import get_settings

COOKIE = "rap_session"
SESSION_MAX_AGE = 30 * 24 * 3600


class LoginRequired(Exception):
    """Raised for unauthenticated browser requests; turned into a redirect to /login."""


def _eq(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def _sign(issued: int) -> str:
    key = get_settings().session_secret.encode()
    return hmac.new(key, f"session:{issued}".encode(), hashlib.sha256).hexdigest()


def make_session_cookie() -> str:
    issued = int(time.time())
    return f"{issued}.{_sign(issued)}"


def valid_session(value: str | None) -> bool:
    if not value or "." not in value or not get_settings().session_secret:
        return False
    issued_s, sig = value.split(".", 1)
    try:
        issued = int(issued_s)
    except ValueError:
        return False
    if time.time() - issued > SESSION_MAX_AGE:
        return False
    return _eq(sig, _sign(issued))


def _header_key(request: Request) -> str:
    key = request.headers.get("x-api-key", "")
    if not key:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
    return key


def _same_origin(request: Request) -> bool:
    src = request.headers.get("origin") or request.headers.get("referer")
    if not src:
        return False
    host = urlparse(src).netloc
    allowed = {request.headers.get("host", ""), urlparse(get_settings().public_base_url).netloc}
    return host in allowed


def require_admin(request: Request) -> str:
    """Dependency for every non-webhook, non-wallet endpoint."""
    if _eq(_header_key(request), get_settings().admin_api_key):
        return "api_key"
    if valid_session(request.cookies.get(COOKIE)):
        if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin(request):
            raise HTTPException(status_code=403, detail="Cross-site request blocked")
        return "session"
    accepts_html = "text/html" in request.headers.get("accept", "")
    if request.method == "GET" and accepts_html:
        raise LoginRequired()
    raise HTTPException(status_code=401, detail="Authentication required")


def require_wallet_key(request: Request) -> None:
    if not _eq(_header_key(request), get_settings().wallet_api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


class LoginThrottle:
    """At most `limit` failed logins per IP per `window` seconds (in memory)."""

    def __init__(self, limit: int = 10, window: int = 900):
        self.limit, self.window = limit, window
        self.failures: dict[str, deque] = defaultdict(deque)

    def blocked(self, ip: str) -> bool:
        q = self.failures[ip]
        cutoff = time.time() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return len(q) >= self.limit

    def fail(self, ip: str) -> None:
        self.failures[ip].append(time.time())


login_throttle = LoginThrottle()


def check_admin_key(candidate: str) -> bool:
    return _eq(candidate.strip(), get_settings().admin_api_key)
