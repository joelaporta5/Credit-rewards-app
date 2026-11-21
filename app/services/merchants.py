"""Merchant name normalization, fuzzy similarity and category detection."""

from __future__ import annotations

import re

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Merchant, PlaidTransaction

# Payment-processor prefixes that card statements put in front of the real merchant,
# e.g. "SQ *BLUE BOTTLE", "TST* JOE'S DINER", "PAYPAL *ETSY".
_PREFIX_RE = re.compile(
    r"^\s*(?:SQ|SQU|SQUARE|TST|TOAST|SP|PP|PAYPAL|DD|DOORDASH|IC|CKE|PY|BT|GOOGLE|GGL|APL|APPLE|AMZN|AMZ|"
    r"FS|ZTL|PAR|CLV|SUMUP|SUM|IZ|IZETTLE|WPY|EB|STRIPE|PLI|LS|LSP|HLU|PRX|SPO|UBR|BB|CHK|RSV|MSFT|NYX|"
    r"AUTH|POS|PMT)\s*\*\s*",
    re.IGNORECASE,
)
_LEADING_WORDS_RE = re.compile(
    r"^\s*(?:POS\s+PURCHASE|DEBIT\s+CARD\s+PURCHASE|PURCHASE\s+AUTHORIZED\s+ON\s+\d\d/\d\d|CHECKCARD\s+\d{4}|"
    r"APPLE\s+PAY|RECURRING\s+PAYMENT|POS|PURCHASE)\s+",
    re.IGNORECASE,
)
_STOP = {"INC", "LLC", "LTD", "CO", "CORP", "THE", "STORE", "STORES", "COM", "WWW", "US", "USA", "AND"}

RESTAURANT, GAS, LODGING, CAR_RENTAL, OTHER = "restaurant", "gas", "lodging", "car_rental", "other"

_PFC_CATEGORY = {
    # Verified against Plaid's PFC taxonomy CSV (v1 and v2): "FOOD_AND_DRINK_RESTAURANT" covers
    # restaurants, bars and gastropubs.
    "FOOD_AND_DRINK_RESTAURANT": RESTAURANT,
    "TRANSPORTATION_GAS": GAS,
    "TRAVEL_LODGING": LODGING,
    "TRAVEL_RENTAL_CARS": CAR_RENTAL,
}

_GAS_WORDS = {"SHELL", "EXXON", "EXXONMOBIL", "MOBIL", "CHEVRON", "TEXACO", "SUNOCO", "MARATHON", "SPEEDWAY",
              "CITGO", "VALERO", "CONOCO", "PHILLIPS", "ARCO", "BP", "GULF", "SINCLAIR", "CIRCLE K", "QUIKTRIP",
              "RACETRAC", "GAS", "FUEL", "PETRO", "SHEETZ", "CASEYS", "PILOT", "LOVES", "COSTCO GAS"}
_LODGING_WORDS = {"HOTEL", "HOTELS", "MOTEL", "INN", "RESORT", "MARRIOTT", "HILTON", "HYATT", "WESTIN",
                  "SHERATON", "AIRBNB", "HOLIDAY INN", "HAMPTON", "COURTYARD", "SUITES", "LODGE", "VRBO"}
_CAR_RENTAL_WORDS = {"HERTZ", "AVIS", "BUDGET RENT", "ENTERPRISE RENT", "NATIONAL CAR", "ALAMO", "SIXT",
                     "THRIFTY", "DOLLAR RENT", "TURO", "RENT A CAR", "RENTAL CAR"}
_RESTAURANT_WORDS = {"RESTAURANT", "GRILL", "TAVERN", "PUB", "BISTRO", "KITCHEN", "STEAKHOUSE", "BRASSERIE",
                     "CANTINA", "TRATTORIA", "SUSHI", "PIZZERIA", "BAR", "BREWERY", "BREWING", "DINER", "CAFE",
                     "TAQUERIA", "RAMEN", "BBQ", "OSTERIA", "GASTROPUB", "LOUNGE", "WINE BAR"}


