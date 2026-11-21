"""Server-rendered pages (dashboard, review queue, settings...) — all require the admin key."""

from __future__ import annotations

from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    Account,
    AuditLog,
    Bill,
    Card,
    LedgerEntry,
    MatchDecision,
    PlaidTransaction,
    ReserveTransfer,
    ReviewItem,
    WalletCardAlias,
    WalletEvent,
    WebhookLog,
)
from app.money import fmt, parse_amount
from app.security import require_admin
from app.services import bills as bills_svc
from app.services import matching, review_actions, settings_store, transfers
from app.services.safe_to_spend import compute
from app.templating import templates
from app.timeutil import now, parse_iso_date

router = APIRouter(dependencies=[Depends(require_admin)])


def back(path: str, msg: str | None = None) -> RedirectResponse:
    return RedirectResponse(f"{path}?msg={quote(msg)}" if msg else path, status_code=303)


def _money_or_400(raw: str, allow_zero: bool = False) -> int:
    pa = parse_amount(raw)
    if pa.cents is None or pa.cents < 0 or (pa.cents == 0 and not allow_zero):
        raise HTTPException(400, f"Not a valid amount: {raw!r}")
    return pa.cents


# --- Dashboard -----------------------------------------------------------------


@router.get("/")
def dashboard(request: Request, msg: str | None = None, db: Session = Depends(get_db)):
    d = compute(db)
    db.commit()
    return templates.TemplateResponse(request, "dashboard.html", {"d": d, "msg": msg})


@router.get("/api/dashboard")
def dashboard_json(db: Session = Depends(get_db)):
    d = compute(db)
    db.commit()
    return {
        "generated_at": d.generated_at.isoformat(),
        "safe_to_spend_cents": d.safe_to_spend,
        "guaranteed": False,
        "verified_cash_cents": d.verified_cash,
        "verified_cash_as_of": d.verified_cash_snapshot.as_of.isoformat() if d.verified_cash_snapshot else None,
        "card_spend_not_yet_covered_cents": d.card_spend_uncovered,
        "ledger_total_cents": d.ledger_total,
        "committed_bills_cents": d.bills_total,
        "bills_window_days": d.bills_window_days,
        "minimum_buffer_cents": d.minimum_buffer,
        "uncertainty_holdback_cents": d.holdback,
        "holdback_reasons": d.holdback_reasons,
        "suggested_transfer_cents": d.suggested_transfer,
        "pending_transfer_cents": d.pending_transfer_total,
        "reserve_cash_cents": d.reserve_cash,
        "possible_double_count_cents": d.possible_double_count,
        "review_count": d.review_count,
        "warnings": d.warnings,
    }


# --- Transfers -----------------------------------------------------------------


@router.post("/transfers")
def mark_transfer(amount: str = Form(...), note: str = Form(""), db: Session = Depends(get_db)):
    cents = _money_or_400(amount)
    transfers.create(db, cents, note or None)
    db.commit()
    return back("/", f"Recorded: you moved {fmt(cents)} to the reserve. It will be confirmed when Plaid shows it on both accounts.")


@router.post("/transfers/{tid}/cancel")
def cancel_transfer(tid: int, db: Session = Depends(get_db)):
    tr = db.get(ReserveTransfer, tid) or _404()
    transfers.cancel(db, tr)
    db.commit()
    return back("/", f"Transfer #{tid} cancelled.")


@router.post("/transfers/{tid}/confirm")
def confirm_transfer(tid: int, db: Session = Depends(get_db)):
    tr = db.get(ReserveTransfer, tid) or _404()
    transfers.confirm_manually(db, tr)
    db.commit()
    return back("/", f"Transfer #{tid} marked as confirmed by you.")


def _404():
    raise HTTPException(404, "Not found")


# --- Review queue ----------------------------------------------------------------


