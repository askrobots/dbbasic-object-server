"""Money representation: an amount is an integer in its denomination's
smallest unit, and the denomination declares what that unit is.

This is the generalization of the "money is integer cents" rule that has
governed this codebase from the start (plan/vocabulary/00-doctrine-and-
contract.md): cents were always *USD minor units*. A business does not hold
only dollars -- it holds bank balances, bitcoin, stablecoins, a box of
physical cash, gift cards, maybe a weight of metal -- and those are
denominated differently, divided differently, and custodied differently
(plan/value-accounts-and-denominations-spec.md).

    USD  scale 2   150000 minor = 1,500.00
    JPY  scale 0      1500 minor = 1,500       (no minor unit at all)
    BTC  scale 8   5000000 minor = 0.05        (satoshis)
    ETH  scale 18                              (Python ints are unbounded)

**Why integer-minor rather than a fixed-scale decimal.** The predecessor
system (private q9 audit, not part of this repo) declared a per-currency
`decimal_places` -- "2 for USD, 0 for JPY, 8 for BTC, 4 for gold" -- while
storing every amount in one fixed Decimal(28,8) column. A schema that lets
a denomination declare 18 decimals but can only hold 8 will silently
truncate the first asset that needs them, and meanwhile it carries six
meaningless digits on every dollar, which is how rounding dust gets into a
ledger. Storing the integer count of the smallest unit has neither problem:
exact by construction, no scale to disagree about, no ceiling.

Floats never appear here. Decimal is used only at the edges, to convert
human text to and from integers.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import object_records
from object_versions import DEFAULT_DATA_DIR

DENOMINATIONS_COLLECTION = "denominations"

DEFAULT_CODE = "USD"
DEFAULT_SCALE = 2
MAX_SCALE = 30       # ETH needs 18; the cap only rejects nonsense

# Fallbacks for the common denominations so formatting still works before
# the collection is installed or seeded. The COLLECTION is authoritative --
# these exist so a fresh install renders sanely, not as a price table.
_FALLBACK_SCALES = {
    "USD": 2, "EUR": 2, "GBP": 2, "CAD": 2, "AUD": 2, "CHF": 2,
    "JPY": 0, "KRW": 0,
    "BTC": 8, "ETH": 18, "USDC": 6, "USDT": 6,
    "XAU": 4, "XAG": 4,
}


class MoneyError(ValueError):
    """An amount that cannot be represented exactly in its denomination."""


def denomination(code: str, *, base_dir: Path | str = DEFAULT_DATA_DIR) -> dict | None:
    """The denomination record for a code, or None."""
    wanted = str(code or "").strip().upper()
    if not wanted:
        return None
    try:
        rows = object_records.read_collection_records(
            DENOMINATIONS_COLLECTION, base_dir=base_dir)
    except Exception:
        return None
    for row in rows:
        if str(row.get("code") or "").strip().upper() == wanted:
            return row
    return None


def scale_for(code: str, *, base_dir: Path | str = DEFAULT_DATA_DIR) -> int:
    """How many decimal places this denomination divides into.

    Reads the denominations collection, falls back to the well-known table,
    and finally to 2. Never raises: formatting an unknown code as if it were
    dollars is a display inconvenience, while an exception here would take
    down any page that shows an amount.
    """
    row = denomination(code, base_dir=base_dir)
    if row is not None:
        try:
            value = int(str(row.get("scale")).strip())
            if 0 <= value <= MAX_SCALE:
                return value
        except (TypeError, ValueError):
            pass
    return _FALLBACK_SCALES.get(str(code or "").strip().upper(), DEFAULT_SCALE)


def to_minor(amount: Any, scale: int) -> int:
    """Human amount ("1,500.00", Decimal("0.05")) -> integer minor units.

    Refuses to round: 0.005 USD is not two decimal places, and quietly
    turning it into a cent is how a half-cent-per-row error becomes a real
    number at scale. Callers that genuinely want rounding must say so with
    quantize_minor().
    """
    text = str(amount if amount is not None else "").strip().replace(",", "")
    if not text:
        return 0
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise MoneyError(f"Not a number: {amount!r}") from exc
    shifted = value.scaleb(int(scale))
    if shifted != shifted.to_integral_value():
        raise MoneyError(
            f"{amount} has more precision than a {scale}-decimal denomination can hold")
    return int(shifted)


def quantize_minor(amount: Any, scale: int, *, rounding=ROUND_HALF_UP) -> int:
    """Like to_minor, but rounds deliberately (a rate calculation, a split).

    Separate from to_minor on purpose: rounding money should be a decision
    someone made in the code, not something the parser does behind them.
    """
    text = str(amount if amount is not None else "").strip().replace(",", "")
    if not text:
        return 0
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise MoneyError(f"Not a number: {amount!r}") from exc
    return int(value.scaleb(int(scale)).to_integral_value(rounding=rounding))


def from_minor(amount_minor: Any, scale: int) -> Decimal:
    """Integer minor units -> exact Decimal in whole units."""
    try:
        value = int(str(amount_minor).strip() or 0)
    except (TypeError, ValueError):
        return Decimal(0)
    return Decimal(value).scaleb(-int(scale))


def format_amount(amount_minor: Any, code: str = DEFAULT_CODE, *,
                  base_dir: Path | str = DEFAULT_DATA_DIR,
                  with_code: bool = True, group: bool = True) -> str:
    """Render minor units for a human: 150000 USD -> "1,500.00 USD".

    Trailing zeros are kept to the denomination's full scale, because an
    amount displayed as "0.05" when the unit divides into eight places
    hides whether the rest is zero or merely unshown -- and for assets where
    the small digits are real money, that ambiguity matters.
    """
    scale = scale_for(code, base_dir=base_dir)
    value = from_minor(amount_minor, scale)
    text = f"{value:,.{scale}f}" if group else f"{value:.{scale}f}"
    row = denomination(code, base_dir=base_dir)
    symbol = str((row or {}).get("symbol") or "").strip()
    if symbol:
        text = f"{symbol}{text}"
    if with_code and str(code or "").strip():
        text = f"{text} {str(code).strip().upper()}"
    return text


def parse_amount(text: Any, code: str = DEFAULT_CODE, *,
                 base_dir: Path | str = DEFAULT_DATA_DIR) -> int:
    """Human text -> integer minor units, using the denomination's scale."""
    cleaned = str(text if text is not None else "").strip()
    for token in (str(code or "").strip().upper(), str(code or "").strip()):
        if token and cleaned.upper().endswith(token.upper()):
            cleaned = cleaned[: -len(token)].strip()
            break
    row = denomination(code, base_dir=base_dir)
    symbol = str((row or {}).get("symbol") or "").strip()
    if symbol and cleaned.startswith(symbol):
        cleaned = cleaned[len(symbol):].strip()
    return to_minor(cleaned, scale_for(code, base_dir=base_dir))


def same_denomination(*codes: str) -> bool:
    """True when every code given is the same denomination.

    Amounts in different denominations must never be added -- a sum of
    dollars and satoshis is not a number, it is a bug. Callers that need to
    combine them convert through a stamped rate first (the rate at the
    moment of the transaction, never today's -- docs/logic-decisions.md #1).
    """
    seen = {str(c or "").strip().upper() for c in codes if str(c or "").strip()}
    return len(seen) <= 1
