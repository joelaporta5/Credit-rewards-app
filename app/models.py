"""Database schema.

Design notes (see DECISIONS.md for the reasoning):
- Money is always integer cents (BigInteger).
- `ledger_entries`, `match_decisions`, `audit_log`, `balance_snapshots`,
  `card_liability_snapshots` and `plaid_transaction_history` are append-only.
  The ledger additionally has a database trigger that rejects UPDATE/DELETE.
- Cards and merchants have their own tables so a rewards engine / Chrome
  extension can attach to them later.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.timeutil import now


def _ts(**kw):
    return mapped_column(DateTime(timezone=True), **kw)


# ---------------------------------------------------------------------------
# Plaid connections, accounts, cards
# ---------------------------------------------------------------------------


class PlaidItem(Base):
    """One Plaid 'Item' = one login at one bank. Holds the encrypted access token."""

    __tablename__ = "plaid_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plaid_item_id: Mapped[str] = mapped_column(String(128), unique=True)
    plaid_env: Mapped[str] = mapped_column(String(16))  # sandbox | production
    institution_id: Mapped[str | None] = mapped_column(String(64))
    institution_name: Mapped[str | None] = mapped_column(String(255))
    access_token_encrypted: Mapped[str] = mapped_column(Text)  # Fernet ciphertext, never plaintext
    transactions_cursor: Mapped[str | None] = mapped_column(Text)
    # active | needs_reauth | expiring | revoked | error | removed
    status: Mapped[str] = mapped_column(String(32), default="active")
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    status_changed_at: Mapped[datetime | None] = _ts()
    new_accounts_available: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_expiration_time: Mapped[datetime | None] = _ts()
    institution_last_successful_update: Mapped[datetime | None] = _ts()
    last_sync_started_at: Mapped[datetime | None] = _ts()
    last_sync_completed_at: Mapped[datetime | None] = _ts()
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    last_webhook_at: Mapped[datetime | None] = _ts()
    balances_fetched_at: Mapped[datetime | None] = _ts()
    liabilities_fetched_at: Mapped[datetime | None] = _ts()
    transactions_update_status: Mapped[str | None] = mapped_column(String(64))
    sync_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _ts(default=now)

    accounts: Mapped[list["Account"]] = relationship(back_populates="item")


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("plaid_items.id"))
    plaid_account_id: Mapped[str] = mapped_column(String(128), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    official_name: Mapped[str | None] = mapped_column(String(255))
    mask: Mapped[str | None] = mapped_column(String(8))
    type: Mapped[str] = mapped_column(String(32))  # depository | credit | loan | investment | other
    subtype: Mapped[str | None] = mapped_column(String(64))
    # primary_checking | reserve | credit_card | ignored
    role: Mapped[str] = mapped_column(String(32), default="ignored")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _ts(default=now)

    item: Mapped[PlaidItem] = relationship(back_populates="accounts")
    card: Mapped["Card | None"] = relationship(back_populates="account", uselist=False)

    __table_args__ = (
        # At most one primary checking account and one reserve account.
        Index("uq_one_primary_checking", "role", unique=True, postgresql_where=text("role = 'primary_checking'")),
        Index("uq_one_reserve", "role", unique=True, postgresql_where=text("role = 'reserve'")),
    )

    @property
    def label(self) -> str:
        inst = self.item.institution_name if self.item else None
        base = f"{inst} · {self.name}" if inst else self.name
        return f"{base} ••{self.mask}" if self.mask else base


class Card(Base):
    """A credit card. Separate from Account so rewards rules can attach here later."""

    __tablename__ = "cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), unique=True)
    nickname: Mapped[str | None] = mapped_column(String(255))
    issuer: Mapped[str | None] = mapped_column(String(255))
    network: Mapped[str | None] = mapped_column(String(32))
    # Entered by me; Plaid doesn't provide the next statement close date.
    manual_next_statement_close_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = _ts(default=now)

    account: Mapped[Account] = relationship(back_populates="card")

    @property
    def display_name(self) -> str:
        return self.nickname or (self.account.label if self.account else f"Card #{self.id}")


class WalletCardAlias(Base):
    """Maps a card name as shown in Apple Wallet to a card."""

    __tablename__ = "wallet_card_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    normalized_name: Mapped[str] = mapped_column(String(255), unique=True)
    display_name: Mapped[str] = mapped_column(String(255))
    card_id: Mapped[int | None] = mapped_column(ForeignKey("cards.id"))
    first_seen_at: Mapped[datetime] = _ts(default=now)
    updated_at: Mapped[datetime] = _ts(default=now)

    card: Mapped[Card | None] = relationship()


class BalanceSnapshot(Base):
    """Append-only: every balance Plaid reports, with when we got it."""

    __tablename__ = "balance_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    fetched_at: Mapped[datetime] = _ts(default=now)
    # The institution's own timestamp when provided, else fetched_at.
    as_of: Mapped[datetime] = _ts()
    available_cents: Mapped[int | None] = mapped_column(BigInteger)
    current_cents: Mapped[int | None] = mapped_column(BigInteger)
    limit_cents: Mapped[int | None] = mapped_column(BigInteger)
    iso_currency_code: Mapped[str | None] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(32))  # accounts_balance_get | liabilities_get | test

    account: Mapped[Account] = relationship()


class CardLiabilitySnapshot(Base):
    """Append-only: statement data from /liabilities/get."""

    __tablename__ = "card_liability_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id"), index=True)
    fetched_at: Mapped[datetime] = _ts(default=now)
    last_statement_balance_cents: Mapped[int | None] = mapped_column(BigInteger)
    last_statement_issue_date: Mapped[date | None] = mapped_column(Date)
    next_payment_due_date: Mapped[date | None] = mapped_column(Date)
    minimum_payment_amount_cents: Mapped[int | None] = mapped_column(BigInteger)
    last_payment_amount_cents: Mapped[int | None] = mapped_column(BigInteger)
    last_payment_date: Mapped[date | None] = mapped_column(Date)
    is_overdue: Mapped[bool | None] = mapped_column(Boolean)


# ---------------------------------------------------------------------------
# Merchants and transactions
# ---------------------------------------------------------------------------


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    normalized_name: Mapped[str] = mapped_column(String(255), unique=True)
    display_name: Mapped[str] = mapped_column(String(255))
    # restaurant | gas | lodging | car_rental | other  (drives match amount tolerance)
    category: Mapped[str | None] = mapped_column(String(32))
    category_source: Mapped[str | None] = mapped_column(String(32))  # plaid_pfc | mcc | keyword | manual
    plaid_merchant_entity_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = _ts(default=now)


class PlaidTransaction(Base):
    """Current state of a Plaid transaction. When a posted transaction replaces a pending
    one (pending_transaction_id), this same row is updated in place; the history table
    keeps every version."""

    __tablename__ = "plaid_transactions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    plaid_transaction_id: Mapped[str] = mapped_column(String(128), unique=True)
    # Set when a posted transaction replaced a pending one: the pending transaction_id.
    replaced_pending_transaction_id: Mapped[str | None] = mapped_column(String(128), index=True)
    pending: Mapped[bool] = mapped_column(Boolean)
    amount_cents: Mapped[int] = mapped_column(BigInteger)  # Plaid sign: + = money out
    iso_currency_code: Mapped[str | None] = mapped_column(String(8))
    txn_date: Mapped[date] = mapped_column(Date)
    authorized_date: Mapped[date | None] = mapped_column(Date)
    authorized_datetime: Mapped[datetime | None] = _ts()
    posted_datetime: Mapped[datetime | None] = _ts()
    name: Mapped[str | None] = mapped_column(String(512))
    merchant_name: Mapped[str | None] = mapped_column(String(512))
    merchant_entity_id: Mapped[str | None] = mapped_column(String(128))
    merchant_category_code: Mapped[str | None] = mapped_column(String(8))
    pfc_primary: Mapped[str | None] = mapped_column(String(64))
    pfc_detailed: Mapped[str | None] = mapped_column(String(128))
    payment_channel: Mapped[str | None] = mapped_column(String(32))
    merchant_id: Mapped[int | None] = mapped_column(ForeignKey("merchants.id"))
    is_removed: Mapped[bool] = mapped_column(Boolean, default=False)
    removed_at: Mapped[datetime | None] = _ts()
    first_seen_at: Mapped[datetime] = _ts(default=now)
    updated_at: Mapped[datetime] = _ts(default=now)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    account: Mapped[Account] = relationship()
    merchant: Mapped[Merchant | None] = relationship()

    @property
    def effective_date(self) -> date:
        """Day the purchase happened (authorized date when known)."""
        return self.authorized_date or self.txn_date

    @property
    def display_name(self) -> str:
        return self.merchant_name or self.name or "(no description)"


class PlaidTransactionHistory(Base):
    """Append-only: every change Plaid reported for a transaction row."""

    __tablename__ = "plaid_transaction_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    plaid_transaction_row_id: Mapped[int] = mapped_column(ForeignKey("plaid_transactions.id"), index=True)
    at: Mapped[datetime] = _ts(default=now)
    # added | modified | removed | posted_replacing_pending | revived_by_posted | removal_ignored_superseded
    change: Mapped[str] = mapped_column(String(64))
    plaid_transaction_id: Mapped[str] = mapped_column(String(128))
    pending: Mapped[bool | None] = mapped_column(Boolean)
    amount_cents: Mapped[int | None] = mapped_column(BigInteger)
    sync_run_id: Mapped[int | None] = mapped_column(ForeignKey("sync_runs.id"))


# ---------------------------------------------------------------------------
# Apple Pay (Wallet) events
# ---------------------------------------------------------------------------


class WalletEvent(Base):
    __tablename__ = "wallet_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    received_at: Mapped[datetime] = _ts(default=now, index=True)
    client_event_id: Mapped[str | None] = mapped_column(String(128), index=True)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    amount_cents: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str | None] = mapped_column(String(8))
    merchant_raw: Mapped[str | None] = mapped_column(String(512))
    merchant_id: Mapped[int | None] = mapped_column(ForeignKey("merchants.id"))
    card_name_raw: Mapped[str | None] = mapped_column(String(255))
    card_alias_id: Mapped[int | None] = mapped_column(ForeignKey("wallet_card_aliases.id"))
    event_at: Mapped[datetime] = _ts()  # when I tapped (from payload, else received_at)
    event_at_source: Mapped[str] = mapped_column(String(16))  # payload | received
    parse_warnings: Mapped[list[str] | None] = mapped_column(JSONB)
    dedupe_key: Mapped[str] = mapped_column(String(64), index=True)
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("wallet_events.id"))
    status: Mapped[str] = mapped_column(String(16), default="accepted")  # accepted | duplicate
    processed_at: Mapped[datetime | None] = _ts()

    merchant: Mapped[Merchant | None] = relationship()
    card_alias: Mapped[WalletCardAlias | None] = relationship()

    @property
    def card_id(self) -> int | None:
        return self.card_alias.card_id if self.card_alias else None


# ---------------------------------------------------------------------------
# Purchases, matching, review
# ---------------------------------------------------------------------------


class Purchase(Base):
    """One real-world card charge (or refund). Evidence: a wallet event and/or a Plaid
    transaction. Its contribution to 'card spend not yet covered' is the sum of its
    ledger entries; the ledger is adjusted (never edited) whenever the evidence changes."""

    __tablename__ = "purchases"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    card_id: Mapped[int | None] = mapped_column(ForeignKey("cards.id"), index=True)
    merchant_id: Mapped[int | None] = mapped_column(ForeignKey("merchants.id"))
    wallet_event_id: Mapped[int | None] = mapped_column(ForeignKey("wallet_events.id"), unique=True)
    plaid_transaction_id: Mapped[int | None] = mapped_column(ForeignKey("plaid_transactions.id"), unique=True)
    kind: Mapped[str] = mapped_column(String(16), default="purchase")  # purchase | refund
    # active | dismissed (I said it never happened / refunded) | duplicate | merged | excluded
    state: Mapped[str] = mapped_column(String(16), default="active")
    state_reason: Mapped[str | None] = mapped_column(Text)
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("purchases.id"))
    match_confidence: Mapped[str | None] = mapped_column(String(16))  # high | medium | confirmed
    match_score: Mapped[float | None] = mapped_column(Float)
    # Plaid amount minus tapped amount, when both exist (e.g. a tip).
    amount_difference_cents: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _ts(default=now)
    updated_at: Mapped[datetime] = _ts(default=now)

    card: Mapped[Card | None] = relationship()
    merchant: Mapped[Merchant | None] = relationship()
    wallet_event: Mapped[WalletEvent | None] = relationship()
    plaid_transaction: Mapped[PlaidTransaction | None] = relationship()


class MatchDecision(Base):
    """Append-only audit log of every matching decision and why it was made."""

    __tablename__ = "match_decisions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = _ts(default=now)
    wallet_event_id: Mapped[int | None] = mapped_column(ForeignKey("wallet_events.id"), index=True)
    plaid_transaction_id: Mapped[int | None] = mapped_column(ForeignKey("plaid_transactions.id"), index=True)
    # auto_linked | linked_needs_review | ambiguous | low_confidence | confirmed_by_user |
    # rejected_by_user | unlinked | marked_duplicate | missing_after_5_days | dismissed_by_user | kept_by_user
    decision: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[str | None] = mapped_column(String(16))
    score: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    actor: Mapped[str] = mapped_column(String(16), default="system")  # system | user


class ReviewItem(Base):
    __tablename__ = "review_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # medium_match | ambiguous_match | low_confidence_match | unmapped_card |
    # missing_after_5_days | amount_problem
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | resolved
    wallet_event_id: Mapped[int | None] = mapped_column(ForeignKey("wallet_events.id"), index=True)
    plaid_transaction_id: Mapped[int | None] = mapped_column(ForeignKey("plaid_transactions.id"), index=True)
    summary: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _ts(default=now)
    resolved_at: Mapped[datetime | None] = _ts()
    resolution: Mapped[str | None] = mapped_column(String(32))
    resolution_note: Mapped[str | None] = mapped_column(Text)

    wallet_event: Mapped[WalletEvent | None] = relationship()
    plaid_transaction: Mapped[PlaidTransaction | None] = relationship()


# ---------------------------------------------------------------------------
# Ledger, reserve transfers, bills, settings
# ---------------------------------------------------------------------------


class LedgerEntry(Base):
    """Append-only ledger of 'card spend not yet covered'. A DB trigger forbids UPDATE/DELETE.

    Positive = more card spend to cover; negative = released or covered."""

    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = _ts(default=now, index=True)
    # purchase_provisional | purchase_plaid | purchase_adjustment | purchase_release | refund |
    # reserve_transfer_left_checking | reserve_transfer_reversed | opening_balance | manual_adjustment
    entry_type: Mapped[str] = mapped_column(String(40))
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    purchase_id: Mapped[int | None] = mapped_column(ForeignKey("purchases.id"), index=True)
    reserve_transfer_id: Mapped[int | None] = mapped_column(ForeignKey("reserve_transfers.id"), index=True)
    card_id: Mapped[int | None] = mapped_column(ForeignKey("cards.id"))
    reason: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (CheckConstraint("amount_cents <> 0", name="ck_ledger_nonzero"),)


class ReserveTransfer(Base):
    """A transfer I made myself (checking -> reserve). The app never moves money."""

    __tablename__ = "reserve_transfers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _ts(default=now)
    note: Mapped[str | None] = mapped_column(Text)
    # pending | left_checking | confirmed | cancelled
    status: Mapped[str] = mapped_column(String(16), default="pending")
    checking_txn_id: Mapped[int | None] = mapped_column(ForeignKey("plaid_transactions.id"))
    reserve_txn_id: Mapped[int | None] = mapped_column(ForeignKey("plaid_transactions.id"))
    manually_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    left_checking_at: Mapped[datetime | None] = _ts()
    confirmed_at: Mapped[datetime | None] = _ts()
    cancelled_at: Mapped[datetime | None] = _ts()

    checking_txn: Mapped[PlaidTransaction | None] = relationship(foreign_keys=[checking_txn_id])
    reserve_txn: Mapped[PlaidTransaction | None] = relationship(foreign_keys=[reserve_txn_id])

    __table_args__ = (CheckConstraint("amount_cents > 0", name="ck_transfer_positive"),)


class Bill(Base):
    __tablename__ = "bills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    due_date: Mapped[date] = mapped_column(Date)  # first (or only) due date
    recurrence: Mapped[str] = mapped_column(String(16), default="none")  # none | weekly | biweekly | monthly | yearly
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _ts(default=now)

    __table_args__ = (CheckConstraint("amount_cents > 0", name="ck_bill_positive"),)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = _ts(default=now)


class AuditLog(Base):
    """Append-only log of things I did (settings changes, mappings, review actions)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = _ts(default=now)
    actor: Mapped[str] = mapped_column(String(16), default="user")
    action: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


# ---------------------------------------------------------------------------
# Operational logs
# ---------------------------------------------------------------------------


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("plaid_items.id"), index=True)
    trigger: Mapped[str] = mapped_column(String(32))  # webhook | schedule | manual | link | refresh
    started_at: Mapped[datetime] = _ts(default=now)
    finished_at: Mapped[datetime | None] = _ts()
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | ok | error
    added: Mapped[int] = mapped_column(Integer, default=0)
    modified: Mapped[int] = mapped_column(Integer, default=0)
    removed: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)


class WebhookLog(Base):
    __tablename__ = "webhook_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    received_at: Mapped[datetime] = _ts(default=now)
    verified: Mapped[bool] = mapped_column(Boolean)
    rejected_reason: Mapped[str | None] = mapped_column(Text)
    webhook_type: Mapped[str | None] = mapped_column(String(64))
    webhook_code: Mapped[str | None] = mapped_column(String(64))
    plaid_item_id: Mapped[str | None] = mapped_column(String(128))
    body: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