@router.get("/review")
def review_page(request: Request, msg: str | None = None, show: str = "open", db: Session = Depends(get_db)):
    q = select(ReviewItem).order_by(desc(ReviewItem.created_at))
    q = q.where(ReviewItem.status == "open") if show == "open" else q.limit(200)
    items = db.scalars(q).all()
    events = {}
    candidates = {}
    for it in items:
        for c in (it.details or {}).get("candidates", []):
            ev = db.get(WalletEvent, c["wallet_event_id"])
            if ev:
                events[ev.id] = ev
        if it.kind == "missing_after_5_days" and it.wallet_event and it.status == "open":
            candidates[it.id] = matching.candidates_for_event(db, it.wallet_event)[:5]
    return templates.TemplateResponse(request, "review.html", {"items": items, "events": events, "candidates": candidates, "msg": msg, "show": show})


@router.post("/review/{rid}/{action}")
def review_action(rid: int, action: str, wallet_event_id: int | None = Form(None), plaid_transaction_id: int | None = Form(None),
                  db: Session = Depends(get_db)):
    item = db.get(ReviewItem, rid) or _404()
    if item.status != "open":
        return back("/review", "That item was already resolved.")
    try:
        if action == "confirm":
            review_actions.confirm_match(db, item, wallet_event_id=wallet_event_id, txn_id=plaid_transaction_id)
        elif action == "reject":
            review_actions.reject_match(db, item)
        elif action == "duplicate":
            review_actions.mark_duplicate(db, item, wallet_event_id=wallet_event_id)
        elif action == "dismiss":
            review_actions.dismiss_event(db, item)
        elif action == "keep":
            review_actions.keep_counting(db, item)
        else:
            raise HTTPException(400, "Unknown action")
    except review_actions.ReviewError as e:
        db.rollback()
        return back("/review", str(e))
    db.commit()
    return back("/review", "Done.")


# --- Accounts, cards and Wallet card mapping ----------------------------------------


@router.get("/accounts")
def accounts_page(request: Request, msg: str | None = None, db: Session = Depends(get_db)):
    accounts = db.scalars(select(Account).order_by(Account.item_id, Account.id)).all()
    cards = db.scalars(select(Card).order_by(Card.id)).all()
    aliases = db.scalars(select(WalletCardAlias).order_by(WalletCardAlias.display_name)).all()
    latest = {a.id: transfers.latest_balance(db, a.id) for a in accounts}
    return templates.TemplateResponse(request, "accounts.html", {"accounts": accounts, "cards": cards, "aliases": aliases,
                                                                 "latest": latest, "msg": msg})


@router.post("/accounts/{aid}/role")
def set_role(aid: int, role: str = Form(...), db: Session = Depends(get_db)):
    acct = db.get(Account, aid) or _404()
    if role not in ("primary_checking", "reserve", "credit_card", "ignored"):
        raise HTTPException(400, "bad role")
    if role in ("primary_checking", "reserve"):
        if acct.type != "depository":
            return back("/accounts", "Only bank (depository) accounts can be checking or reserve.")
        other = db.scalar(select(Account).where(Account.role == role, Account.id != acct.id))
        if other:
            other.role = "ignored"
            db.flush()
    if role == "credit_card" and acct.type != "credit":
        return back("/accounts", "Only credit accounts can be cards.")
    old = acct.role
    acct.role = role
    db.flush()
    if role == "credit_card" and db.scalar(select(Card).where(Card.account_id == acct.id)) is None:
        db.add(Card(account_id=acct.id, issuer=acct.item.institution_name if acct.item else None))
    db.add(AuditLog(action="account_role_changed", details={"account_id": acct.id, "from": old, "to": role}))
    transfers.reconcile_transfers(db)
    db.commit()
    return back("/accounts", f"{acct.name} is now: {role.replace('_', ' ')}.")


@router.post("/cards/{cid}")
def update_card(cid: int, nickname: str = Form(""), next_close: str = Form(""), db: Session = Depends(get_db)):
    card = db.get(Card, cid) or _404()
    card.nickname = nickname.strip() or None
    d = parse_iso_date(next_close.strip()) if next_close.strip() else None
    if next_close.strip() and d is None:
        return back("/accounts", "Use the date picker for the statement close date.")
    card.manual_next_statement_close_date = d
    db.add(AuditLog(action="card_updated", details={"card_id": cid, "nickname": card.nickname, "manual_close": next_close or None}))
    db.commit()
    return back("/accounts", "Card saved.")


