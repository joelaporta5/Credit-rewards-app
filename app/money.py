"""Money helpers. All money is stored as integer cents. Never use floats for currency.

Plaid returns JSON numbers; we convert them via their decimal string representation,
never by multiplying a float.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_CURRENCY_SYMBOLS = {"$": "USD", "US$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "C$": "CAD", "CA$": "CAD"}
_CODE_RE = re.compile(r"\b([A-Z]{3})\b")


@dataclass
class ParsedAmount:
    cents: int | None
    currency: str | None = None
    warnings: list[str] = field(default_factory=list)


def decimal_to_cents(d: Decimal) -> int:
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def plaid_amount_to_cents(value) -> int | None:
    """Convert a Plaid JSON number (float/int/str) to integer cents."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not an amount")
    try:
        # str(float) is the shortest round-trip representation, e.g. 12.34 -> "12.34".
        return decimal_to_cents(Decimal(str(value)))
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {value!r}") from exc


def parse_amount(raw) -> ParsedAmount:
    """Parse an amount typed or formatted by a phone, e.g. "$12.34", "12,34", "1.234,56", "USD 5".

    Returns cents=None (with a warning) if the value can't be understood.
    The sign is preserved: "-$5.00" and "(5.00)" are negative.
    """
    warnings: list[str] = []
    if raw is None:
        return ParsedAmount(None, None, ["amount missing"])
    if isinstance(raw, bool):
        return ParsedAmount(None, None, [f"amount not understood: {raw!r}"])
    if isinstance(raw, int):
        return ParsedAmount(raw * 100, None, [])
    if isinstance(raw, float):
        return ParsedAmount(decimal_to_cents(Decimal(str(raw))), None, [])
    if isinstance(raw, Decimal):
        return ParsedAmount(decimal_to_cents(raw), None, [])

    s = str(raw).strip().replace(" ", " ").replace(" ", " ")
    if not s:
        return ParsedAmount(None, None, ["amount empty"])

    currency = None
    for sym in sorted(_CURRENCY_SYMBOLS, key=len, reverse=True):
        if sym in s:
            currency = _CURRENCY_SYMBOLS[sym]
            s = s.replace(sym, " ")
            break
    m = _CODE_RE.search(s.upper())
    if m:
        currency = currency or m.group(1)
        s = re.sub(r"[A-Za-z]{3}", " ", s)

    negative = False
    s = s.strip()
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    s = s.strip()
    if s.startswith("-") or s.endswith("-") or s.startswith("−"):
        negative = True
    s = s.replace("−", "").replace("-", "").replace("+", "").replace(" ", "").replace("'", "")

    if not s or not re.fullmatch(r"[0-9.,]+", s) or not re.search(r"\d", s):
        return ParsedAmount(None, currency, [f"amount not understood: {raw!r}"])

    last_dot, last_comma = s.rfind("."), s.rfind(",")
    if last_dot >= 0 and last_comma >= 0:
        # Both present: whichever comes last is the decimal separator.
        dec = "." if last_dot > last_comma else ","
        thou = "," if dec == "." else "."
        number = s.replace(thou, "").replace(dec, ".")
    elif last_comma >= 0 or last_dot >= 0:
        sep = "," if last_comma >= 0 else "."
        parts = s.split(sep)
        if len(parts) > 2:
            # "1,234,567" or "1.234.567": thousands separators.
            if all(len(p) == 3 for p in parts[1:]):
                number = "".join(parts)
            else:
                return ParsedAmount(None, currency, [f"amount not understood: {raw!r}"])
        else:
            frac = parts[1]
            if len(frac) in (1, 2):
                number = parts[0] + "." + frac
            elif len(frac) == 3:
                # "1,234" is almost certainly one thousand two hundred thirty-four dollars.
                number = parts[0] + frac
                warnings.append(f"treated '{sep}' in {raw!r} as a thousands separator")
            elif len(frac) == 0:
                number = parts[0]
            else:
                number = parts[0] + "." + frac
                warnings.append(f"amount {raw!r} has more than 2 decimals; rounded")
    else:
        number = s

    try:
        d = Decimal(number)
    except InvalidOperation:
        return ParsedAmount(None, currency, [f"amount not understood: {raw!r}"])
    cents = decimal_to_cents(d)
    if negative:
        cents = -cents
    if currency and currency != "USD":
        warnings.append(f"currency {currency} is not USD; amount was not converted")
    return ParsedAmount(cents, currency, warnings)


def fmt(cents: int | None, signed: bool = False) -> str:
    """Format cents as dollars, e.g. 123456 -> "$1,234.56"."""
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ("+" if signed and cents > 0 else "")
    c = abs(int(cents))
    return f"{sign}${c // 100:,}.{c % 100:02d}"
