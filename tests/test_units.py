"""Unit tests for parsing, normalization and date helpers."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app import timeutil
from app.models import Bill, Card, CardLiabilitySnapshot
from app.money import fmt, parse_amount, plaid_amount_to_cents
from app.services.bills import occurrences
from app.services.matching import amount_rule
from app.services.merchants import clean_merchant, similarity
from app.services.safe_to_spend import estimate_next_close
from app.services.wallet import parse_timestamp


@pytest.mark.parametrize("raw,cents", [
    ("$12.34", 1234), ("12.34", 1234), ("12,34", 1234), ("1,234.56", 123456), ("1.234,56", 123456),
    ("$1,234", 123400), ("USD 5", 500), ("5.00 USD", 500), ("-$5.00", -500), ("(5.00)", -500), (" $ 7.5 ", 750),
    (12.34, 1234), (7, 700), ("0.1", 10), ("1 234,50", 123450), ("$0.99", 99), ("12.", 1200),
])
def test_parse_amount_variants(raw, cents):
    assert parse_amount(raw).cents == cents


@pytest.mark.parametrize("raw", [None, "", "abc", "$", "12.34.5x", True, "1,23,4"])
def test_parse_amount_rejects_garbage(raw):
    r = parse_amount(raw)
    assert r.cents is None and r.warnings


def test_parse_amount_flags_non_usd():
    r = parse_amount("€12,34")
    assert r.cents == 1234 and r.currency == "EUR" and any("not USD" in w for w in r.warnings)


def test_plaid_amount_uses_decimal_not_float_math():
    assert plaid_amount_to_cents(0.1 + 0.2) == 30  # 0.30000000000000004 -> 30 cents
    assert plaid_amount_to_cents(1234.5) == 123450
    assert plaid_amount_to_cents(-42.17) == -4217
    assert plaid_amount_to_cents(None) is None


def test_fmt():
    assert fmt(123456) == "$1,234.56" and fmt(-5) == "-$0.05" and fmt(800, signed=True) == "+$8.00"


@pytest.mark.parametrize("raw,clean", [
    ("SQ *BLUE BOTTLE COFFEE", "BLUE BOTTLE COFFEE"), ("TST* Joe's Diner", "JOES DINER"),
    ("PAYPAL *ETSY INC", "ETSY"), ("STARBUCKS STORE #12345", "STARBUCKS"), ("SHELL OIL 57444", "SHELL OIL"),
])
def test_clean_merchant(raw, clean):
    assert clean_merchant(raw) == clean


def test_similarity_tolerates_prefixes_and_noise():
    assert similarity("Blue Bottle Coffee", "SQ *BLUE BOTTLE COFFEE") == 1.0
    assert similarity("Joe's Diner", "TST* JOES DINER NEW YORK NY") > 0.85
    assert similarity("Shell", "SHELL OIL 57444") > 0.9
    assert similarity("Starbucks", "Home Depot") < 0.55


def test_amount_rules():
    assert amount_rule("other", 4000, 4001) == ("exact", 1.0)
    assert amount_rule("other", 4000, 4002) is None
    assert amount_rule("restaurant", 4000, 5400)[0] == "tip"  # +35%
    assert amount_rule("restaurant", 4000, 5500) is None  # +37.5%
    assert amount_rule("restaurant", 4000, 3000) is None  # posted lower than tapped
    assert amount_rule("gas", 10000, 4217)[0] == "hold"
    assert amount_rule("lodging", 20000, 51234)[0] == "hold"
    assert amount_rule("car_rental", 30000, 18000)[0] == "hold"


def test_timestamp_parsing():
    rec = datetime(2026, 11, 10, 17, 0, tzinfo=timezone.utc)
    assert parse_timestamp("2026-11-10T11:58:00-05:00", rec)[0] == datetime(2026, 11, 10, 16, 58, tzinfo=timezone.utc)
    assert parse_timestamp("Nov 10, 2026 at 11:58 AM", rec)[1] == "payload"
    assert parse_timestamp(None, rec) == (rec, "received", ["timestamp missing; used time received"])
    assert parse_timestamp("yesterday-ish", rec)[1] == "received"
    assert parse_timestamp("2030-01-01T00:00:00Z", rec)[1] == "received"  # implausible future
    assert parse_timestamp(rec.timestamp(), rec)[0] == rec


def test_bill_occurrences():
    b = Bill(name="Rent", amount_cents=100, due_date=date(2026, 1, 31), recurrence="monthly")
    assert occurrences(b, date(2026, 2, 1), date(2026, 3, 31)) == [date(2026, 2, 28), date(2026, 3, 31)]
    one = Bill(name="Car", amount_cents=100, due_date=date(2026, 11, 15), recurrence="none")
    assert occurrences(one, date(2026, 11, 10), date(2026, 11, 20)) == [date(2026, 11, 15)]
    assert occurrences(one, date(2026, 11, 16), date(2026, 11, 20)) == []
    wk = Bill(name="Gym", amount_cents=100, due_date=date(2026, 11, 2), recurrence="biweekly")
    assert occurrences(wk, date(2026, 11, 10), date(2026, 12, 10)) == [date(2026, 11, 16), date(2026, 11, 30)]


def test_statement_close_estimate_and_manual_override():
    liab = CardLiabilitySnapshot(card_id=1, last_statement_issue_date=date(2026, 10, 25))
    card = Card(account_id=1)
    assert estimate_next_close(card, liab, date(2026, 11, 10)) == (date(2026, 11, 25), "estimate")
    assert estimate_next_close(card, liab, date(2026, 11, 26)) == (date(2026, 12, 25), "estimate")
    card.manual_next_statement_close_date = date(2026, 11, 22)
    assert estimate_next_close(card, liab, date(2026, 11, 10)) == (date(2026, 11, 22), "manual")
    assert estimate_next_close(Card(account_id=2), None, date(2026, 11, 10)) == (None, None)


def test_add_months_clamps():
    assert timeutil.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert timeutil.add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)
