"""The 14 required Sprint 1 scenarios, end to end through the services (fake data only)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app import timeutil
from app.models import (
    LedgerEntry,
    MatchDecision,
    PlaidTransaction,
    PlaidTransactionHistory,
    Purchase,
    ReserveTransfer,
    ReviewItem,
    WalletCardAlias,
    WalletEvent,
)
from app.services import ledger, matching, review_actions, transfers
from tests.conftest import txn


def active_purchases(s):
    return s.scalars(select(Purchase).where(Purchase.state == "active")).all()


def open_reviews(s, kind=None):
    q = select(ReviewItem).where(ReviewItem.status == "open")
    if kind:
        q = q.where(ReviewItem.kind == kind)
    return s.scalars(q).all()


def assert_ledger_consistent(s):
    """Every purchase's ledger sum equals what its evidence says it should be."""
    for p in s.scalars(select(Purchase)):
        assert ledger.purchase_sum(s, p.id) == ledger.purchase_target(p), f"purchase {p.id}"


# 1 ---------------------------------------------------------------------------
def test_01_apple_pay_then_pending_then_posted_counted_once(world):
    s = world.s
    world.tap("$23.45", "Blue Bottle Coffee")
    assert world.dash().card_spend_uncovered == 2345

    world.plaid(added=[txn("p1", "23.45", "SQ *BLUE BOTTLE COFFEE", pending=True)])
    p = s.scalar(select(Purchase).where(Purchase.wallet_event_id.is_not(None)))
    assert p.plaid_transaction is not None and p.match_confidence == "high"
    assert world.dash().card_spend_uncovered == 2345

    # Posting: Plaid adds the posted transaction (pointing at the pending one) and removes the pending id.
    world.plaid(added=[txn("t1", "23.45", "SQ *BLUE BOTTLE COFFEE", pending=False, pending_id="p1")], removed=["p1"])
    d = world.dash()
    assert d.card_spend_uncovered == 2345
    assert len(active_purchases(s)) == 1
    rows = s.scalars(select(PlaidTransaction)).all()
    assert len(rows) == 1 and rows[0].plaid_transaction_id == "t1" and rows[0].replaced_pending_transaction_id == "p1"
    assert not rows[0].pending and not rows[0].is_removed
    changes = [h.change for h in s.scalars(select(PlaidTransactionHistory).order_by(PlaidTransactionHistory.id))]
    assert changes == ["added", "posted_replacing_pending", "removal_ignored_superseded"]
    assert d.cards[0].cycle_posted == 2345 and d.cards[0].cycle_provisional == 0
    assert_ledger_consistent(s)


def test_01b_removal_arriving_before_posted_version_is_not_double_counted(world):
    s = world.s
    world.tap("23.45", "Blue Bottle Coffee")
    world.plaid(added=[txn("p1", "23.45", "BLUE BOTTLE COFFEE")])
    world.plaid(removed=["p1"])  # pending dropped first (separate update)
    assert world.dash().card_spend_uncovered == 0
    world.plaid(added=[txn("t1", "23.45", "BLUE BOTTLE COFFEE", pending=False, pending_id="p1")])
    assert world.dash().card_spend_uncovered == 2345
    assert len(active_purchases(s)) == 1
    assert_ledger_consistent(s)


# 2 ---------------------------------------------------------------------------
def test_02_restaurant_tip_matched_and_recorded(world):
    s = world.s
    world.tap("40.00", "Joe's Diner")
    world.plaid(added=[txn("p1", "40.00", "TST* JOES DINER", pfc="FOOD_AND_DRINK_RESTAURANT")])
    world.plaid(added=[txn("t1", "48.00", "TST* JOES DINER", pending=False, pending_id="p1", pfc="FOOD_AND_DRINK_RESTAURANT")],
                removed=["p1"])
    p = s.scalar(select(Purchase).where(Purchase.wallet_event_id.is_not(None)))
    assert p.plaid_transaction.amount_cents == 4800
    assert p.amount_difference_cents == 800
    assert world.dash().card_spend_uncovered == 4800
    assert len(active_purchases(s)) == 1
    assert_ledger_consistent(s)


