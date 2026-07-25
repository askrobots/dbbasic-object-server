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


# --- conversion -------------------------------------------------------------
#
# Two rules govern everything below, and both are the kind that only look
# pedantic until an auditor asks:
#
# 1. **Never look forward.** A rate dated after the moment being valued may
#    never be used. Valuing yesterday's transaction with today's price is
#    time travel; it silently restates history and is a classic audit
#    finding. A lookup returns the newest rate at-or-before the moment, or
#    nothing at all.
# 2. **The rate is an input; the result is money.** Rates are measurements
#    with significant figures, so they are stored as exact decimal strings.
#    The converted amount is a count of minor units, so it is an integer,
#    and the rounding that produces it is stated rather than inherited.

RATES_COLLECTION = "rates"

KIND_SPOT = "spot"
KIND_CLOSE = "close"
KIND_AVERAGE = "average"


class RateNotFound(LookupError):
    """No usable rate at or before the moment asked about."""


def _norm(code: str) -> str:
    return str(code or "").strip().upper()


def rate_records(base_code: str, quote_code: str, *,
                 base_dir: Path | str = DEFAULT_DATA_DIR) -> list[dict]:
    """Every stored rate for a pair, newest `as_of` first."""
    want_base, want_quote = _norm(base_code), _norm(quote_code)
    try:
        rows = object_records.read_collection_records(RATES_COLLECTION, base_dir=base_dir)
    except Exception:
        return []
    matches = [r for r in rows
               if _norm(r.get("base_code")) == want_base
               and _norm(r.get("quote_code")) == want_quote]
    matches.sort(key=lambda r: (str(r.get("as_of") or ""), str(r.get("fetched_at") or "")),
                 reverse=True)
    return matches


def find_rate(base_code: str, quote_code: str, *,
              base_dir: Path | str = DEFAULT_DATA_DIR,
              as_of: str = "", kind: str = "", source: str = "",
              allow_inverse: bool = True) -> dict | None:
    """The rate to use for a pair at a moment, or None.

    Returns the newest rate dated at or before `as_of` (all of them when
    `as_of` is blank), optionally narrowed to a kind or a source. When the
    pair is only stored the other way round, the inverse is derived and
    marked `inverted` so a caller can see that the number was computed
    rather than quoted -- inversion loses precision at the far end of a
    ratio, and pretending otherwise would hide it.
    """
    if _norm(base_code) == _norm(quote_code):
        return {"base_code": _norm(base_code), "quote_code": _norm(quote_code),
                "rate": "1", "as_of": as_of, "kind": kind or KIND_SPOT,
                "source": "identity", "inverted": False}

    def _pick(rows):
        for row in rows:
            if kind and str(row.get("kind") or KIND_SPOT).strip().lower() != kind:
                continue
            if source and str(row.get("source") or "").strip().lower() != source.lower():
                continue
            if as_of and str(row.get("as_of") or "") > as_of:
                continue          # never look forward
            try:
                if Decimal(str(row.get("rate") or "0")) <= 0:
                    continue      # a non-positive rate is data corruption
            except InvalidOperation:
                continue
            return row
        return None

    direct = _pick(rate_records(base_code, quote_code, base_dir=base_dir))
    if direct is not None:
        return {**direct, "inverted": False}
    if not allow_inverse:
        return None
    reverse = _pick(rate_records(quote_code, base_code, base_dir=base_dir))
    if reverse is None:
        return None
    inverse = Decimal(1) / Decimal(str(reverse["rate"]))
    return {**reverse, "base_code": _norm(base_code), "quote_code": _norm(quote_code),
            "rate": str(inverse), "inverted": True}