def clean_merchant(name: str | None) -> str:
    """Strip processor prefixes, store numbers and punctuation. 'SQ *BLUE BOTTLE #12' -> 'BLUE BOTTLE'."""
    if not name:
        return ""
    s = name.upper().strip()
    for _ in range(2):
        s = _PREFIX_RE.sub("", s)
        s = _LEADING_WORDS_RE.sub("", s)
    s = re.sub(r"#\s*\d+", " ", s)  # store numbers
    s = re.sub(r"\b\d{3,}\b", " ", s)  # long numbers (store ids, phone fragments)
    s = re.sub(r"[^A-Z0-9& ]+", " ", s.replace("'", ""))
    tokens = [t for t in s.split() if t not in _STOP]
    return " ".join(tokens)


def similarity(a: str | None, b: str | None) -> float:
    """0..1 fuzzy similarity of two merchant names after cleaning."""
    ca, cb = clean_merchant(a), clean_merchant(b)
    if not ca or not cb:
        return 0.0
    if ca == cb:
        return 1.0
    scores = [fuzz.token_set_ratio(ca, cb), fuzz.WRatio(ca, cb)]
    # "BLUEBOTTLE" vs "BLUE BOTTLE"
    scores.append(fuzz.ratio(ca.replace(" ", ""), cb.replace(" ", "")))
    return max(scores) / 100.0


def best_similarity(wallet_name: str | None, txn: PlaidTransaction) -> float:
    names = [txn.merchant_name, txn.name]
    if txn.raw:
        for cp in txn.raw.get("counterparties") or []:
            if isinstance(cp, dict) and cp.get("name"):
                names.append(cp["name"])
    return max((similarity(wallet_name, n) for n in names if n), default=0.0)


def _keyword_category(name: str) -> str | None:
    padded = f" {name} "
    for words, cat in ((_CAR_RENTAL_WORDS, CAR_RENTAL), (_LODGING_WORDS, LODGING), (_GAS_WORDS, GAS),
                       (_RESTAURANT_WORDS, RESTAURANT)):
        if any(f" {w} " in padded for w in words):
            return cat
    return None


def _mcc_category(mcc: str | None) -> str | None:
    if not mcc or not mcc.isdigit():
        return None
    m = int(mcc)
    if m in (5812, 5813):  # eating places/restaurants, drinking places/bars (5814 fast food excluded)
        return RESTAURANT
    if m in (5541, 5542, 5983):
        return GAS
    if m == 7011 or 3501 <= m <= 3999:
        return LODGING
    if m in (7512, 7513, 7519) or 3351 <= m <= 3500:
        return CAR_RENTAL
    return None


def category_for_txn(txn: PlaidTransaction, merchant: Merchant | None = None) -> tuple[str, str]:
    """(category, source). A category I set manually on the merchant wins."""
    if merchant and merchant.category and merchant.category_source == "manual":
        return merchant.category, "manual"
    if txn.pfc_detailed and txn.pfc_detailed in _PFC_CATEGORY:
        return _PFC_CATEGORY[txn.pfc_detailed], "plaid_pfc"
    mcc_cat = _mcc_category(txn.merchant_category_code)
    if mcc_cat:
        return mcc_cat, "mcc"
    kw = _keyword_category(clean_merchant(txn.merchant_name or txn.name))
    if kw:
        return kw, "keyword"
    return OTHER, "default"


def get_or_create_merchant(session: Session, name: str | None, entity_id: str | None = None) -> Merchant | None:
    cleaned = clean_merchant(name)
    if not cleaned:
        return None
    m = session.scalar(select(Merchant).where(Merchant.normalized_name == cleaned))
    if m is None:
        m = Merchant(normalized_name=cleaned, display_name=(name or cleaned).strip()[:255], plaid_merchant_entity_id=entity_id)
        session.add(m)
        session.flush()
    elif entity_id and not m.plaid_merchant_entity_id:
        m.plaid_merchant_entity_id = entity_id
    return m


def classify_card_txn(txn: PlaidTransaction) -> str:
    """'purchase', 'refund' or 'payment' for a transaction on a credit card account."""
    if txn.amount_cents >= 0:
        return "purchase"
    if txn.pfc_primary in ("LOAN_PAYMENTS", "TRANSFER_IN") or txn.pfc_detailed == "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT":
        return "payment"
    text = f"{txn.name or ''} {txn.merchant_name or ''}".upper()
    if re.search(r"\b(PAYMENT|AUTOPAY|AUTO PAY|THANK YOU|PYMT|ONLINE PMT)\b", text):
        return "payment"
    return "refund"