def test_02b_restaurant_posted_only_with_tip_matches_by_tip_rule(world):
    s = world.s
    world.tap("40.00", "Joe's Diner")
    world.plaid(added=[txn("t1", "48.00", "JOES DINER", pending=False, pfc="FOOD_AND_DRINK_RESTAURANT")])
    p = s.scalar(select(Purchase).where(Purchase.wallet_event_id.is_not(None)))
    assert p.plaid_transaction is not None and p.amount_difference_cents == 800
    d = s.scalar(select(MatchDecision).where(MatchDecision.decision.in_(["auto_linked", "linked_needs_review"])))
    assert "tip" in d.reason
    assert world.dash().card_spend_uncovered == 4800


def test_02c_non_restaurant_amount_mismatch_does_not_match(world):
    s = world.s
    world.tap("40.00", "Best Buy")
    world.plaid(added=[txn("t1", "48.00", "BEST BUY", pending=False, pfc="GENERAL_MERCHANDISE_ELECTRONICS")])
    p = s.scalar(select(Purchase).where(Purchase.wallet_event_id.is_not(None)))
    assert p.plaid_transaction is None
    assert world.dash().card_spend_uncovered == 8800  # both counted: conservative, and the tap goes to review after 5 days


# 3 ---------------------------------------------------------------------------
def test_03_gas_hold_then_final_amount(world):
    s = world.s
    world.tap("100.00", "Shell")
    world.plaid(added=[txn("p1", "100.00", "SHELL OIL 57444", pfc="TRANSPORTATION_GAS")])
    assert world.dash().card_spend_uncovered == 10000
    world.plaid(added=[txn("t1", "42.17", "SHELL OIL 57444", pending=False, pending_id="p1", pfc="TRANSPORTATION_GAS")],
                removed=["p1"])
    p = s.scalar(select(Purchase).where(Purchase.wallet_event_id.is_not(None)))
    assert p.plaid_transaction.amount_cents == 4217
    assert world.dash().card_spend_uncovered == 4217
    assert len(active_purchases(s)) == 1
    assert_ledger_consistent(s)


def test_03b_gas_tap_amount_differs_from_hold_still_matches(world):
    s = world.s
    world.tap("42.17", "Shell")
    world.plaid(added=[txn("p1", "100.00", "SHELL OIL 57444", pfc="TRANSPORTATION_GAS")])
    p = s.scalar(select(Purchase).where(Purchase.wallet_event_id.is_not(None)))
    assert p.plaid_transaction is not None  # matched on merchant + timing
    assert world.dash().card_spend_uncovered == 10000  # the hold is reserved while pending (conservative)
    # Posted version without a pending link (some banks), plus pending removal: re-matched.
    world.plaid(added=[txn("t1", "42.17", "SHELL OIL 57444", pending=False, pfc="TRANSPORTATION_GAS")], removed=["p1"])
    assert world.dash().card_spend_uncovered == 4217
    assert len(active_purchases(s)) == 1
    assert_ledger_consistent(s)


# 4 ---------------------------------------------------------------------------
def test_04_two_identical_purchases_same_day_no_double_matching(world):
    s = world.s
    e1 = world.tap("5.25", "Starbucks")
    timeutil.advance(minutes=20)
    e2 = world.tap("5.25", "Starbucks")
    assert e2.status == "accepted"  # 20 minutes apart: not a duplicate delivery
    world.plaid(added=[txn("t1", "5.25", "STARBUCKS STORE 1234"), txn("t2", "5.25", "STARBUCKS STORE 1234")])

    # No link at all: both taps tie for both transactions.
    linked = s.scalars(select(Purchase).where(Purchase.wallet_event_id.is_not(None), Purchase.plaid_transaction_id.is_not(None))).all()
    assert linked == []
    amb = open_reviews(s, "ambiguous_match")
    assert len(amb) == 2
    d = world.dash()
    assert d.card_spend_uncovered == 4 * 525  # conservative while under review
    assert d.possible_double_count == 2 * 525
    assert any("counted twice" in w for w in d.warnings)

    # I resolve one; the other becomes unambiguous and is linked automatically.
    item = next(i for i in amb if i.plaid_transaction.plaid_transaction_id == "t1")
    review_actions.confirm_match(s, item, wallet_event_id=e1.id)
    s.commit()
    pairs = s.execute(select(Purchase.wallet_event_id, Purchase.plaid_transaction_id).where(
        Purchase.wallet_event_id.is_not(None), Purchase.plaid_transaction_id.is_not(None))).all()
    assert len(pairs) == 2
    assert len({p[0] for p in pairs}) == 2 and len({p[1] for p in pairs}) == 2  # one-to-one
    assert open_reviews(s, "ambiguous_match") == []
    assert world.dash().card_spend_uncovered == 2 * 525
    assert_ledger_consistent(s)