@router.post("/wallet-cards/{alias_id}")
def map_wallet_card(alias_id: int, card_id: str = Form(""), db: Session = Depends(get_db)):
    alias = db.get(WalletCardAlias, alias_id) or _404()
    new = int(card_id) if card_id else None
    if new is not None and db.get(Card, new) is None:
        raise HTTPException(400, "unknown card")
    old = alias.card_id
    alias.card_id, alias.updated_at = new, now()
    db.add(AuditLog(action="wallet_card_mapped", details={"alias": alias.display_name, "from": old, "to": new}))
    db.flush()
    matching.reassign_alias(db, alias)
    db.commit()
    return back("/accounts", f"'{alias.display_name}' mapping saved.")


@router.post("/wallet-cards")
def add_wallet_card(name: str = Form(...), card_id: str = Form(""), db: Session = Depends(get_db)):
    from app.services.wallet import get_or_create_alias

    alias = get_or_create_alias(db, name)
    if alias is None:
        return back("/accounts", "Enter the card name exactly as Wallet shows it.")
    alias.card_id = int(card_id) if card_id else None
    db.add(AuditLog(action="wallet_card_mapped", details={"alias": alias.display_name, "to": alias.card_id}))
    db.flush()
    matching.reassign_alias(db, alias)
    db.commit()
    return back("/accounts", f"Added Wallet card '{alias.display_name}'.")


# --- Settings ------------------------------------------------------------------------


@router.get("/settings")
def settings_page(request: Request, msg: str | None = None, db: Session = Depends(get_db)):
    vals = {k: settings_store.get(db, k) for k in settings_store.DEFAULTS}
    vals["tracking_start_date"] = settings_store.tracking_start_date(db).isoformat()
    db.commit()
    return templates.TemplateResponse(request, "settings.html", {"v": vals, "msg": msg})


@router.post("/settings")
def save_settings(
    minimum_buffer: str = Form(...), uncertainty_holdback: str = Form(...), stale_threshold_hours: int = Form(...),
    card_stale_threshold_hours: int = Form(...), bills_window_days: int = Form(...), tracking_start_date: str = Form(...),
    db: Session = Depends(get_db),
):
    d = parse_iso_date(tracking_start_date)
    if d is None:
        return back("/settings", "Tracking start date is not a valid date.")
    if not (1 <= stale_threshold_hours <= 168 and 1 <= card_stale_threshold_hours <= 336 and 0 <= bills_window_days <= 120):
        return back("/settings", "One of the numbers is out of range.")
    settings_store.set_value(db, "minimum_buffer_cents", str(_money_or_400(minimum_buffer, allow_zero=True)))
    settings_store.set_value(db, "uncertainty_holdback_cents", str(_money_or_400(uncertainty_holdback, allow_zero=True)))
    settings_store.set_value(db, "stale_threshold_hours", str(stale_threshold_hours))
    settings_store.set_value(db, "card_stale_threshold_hours", str(card_stale_threshold_hours))
    settings_store.set_value(db, "bills_window_days", str(bills_window_days))
    old_start = settings_store.tracking_start_date(db)
    settings_store.set_value(db, "tracking_start_date", d.isoformat())
    if d < old_start:
        _count_newly_tracked(db, d)
    elif d > old_start:
        db.rollback()
        return back("/settings", "Moving the start date later isn't supported (it would rewrite history). Add a manual "
                                 "adjustment on the Ledger page instead.")
    db.commit()
    return back("/settings", "Settings saved.")


def _count_newly_tracked(db: Session, start: date) -> None:
    from app.services.purchases import ensure_purchase_for_txn

    for t in db.scalars(select(PlaidTransaction).join(Account).where(Account.role == "credit_card", PlaidTransaction.txn_date >= start)):
        ensure_purchase_for_txn(db, t, "Tracking start date moved earlier")


# --- Bills --------------------------------------------------------------------------


