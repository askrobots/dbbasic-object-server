"""Line arithmetic: one implementation of what a document line costs.

app-orders and app-invoices each carried their own `_line_amounts`, near
enough identical that the orders one is the invoices one plus
`modifier_cents`. That duplication was survivable while the answer was
"quantity times price"; it stops being survivable the moment a discount
has to be subtracted in both, and app-shop, the cart, the emails and the
document renderer all restate the same number too.

So the math lives here, once, pure -- no I/O, no clock, testable without
a data directory, same posture as object_promotions and object_cart. The
totals handlers become callers rather than authors.

## What a discount is here, and what it is NOT

This is a LINE discount: a negotiated reduction on one row -- ex-display
stock, a trade rate, ten per cent off this item and nothing else. It is
recorded on the line and prints on the invoice beside what it reduced.

It is not a promotion. object_promotions owns those: a code somebody
types, applied to the whole basket and ROUNDED ONCE, deliberately, so
four separately-rounded lines cannot accumulate an error nobody can
reconcile against the percentage printed on the receipt. The two compose
-- a line discount changes the line, a promotion then comes off the
discounted subtotal -- and keeping them apart is what stops "10% off"
meaning two different numbers depending on which layer applied it.

## The order of operations, which is the whole specification

    gross    = floor(quantity x (unit_price + modifier))
    discount = gross x discount_bps // 10000
    net      = gross - discount
    tax      = net x tax_rate_bps // 10000

Three things about that, each of which would be a bug the other way:

**Tax is charged on the DISCOUNTED amount.** A discount on goods reduces
the taxable base -- object_promotions.applies_to says the same thing for
promotions, and the two must not disagree about it. Taxing the gross
would charge a customer tax on money nobody ever asked them for.

**The discount floors off the already-floored gross**, not off a
fractional intermediate. This is what makes a zero discount arithmetically
invisible: every existing line total is bit-identical to what it was
before this module existed, which is the only acceptable behaviour for a
change that touches every historical document.

**A discount can never make a line negative.** 12000 basis points is a
typo, not a refund, and a line that pays the customer is not something to
infer from a mistyped rate.

Basis points, matching `tax_rate_bps`: 1000 = 10%. A flat per-unit
reduction already has a home in `modifier_cents`, which is signed -- the
percentage is what nothing could express.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any

#: 100% off. Anything above this is a typo rather than an intention.
MAX_DISCOUNT_BPS = 10000


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value if value is not None else "").strip() or default)
    except (TypeError, ValueError):
        return default


def _decimal(value: Any) -> Decimal:
    text = str(value if value is not None else "").strip()
    if not text:
        return Decimal("0")
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal("0")


def line_amounts(line: Any) -> dict:
    """Every money number one document line implies.

    Returns {"gross_cents", "discount_cents", "line_total_cents",
    "line_tax_cents"}. `line_total_cents` is the NET of discount, because
    that is the number every existing caller already stores under that
    name and the number a customer is asked to pay.

    Reads every field with .get and a zero default, so a row written
    before any of these columns existed folds exactly as it always did.
    """
    line = line or {}
    quantity = _decimal(line.get("quantity"))
    unit_price = _decimal(line.get("unit_price_cents"))
    # A per-unit delta ("oat milk +60c"), added to the UNIT price because
    # two oat lattes are two lots of oat milk. Signed, so it is also how a
    # flat per-unit reduction is expressed.
    modifier = _decimal(line.get("modifier_cents"))

    gross_cents = int((quantity * (unit_price + modifier)).to_integral_value(
        rounding=ROUND_FLOOR))

    discount_bps = _int(line.get("discount_bps"))
    if discount_bps < 0:
        discount_bps = 0
    if discount_bps > MAX_DISCOUNT_BPS:
        discount_bps = MAX_DISCOUNT_BPS

    # Integer floor division on an already-integer gross: with no
    # discount this is exactly zero and every historical total is
    # untouched.
    discount_cents = (gross_cents * discount_bps) // 10000
    if gross_cents < 0:
        # A negative line (a credit row) is not discountable; taking a
        # percentage off it would make the credit smaller, which is the
        # opposite of what anyone typing "10% off" means.
        discount_cents = 0
    discount_cents = min(discount_cents, max(gross_cents, 0))

    line_total_cents = gross_cents - discount_cents
    tax_rate_bps = _int(line.get("tax_rate_bps"))
    line_tax_cents = (line_total_cents * tax_rate_bps) // 10000

    return {
        "gross_cents": gross_cents,
        "discount_cents": discount_cents,
        "line_total_cents": line_total_cents,
        "line_tax_cents": line_tax_cents,
    }


def totals(lines: Any) -> dict:
    """Fold many lines into document-level sums.

    Kept beside the per-line math so a caller cannot fold with one rule
    and display with another -- the failure the orders handler's own
    docstring warns about, where a fold that cannot reproduce the number
    it restates quietly replaces a correct total with a smaller one.
    """
    gross = discount = subtotal = tax = 0
    for line in lines or ():
        amounts = line_amounts(line)
        gross += amounts["gross_cents"]
        discount += amounts["discount_cents"]
        subtotal += amounts["line_total_cents"]
        tax += amounts["line_tax_cents"]
    return {
        "gross_cents": gross,
        "discount_cents": discount,
        "subtotal_cents": subtotal,
        "tax_cents": tax,
        "total_cents": subtotal + tax,
    }