def test_04b_same_amount_distinguished_by_authorization_time(world):
    s = world.s
    e1 = world.tap("5.25", "Starbucks")
    t_e1 = timeutil.now()
    timeutil.advance(hours=3)
    e2 = world.tap("5.25", "Starbucks")
    t_e2 = timeutil.now()
    world.plaid(added=[
        txn("t2", "5.25", "STARBUCKS", authorized_datetime=t_e2.isoformat()),
        txn("t1", "5.25", "STARBUCKS", authorized_datetime=t_e1.isoformat()),
    ])
    by_event = dict(s.execute(select(Purchase.wallet_event_id, Purchase.plaid_transaction_id).where(Purchase.wallet_event_id.is_not(None))).all())
    t1 = s.scalar(select(PlaidTransaction).where(PlaidTransaction.plaid_transaction_id == "t1"))
    t2 = s.scalar(select(PlaidTransaction).where(PlaidTransaction.plaid_transaction_id == "t2"))
    assert by_event == {e1.id: t1.id, e2.id: t2.id}
    assert world.dash().card_spend_uncovered == 1050


# 5 ---------------------------------------------------------------------------
def test_05_duplicate_shortcut_delivery_counted_once(world):
    s = world.s
    a = world.tap("$12.34", "Chipotle")
    timeutil.advance(seconds=3)
    b = world.tap("12.34", "CHIPOTLE")
    assert a.status == "accepted" and b.status == "duplicate" and b.duplicate_of_id == a.id
    c = world.tap("9.99", "Chipotle", id="abc-1")
    timeutil.advance(minutes=10)  # outside the time window, but same client id
    d_ = world.tap("9.99", "Chipotle", id="abc-1")
    assert c.status == "accepted" and d_.status == "duplicate"
    assert world.dash().card_spend_uncovered == 1234 + 999
    assert len(active_purchases(s)) == 2
    assert s.scalar(select(func.count()).select_from(MatchDecision).where(MatchDecision.decision == "duplicate_delivery")) == 2


# 6 ---------------------------------------------------------------------------
def test_06_never_posts_goes_to_review_after_5_days(world):
    s = world.s
    ev = world.tap("18.00", "Corner Bakery")
    timeutil.advance(days=4, hours=23)
    assert matching.mark_missing_events(s) == 0
    timeutil.advance(hours=2)
    assert matching.mark_missing_events(s) == 1
    assert matching.mark_missing_events(s) == 0  # idempotent
    s.commit()
    items = open_reviews(s, "missing_after_5_days")
    assert len(items) == 1 and items[0].wallet_event_id == ev.id
    assert world.dash().card_spend_uncovered == 1800  # still reserved until I decide
    review_actions.dismiss_event(s, items[0])
    s.commit()
    assert world.dash().card_spend_uncovered == 0
    assert_ledger_consistent(s)


# 7 ---------------------------------------------------------------------------
def test_07_online_purchase_seen_only_in_plaid(world):
    s = world.s
    world.plaid(added=[txn("p1", "59.99", "AMAZON.COM*AB12CD", merchant_name="Amazon", pfc="GENERAL_MERCHANDISE_ONLINE_MARKETPLACES")])
    assert world.dash().card_spend_uncovered == 5999
    world.plaid(added=[txn("t1", "59.99", "AMAZON.COM*AB12CD", merchant_name="Amazon", pending=False, pending_id="p1")], removed=["p1"])
    d = world.dash()
    assert d.card_spend_uncovered == 5999
    assert len(active_purchases(s)) == 1
    assert open_reviews(s) == []


def test_07b_transactions_before_tracking_start_are_not_counted(world):
    old = timeutil.today() - timedelta(days=60)
    world.plaid(added=[txn("old", "500.00", "OLD PURCHASE", pending=False, day=old)])
    assert world.dash().card_spend_uncovered == 0