def convert(amount_minor: Any, from_code: str, to_code: str, rate: Any, *,
            base_dir: Path | str = DEFAULT_DATA_DIR,
            rounding=ROUND_HALF_UP) -> int:
    """Convert minor units of one denomination into minor units of another.

    `rate` is how many `to_code` units one `from_code` unit is worth. The
    caller passes the rate explicitly — usually the one it is about to STAMP
    onto the record (docs/logic-decisions.md #1) — because a conversion that
    silently looked up its own rate would be re-derivable later, and a
    re-derivable conversion is one that can change after the fact.

    Rounding happens once, at the end, at the target's scale: converting
    then rounding per line is how a total stops matching the sum of its
    parts.
    """
    from_scale = scale_for(from_code, base_dir=base_dir)
    to_scale = scale_for(to_code, base_dir=base_dir)
    try:
        multiplier = Decimal(str(rate))
    except InvalidOperation as exc:
        raise MoneyError(f"Not a rate: {rate!r}") from exc
    if multiplier <= 0:
        raise MoneyError(f"A conversion rate must be positive, got {rate!r}")
    whole = from_minor(amount_minor, from_scale)
    return int((whole * multiplier).scaleb(to_scale).to_integral_value(rounding=rounding))


def convert_at(amount_minor: Any, from_code: str, to_code: str, *,
               base_dir: Path | str = DEFAULT_DATA_DIR, as_of: str = "",
               kind: str = "", source: str = "") -> dict:
    """Convert using the stored rate that applies at `as_of`.

    Returns {"amount_minor", "rate", "rate_id", "as_of", "source",
    "inverted"} — the rate comes back with the result precisely so the
    caller can stamp it onto whatever it writes. Raises RateNotFound rather
    than guessing: a conversion with no rate behind it is a number nobody
    can defend later.
    """
    found = find_rate(from_code, to_code, base_dir=base_dir, as_of=as_of,
                      kind=kind, source=source)
    if found is None:
        raise RateNotFound(
            f"No {from_code}/{to_code} rate on or before {as_of or 'now'}"
            + (f" from {source}" if source else ""))
    return {
        "amount_minor": convert(amount_minor, from_code, to_code, found["rate"],
                                base_dir=base_dir),
        "rate": str(found["rate"]),
        "rate_id": found.get("id", ""),
        "as_of": found.get("as_of", ""),
        "source": found.get("source", ""),
        "inverted": bool(found.get("inverted")),
    }


# --- assurance --------------------------------------------------------------

# How much a reconciliation of an account is actually worth, by the class of
# evidence behind it. Doctrine #8 says the control is that evidence was
# authored by someone with no stake in our numbers -- which means these are
# NOT equivalent, and a system that renders them identically is quietly
# telling its operator a comfortable lie.
ASSURANCE_BY_VERIFICATION = {
    # Anyone can verify it, no institution need be trusted, history is
    # cryptographically ordered. Stronger evidence than a bank statement.
    "chain_query": "strong",
    # An independent third party attests -- but you trust their copy, and
    # they can restate it.
    "statement_import": "strong",
    # The issuer states a number, usually with no line-item history to tie
    # out against.
    "issuer_balance": "medium",
    # Self-certification: the person counting the till is usually the person
    # who could take from it. Witnessed counts are why this can be lifted.
    "physical_count": "weak",
    "none": "none",
}


def assurance_for(verification: str, *, witnessed: bool = False) -> str:
    """The evidence class a verification method actually provides.

    `witnessed` lifts a physical count out of self-certification: a second
    person attesting is the oldest control there is against the count and
    the custody being the same pair of hands.
    """
    level = ASSURANCE_BY_VERIFICATION.get(str(verification or "").strip(), "none")
    if level == "weak" and witnessed:
        return "medium"
    return level


def same_denomination(*codes: str) -> bool:
    """True when every code given is the same denomination.

    Amounts in different denominations must never be added -- a sum of
    dollars and satoshis is not a number, it is a bug. Callers that need to
    combine them convert through a stamped rate first (the rate at the
    moment of the transaction, never today's -- docs/logic-decisions.md #1).
    """
    seen = {str(c or "").strip().upper() for c in codes if str(c or "").strip()}
    return len(seen) <= 1
