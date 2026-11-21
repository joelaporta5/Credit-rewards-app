"""Plaid Link (connect / reconnect), refresh, and webhook URL maintenance."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import jobs
from app.config import get_settings
from app.crypto import decrypt_token
from app.db import get_db
from app.models import AuditLog, PlaidItem
from app.security import require_admin
from app.services import plaid_sync
from app.services.plaid_gateway import PlaidError, get_gateway
from app.templating import templates

log = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_admin)])

TRIAL_ITEM_LIMIT = 10


class LinkTokenIn(BaseModel):
    item_id: int | None = None
    account_selection: bool = False


class ExchangeIn(BaseModel):
    public_token: str
    institution_id: str | None = None
    institution_name: str | None = None
    confirm_duplicate: bool = False


@router.get("/connect")
def connect_page(request: Request, db: Session = Depends(get_db)):
    items = db.scalars(select(PlaidItem).order_by(PlaidItem.id)).all()
    env = get_settings().plaid_env
    prod_count = sum(1 for i in items if i.plaid_env == "production")
    return templates.TemplateResponse(request, "connect.html", {
        "items": items, "plaid_env": env, "prod_count": prod_count, "trial_limit": TRIAL_ITEM_LIMIT,
        "configured": bool(get_settings().plaid_client_id and get_settings().plaid_secret),
    })


@router.post("/plaid/link-token")
def link_token(body: LinkTokenIn, db: Session = Depends(get_db)):
    s = get_settings()
    access_token = None
    if body.item_id is not None:
        item = db.get(PlaidItem, body.item_id)
        if item is None:
            raise HTTPException(404, "Connection not found")
        access_token = decrypt_token(item.access_token_encrypted)
    try:
        resp = get_gateway().link_token_create(
            webhook=s.webhook_url, redirect_uri=s.plaid_redirect_uri or None, access_token=access_token,
            account_selection=body.account_selection,
        )
    except PlaidError as e:
        raise HTTPException(502, f"Plaid error {e.error_code}: {e.display_message or e.message}") from None
    return {"link_token": resp["link_token"], "expiration": resp.get("expiration")}


@router.post("/plaid/exchange")
def exchange(body: ExchangeIn, background: BackgroundTasks, db: Session = Depends(get_db)):
    dup = plaid_sync.find_duplicate_institution(db, body.institution_id)
    if dup is not None and not body.confirm_duplicate:
        raise HTTPException(409, f"You already connected {dup.institution_name}. Connecting it again creates a second "
                                 f"connection (and, on the Plaid Trial plan, uses another of your {TRIAL_ITEM_LIMIT} slots).")
    try:
        item = plaid_sync.connect_item(db, get_gateway(), body.public_token, body.institution_id, body.institution_name)
    except PlaidError as e:
        raise HTTPException(502, f"Plaid error {e.error_code}: {e.display_message or e.message}") from None
    db.commit()
    background.add_task(jobs.sync_item, item.id, "link", True, True)
    return {"ok": True, "item_id": item.id, "institution": item.institution_name}


@router.post("/plaid/items/{item_id}/reconnected")
def reconnected(item_id: int, background: BackgroundTasks, db: Session = Depends(get_db)):
    """Called after Link update mode succeeds. The access token doesn't change."""
    item = db.get(PlaidItem, item_id)
    if item is None:
        raise HTTPException(404, "Connection not found")
    plaid_sync.set_item_status(db, item, "active")
    item.new_accounts_available = False
    db.add(AuditLog(action="plaid_item_reconnected", details={"item_id": item.id}))
    db.commit()
    background.add_task(jobs.sync_item, item.id, "link", True, True)
    return {"ok": True}


@router.get("/link/oauth-return")
def oauth_return(request: Request):
    """Plaid sends you back here after logging in at an OAuth bank (Chase, etc.)."""
    return templates.TemplateResponse(request, "oauth_return.html", {})


@router.post("/refresh")
def refresh_now(request: Request, db: Session = Depends(get_db)):
    items = db.scalars(select(PlaidItem).where(PlaidItem.status.not_in(["removed", "revoked"]))).all()
    notes = []
    gw = get_gateway()
    for item in items:
        try:
            res, note = plaid_sync.refresh_item(item.id, gw=gw)
            extra = f" (+{res.added} new, {res.modified} changed, {res.removed} removed)" if res.status == "ok" else f" — {res.status}: {'; '.join(res.messages)}"
            notes.append(f"{item.institution_name}: {note}{extra}")
        except Exception as e:  # noqa: BLE001
            log.exception("refresh failed")
            notes.append(f"{item.institution_name}: refresh failed ({type(e).__name__})")
    from urllib.parse import quote

    msg = " | ".join(notes) or "No connected accounts yet."
    return RedirectResponse(f"/?msg={quote(msg[:1500])}", status_code=303)


@router.post("/admin/update-webhooks")
def update_webhooks():
    out = plaid_sync.update_all_webhooks()
    from urllib.parse import quote

    return RedirectResponse(f"/connect?msg={quote(' | '.join(out) or 'No connections')}", status_code=303)


@router.post("/admin/sandbox/test-webhook")
def sandbox_test_webhook(db: Session = Depends(get_db)):
    """Sandbox only: ask Plaid to send a real, signed SYNC_UPDATES_AVAILABLE webhook to this server.
    Check the Activity page a few seconds later: it should show as verified."""
    from urllib.parse import quote

    if get_settings().plaid_env != "sandbox":
        raise HTTPException(400, "Only available in Sandbox")
    out = []
    gw = get_gateway()
    for item in db.scalars(select(PlaidItem).where(PlaidItem.plaid_env == "sandbox", PlaidItem.status != "removed")):
        try:
            gw.sandbox_fire_webhook(decrypt_token(item.access_token_encrypted), "SYNC_UPDATES_AVAILABLE", "TRANSACTIONS")
            out.append(f"{item.institution_name}: webhook requested")
        except PlaidError as e:
            out.append(f"{item.institution_name}: {e.error_code}")
    return RedirectResponse(f"/activity?msg={quote(' | '.join(out) or 'No Sandbox connections')}", status_code=303)