# 8 ---------------------------------------------------------------------------
def test_08_refund_releases_reserved_amount(world):
    s = world.s
    world.plaid(added=[txn("t1", "80.00", "TARGET 00012345", merchant_name="Target", pending=False)])
    assert world.dash().card_spend_uncovered == 8000
    world.plaid(added=[txn("r1", "-80.00", "TARGET RETURN", merchant_name="Target", pending=False)])
    assert world.dash().card_spend_uncovered == 0
    refund = s.scalar(select(LedgerEntry).where(LedgerEntry.entry_type == "refund"))
    assert refund.amount_cents == -8000
    # A card *payment* is not a refund and must not reduce card spend.
    world.plaid(added=[txn("pay1", "-500.00", "PAYMENT THANK YOU", pending=False, pfc="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")])
    world.plaid(added=[txn("t2", "25.00", "CVS PHARMACY", pending=False)])
    assert world.dash().card_spend_uncovered == 2500
    assert_ledger_consistent(s)


# 9 ---------------------------------------------------------------------------
def test_09_pending_removed_without_replacement_releases_and_records(world):
    s = world.s
    world.plaid(added=[txn("p1", "75.00", "HERTZ RENT A CAR", pfc="TRAVEL_RENTAL_CARS")])
    ev = world.tap("12.00", "Corner Cafe")
    world.plaid(added=[txn("p2", "12.00", "CORNER CAFE")])
    assert world.dash().card_spend_uncovered == 8700
    world.plaid(removed=["p1", "p2"])
    assert world.dash().card_spend_uncovered == 0
    releases = s.scalars(select(LedgerEntry).where(LedgerEntry.entry_type == "purchase_release")).all()
    assert sorted(r.amount_cents for r in releases) == [-7500, -1200]
    assert all("removed by Plaid" in r.reason for r in releases)
    assert s.scalar(select(func.count()).select_from(PlaidTransactionHistory).where(PlaidTransactionHistory.change == "removed")) == 2
    # The tap isn't sent to the 'missing' review: Plaid saw it and then dropped it.
    timeutil.advance(days=6)
    assert matching.mark_missing_events(s) == 0
    assert ev.id
    assert_ledger_consistent(s)


# 10 --------------------------------------------------------------------------
def test_10_reserve_transfer_in_transit_not_double_counted(world):
    s = world.s
    world.balance("acct_checking", "2000.00")
    world.balance("acct_reserve", "0.00")
    world.plaid(added=[txn("t1", "300.00", "COSTCO WHSE", pending=False)])
    d = world.dash()
    assert (d.verified_cash, d.card_spend_uncovered, d.safe_to_spend, d.suggested_transfer) == (200000, 30000, 170000, 30000)

    # I move $300 myself and click "I moved $300".
    timeutil.advance(minutes=5)
    tr = transfers.create(s, 30000)
    s.commit()
    d = world.dash()
    assert tr.status == "pending"
    assert (d.card_spend_uncovered, d.safe_to_spend, d.suggested_transfer, d.pending_transfer_total) == (30000, 170000, 0, 30000)

    # Plaid sees the outflow on checking, but the balance we hold is older: still not counted as gone.
    timeutil.advance(minutes=30)
    world.plaid(added=[
        txn("c1", "300.00", "ONLINE TRANSFER TO RESERVE", account="acct_checking", pfc="TRANSFER_OUT_ACCOUNT_TRANSFER"),
        txn("r1", "-300.00", "ONLINE TRANSFER FROM CHECKING", account="acct_reserve", pfc="TRANSFER_IN_ACCOUNT_TRANSFER"),
    ])
    transfers.reconcile_transfers(s)
    s.commit()
    assert tr.status == "pending"
    assert world.dash().safe_to_spend == 170000

    # Fresh balances reflect the transfer: verified cash drops by $300 and card spend drops by $300.
    timeutil.advance(minutes=1)
    world.balance("acct_checking", "1700.00")
    world.balance("acct_reserve", "300.00")
    transfers.reconcile_transfers(s)
    s.commit()
    d = world.dash()
    assert tr.status == "left_checking"
    assert (d.verified_cash, d.card_spend_uncovered, d.safe_to_spend, d.suggested_transfer) == (170000, 0, 170000, 0)

    # Both sides post.
    timeutil.advance(days=1)
    world.plaid(added=[
        txn("c1p", "300.00", "ONLINE TRANSFER TO RESERVE", account="acct_checking", pending=False, pending_id="c1"),
        txn("r1p", "-300.00", "ONLINE TRANSFER FROM CHECKING", account="acct_reserve", pending=False, pending_id="r1"),
    ], removed=["c1", "r1"])
    world.balance("acct_checking", "1700.00")
    world.item.last_sync_completed_at = timeutil.now()  # card data fresh too (else a holdback applies)
    transfers.reconcile_transfers(s)
    s.commit()
    d = world.dash()
    assert tr.status == "confirmed"
    assert (d.card_spend_uncovered, d.safe_to_spend) == (0, 170000)
    entries = s.scalars(select(LedgerEntry).where(LedgerEntry.reserve_transfer_id == tr.id)).all()
    assert [e.amount_cents for e in entries] == [-30000]  # exactly one entry, never doubled


