"""Endpoints reachable from the internet without the admin key: health check, login,
the iPhone Shortcut endpoint (wallet key) and Plaid webhooks (signature-verified)."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse

from app import jobs
from app.config import get_settings
from app.db import SessionLocal
from app.models import WebhookLog
from app.security import (
    COOKIE,
    SESSION_MAX_AGE,
    check_admin_key,
    has_valid_wallet_header,
    login_throttle,
    make_session_cookie,
    wallet_key_matches,
)
from app.services import wallet, webhooks
from app.services.plaid_gateway import get_gateway
from app.templating import templates

log = logging.getLogger(__name__)
router = APIRouter()

MAX_WALLET_BODY = 16 * 1024
MAX_WEBHOOK_BODY = 256 * 1024


@router.get("/healthz")
def healthz():
    return {"ok": True}


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    return templates.TemplateResponse(request, "login.html", {"next": next if next.startswith("/") else "/", "error": None})


@router.post("/login")
def login(request: Request, api_key: str = Form(...), next: str = Form("/")):
    ip = request.client.host if request.client else "?"
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    if login_throttle.blocked(ip):
        return templates.TemplateResponse(request, "login.html", {"next": target, "error": "Too many attempts. Try again in 15 minutes."}, status_code=429)
    if not check_admin_key(api_key):
        login_throttle.fail(ip)
        return templates.TemplateResponse(request, "login.html", {"next": target, "error": "That key isn't right."}, status_code=401)
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(COOKIE, make_session_cookie(), max_age=SESSION_MAX_AGE, httponly=True, secure=get_settings().is_https, samesite="lax")
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


async def _read_payload(request: Request):
    body = await request.body()
    if len(body) > MAX_WALLET_BODY:
        return None, "body too large"
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return {}, None
    try:
        return json.loads(text), None
    except ValueError:
        pass
    ctype = request.headers.get("content-type", "")
    if "form" in ctype:
        from urllib.parse import parse_qsl

        return dict(parse_qsl(text)), None
    return {"_unstructured": text[:2000]}, None


BODY_KEY_FIELDS = ("key", "api_key", "apikey", "wallet_key")


@router.post("/events/wallet", status_code=202)
async def wallet_event(request: Request, background: BackgroundTasks):
    """Called by the iPhone Shortcut on every Apple Pay tap. Stores the event and answers
    immediately; matching runs in the background.

    The wallet key may be sent as a header (X-API-Key / Authorization: Bearer) or, to keep the
    Shortcut simple, as a "key" field in the JSON body. A body key is removed before anything is
    stored or logged."""
    payload, err = await _read_payload(request)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=413)
    body_key = ""
    if isinstance(payload, dict):
        for k in list(payload.keys()):
            if str(k).strip().lower() in BODY_KEY_FIELDS:
                body_key = str(payload.pop(k) or "")
    if not (has_valid_wallet_header(request) or wallet_key_matches(body_key)):
        return JSONResponse({"ok": False, "error": "Invalid or missing API key"}, status_code=401)

    def store():
        with SessionLocal() as s:
            ev = wallet.ingest(s, payload)
            s.commit()
            return ev.id, ev.status, ev.amount_cents, ev.parse_warnings or []

    event_id, status, cents, warnings = await run_in_threadpool(store)
    if status == "accepted":
        background.add_task(jobs.process_wallet_event, event_id)
    return {"ok": True, "status": status, "event_id": event_id, "amount_cents": cents, "warnings": warnings}


@router.post("/webhooks/plaid")
async def plaid_webhook(request: Request, background: BackgroundTasks):
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BODY:
        return JSONResponse({"ok": False}, status_code=413)
    signed = request.headers.get("plaid-verification")

    def verify_and_handle():
        try:
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            payload = None
        try:
            webhooks.verify(body, signed, get_gateway())
        except webhooks.WebhookVerificationError as e:
            with SessionLocal() as s:
                s.add(WebhookLog(verified=False, rejected_reason=str(e)[:500],
                                 webhook_type=(payload or {}).get("webhook_type"), webhook_code=(payload or {}).get("webhook_code")))
                s.commit()
            log.warning("rejected Plaid webhook: %s", e)
            return None, None, str(e)
        if payload is None:
            return None, None, "body is not a JSON object"
        with SessionLocal() as s:
            s.add(WebhookLog(verified=True, webhook_type=payload.get("webhook_type"), webhook_code=payload.get("webhook_code"),
                             plaid_item_id=payload.get("item_id"), body=payload))
            action = webhooks.handle(s, payload)
            from sqlalchemy import select

            from app.models import PlaidItem

            item = s.scalar(select(PlaidItem).where(PlaidItem.plaid_item_id == payload.get("item_id")))
            s.commit()
            return action, (item.id if item else None), None

    action, item_id, error = await run_in_threadpool(verify_and_handle)
    if error:
        return JSONResponse({"ok": False, "error": "verification failed"}, status_code=401)
    if action == "sync" and item_id:
        background.add_task(jobs.sync_item, item_id, "webhook")
    elif action == "liabilities" and item_id:
        background.add_task(jobs.sync_item, item_id, "webhook", True)
    return {"ok": True}