@router.get("/bills")
def bills_page(request: Request, msg: str | None = None, db: Session = Depends(get_db)):
    all_bills = db.scalars(select(Bill).where(Bill.active.is_(True)).order_by(Bill.due_date)).all()
    window = settings_store.get_int(db, "bills_window_days")
    from app.timeutil import today

    due = bills_svc.due_within(db, today(), window)
    return templates.TemplateResponse(request, "bills.html", {"bills": all_bills, "due": due, "window": window, "msg": msg,
                                                              "recurrences": bills_svc.RECURRENCES})


@router.post("/bills")
def add_bill(name: str = Form(...), amount: str = Form(...), due_date: str = Form(...), recurrence: str = Form("none"),
             db: Session = Depends(get_db)):
    d = parse_iso_date(due_date)
    if d is None or recurrence not in bills_svc.RECURRENCES or not name.strip():
        return back("/bills", "Please fill in a name, amount, date and repeat option.")
    cents = _money_or_400(amount)
    db.add(Bill(name=name.strip()[:255], amount_cents=cents, due_date=d, recurrence=recurrence))
    db.add(AuditLog(action="bill_added", details={"name": name, "amount_cents": cents, "due": d.isoformat(), "recurrence": recurrence}))
    db.commit()
    return back("/bills", f"Added {name} ({fmt(cents)}).")


@router.post("/bills/{bid}/delete")
def delete_bill(bid: int, db: Session = Depends(get_db)):
    b = db.get(Bill, bid) or _404()
    b.active = False
    db.add(AuditLog(action="bill_removed", details={"bill_id": bid, "name": b.name}))
    db.commit()
    return back("/bills", f"Removed {b.name}.")


# --- Ledger & activity (audit) -------------------------------------------------------


@router.get("/ledger")
def ledger_page(request: Request, msg: str | None = None, db: Session = Depends(get_db)):
    entries = db.scalars(select(LedgerEntry).order_by(LedgerEntry.id)).all()
    running, rows = 0, []
    for e in entries:
        running += e.amount_cents
        rows.append((e, running))
    rows.reverse()
    return templates.TemplateResponse(request, "ledger.html", {"rows": rows[:500], "total": running, "count": len(entries), "msg": msg})


@router.post("/ledger/adjust")
def ledger_adjust(amount: str = Form(...), direction: str = Form(...), reason: str = Form(...), db: Session = Depends(get_db)):
    from app.services import ledger

    cents = _money_or_400(amount)
    if direction not in ("add", "remove") or not reason.strip():
        return back("/ledger", "Choose add/remove and give a reason.")
    signed = cents if direction == "add" else -cents
    ledger.post(db, "manual_adjustment" if direction == "remove" else "opening_balance", signed, f"Manual entry by me: {reason.strip()[:500]}")
    db.add(AuditLog(action="ledger_manual_entry", details={"amount_cents": signed, "reason": reason[:500]}))
    db.commit()
    return back("/ledger", f"Added a ledger entry of {fmt(signed, signed=True)}.")


@router.get("/activity")
def activity_page(request: Request, msg: str | None = None, db: Session = Depends(get_db)):
    events = db.scalars(select(WalletEvent).order_by(desc(WalletEvent.id)).limit(50)).all()
    txns = db.scalars(select(PlaidTransaction).order_by(desc(PlaidTransaction.updated_at)).limit(50)).all()
    decisions = db.scalars(select(MatchDecision).order_by(desc(MatchDecision.id)).limit(100)).all()
    hooks = db.scalars(select(WebhookLog).order_by(desc(WebhookLog.id)).limit(30)).all()
    audit = db.scalars(select(AuditLog).order_by(desc(AuditLog.id)).limit(50)).all()
    counts = {"events": db.scalar(select(func.count()).select_from(WalletEvent)),
              "txns": db.scalar(select(func.count()).select_from(PlaidTransaction))}
    return templates.TemplateResponse(request, "activity.html", {"events": events, "txns": txns, "decisions": decisions,
                                                                 "hooks": hooks, "audit": audit, "counts": counts, "msg": msg})