# 11 --------------------------------------------------------------------------
def test_11_stale_balance_applies_holdback_and_warns(world):
    world.balance("acct_checking", "1000.00")
    d = world.dash()
    assert d.holdback == 0 and d.safe_to_spend == 100000
    timeutil.advance(hours=13)
    world.item.last_sync_completed_at = timeutil.now()
    world.s.commit()
    d = world.dash()
    assert d.holdback == 25000
    assert d.safe_to_spend == 100000 - 25000
    assert any("Checking balance is from 13 hours ago" in r for r in d.holdback_reasons)
    assert any("cushion" in w for w in d.warnings)


# 12 --------------------------------------------------------------------------
def test_12_unmapped_wallet_card_counts_and_goes_to_review(world):
    s = world.s
    world.balance("acct_checking", "1000.00")
    ev = world.tap("64.10", "Trader Joe's", card="Mystery Visa ••4242")
    d = world.dash()
    assert d.card_spend_uncovered == 6410
    assert d.safe_to_spend == 100000 - 6410
    assert d.unmapped_cycle == 6410
    items = open_reviews(s, "unmapped_card")
    assert len(items) == 1 and items[0].wallet_event_id == ev.id
    # I map the card; the review item resolves and matching runs.
    world.plaid(added=[txn("t1", "64.10", "TRADER JOE S #552", account="acct_card_b", pending=False)])
    assert world.dash().card_spend_uncovered == 2 * 6410  # not matchable yet (unmapped)
    alias = s.scalar(select(WalletCardAlias).where(WalletCardAlias.normalized_name == "MYSTERY VISA 4242"))
    alias.card_id = world.card_b.id
    matching.reassign_alias(s, alias)
    s.commit()
    assert open_reviews(s, "unmapped_card") == []
    assert world.dash().card_spend_uncovered == 6410
    assert_ledger_consistent(s)


# 13 is in test_api.py (it goes through the HTTP webhook endpoint).


# 14 --------------------------------------------------------------------------
def test_14_ledger_integrity_dashboard_equals_sum_of_entries(world):
    s = world.s
    world.balance("acct_checking", "3000.00")
    world.tap("40.00", "Joe's Diner")
    world.plaid(added=[txn("t1", "48.00", "JOES DINER", pending=False, pfc="FOOD_AND_DRINK_RESTAURANT")])
    world.tap("23.45", "Blue Bottle")
    world.tap("9.00", "Somewhere", card="Unknown Card")
    world.plaid(added=[txn("p9", "75.00", "HERTZ", pfc="TRAVEL_RENTAL_CARS"), txn("t8", "80.00", "TARGET", pending=False)])
    world.plaid(added=[txn("r8", "-80.00", "TARGET", pending=False)], removed=["p9"])
    transfers.create(s, 1000)
    s.commit()

    d = world.dash()
    raw_sum = s.execute(text("SELECT COALESCE(SUM(amount_cents),0) FROM ledger_entries")).scalar()
    assert d.ledger_total == raw_sum
    assert d.card_spend_uncovered == max(0, raw_sum)
    # Per-card + unmapped purchase totals tie back to the purchase entries.
    purchase_sum = s.execute(text("SELECT COALESCE(SUM(amount_cents),0) FROM ledger_entries WHERE purchase_id IS NOT NULL")).scalar()
    by_card = sum(c.uncovered_ledger for c in d.cards) + d.unmapped_cycle
    assert by_card == purchase_sum
    assert d.safe_to_spend == d.verified_cash - d.card_spend_uncovered - d.bills_total - d.minimum_buffer - d.holdback
    assert_ledger_consistent(s)
    assert raw_sum == 4800 + 2345 + 900  # tip-adjusted diner + coffee + unmapped; Hertz and Target net to 0

    # The ledger is append-only, enforced by the database.
    s.commit()
    with pytest.raises(DBAPIError):
        s.execute(text("UPDATE ledger_entries SET amount_cents = 1"))
    s.rollback()
    with pytest.raises(DBAPIError):
        s.execute(text("DELETE FROM ledger_entries"))
    s.rollback()
