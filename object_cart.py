"""The basket: what somebody intends to buy, before they commit.

Pure arithmetic and pure decisions, testable without a data directory --
same posture as object_billing, object_rates and object_money.

**A basket is not an order.** An order is a commitment: it has a number,
a customer, a books consequence and a place in a sequence somebody
counts. A basket is a browsing artefact that usually gets abandoned.
Modelling one as a draft order fills the order sequence with junk and
makes "how many orders did we take?" unanswerable, which is why carts are
their own collection with their own lifetime.

**Price is stamped when it goes in, and re-checked at checkout.** A price
that moves while somebody shops must not silently change what they
agreed to; a basket that sat for three weeks must not hold the seller to
a stale price either. So both numbers are kept and the disagreement is
SURFACED rather than resolved in secret -- the same instinct as stamping
a rate at approval, and refusing to let a later change restate it.

**Availability is decided at checkout, never at add.** Reserving stock
when somebody drops a thing in a basket is how a shop shows "sold out"
for goods nobody bought.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _num(value: Any, default: int = 0) -> Decimal:
    try:
        return Decimal(_text(value) or str(default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(_text(value) or default)
    except (TypeError, ValueError):
        return default


def line_total_cents(quantity: Any, unit_price_cents: Any) -> int:
    """One line's money, rounded half-up once.

    Fractional quantities are real (1.5 kg, 2.5 hours of setup), so this
    is a Decimal multiplication rather than an integer one -- and the
    rounding happens here, per line, so the basket total always equals the
    sum of the lines a shopper can see.
    """
    total = _num(quantity) * _num(unit_price_cents)
    if total <= 0:
        return 0
    return int(total.to_integral_value(rounding=ROUND_HALF_UP))


def totals(items: list[dict]) -> dict:
    """What the basket comes to: {"lines", "subtotal_cents", "count"}."""
    lines = []
    subtotal = 0
    count = Decimal(0)
    for item in items:
        quantity = _num(item.get("quantity"))
        if quantity <= 0:
            continue
        amount = line_total_cents(quantity, item.get("unit_price_cents"))
        lines.append({
            "cart_item_id": _text(item.get("id")),
            "product_id": _text(item.get("product_id")),
            "description": _text(item.get("description")),
            "quantity": str(quantity),
            "unit_price_cents": _int(item.get("unit_price_cents")),
            "line_total_cents": amount,
        })
        subtotal += amount
        count += quantity
    return {"lines": lines, "subtotal_cents": subtotal, "count": str(count)}


def price_changes(items: list[dict], products: dict[str, dict]) -> list[dict]:
    """Lines whose product now costs something else than when it went in.

    Reported, never applied. Silently charging the new price is a
    bait-and-switch and silently honouring the old one is an open-ended
    liability on anything left in a basket; the only defensible move is to
    show the shopper both numbers and let them agree again.
    """
    changed = []
    for item in items:
        product = products.get(_text(item.get("product_id")))
        if not product:
            continue
        was = _int(item.get("unit_price_cents"))
        now = _int(product.get("price_cents"))
        if now and now != was:
            changed.append({
                "cart_item_id": _text(item.get("id")),
                "product_id": _text(item.get("product_id")),
                "description": _text(item.get("description"))
                               or _text(product.get("name")),
                "was_cents": was,
                "now_cents": now,
                "direction": "up" if now > was else "down",
            })
    return changed


def availability(items: list[dict], on_hand: dict[str, Any],
                 *, tracked: set[str] | None = None) -> list[dict]:
    """Lines that cannot be filled from stock, with how many are short.

    Only products the shop actually TRACKS are checked. A service, a
    digital download or a made-to-order item has no stock level, and
    treating "no level recorded" as "none available" would refuse to sell
    the very things that are always available.
    """
    short = []
    for item in items:
        product_id = _text(item.get("product_id"))
        if tracked is not None and product_id not in tracked:
            continue
        if product_id not in on_hand:
            continue
        wanted = _num(item.get("quantity"))
        have = _num(on_hand.get(product_id))
        if wanted > have:
            short.append({
                "cart_item_id": _text(item.get("id")),
                "product_id": product_id,
                "description": _text(item.get("description")),
                "wanted": str(wanted),
                "available": str(have if have > 0 else 0),
                "short_by": str(wanted - have),
            })
    return short


def checkout_blockers(items: list[dict], products: dict[str, dict],
                      on_hand: dict[str, Any],
                      *, tracked: set[str] | None = None) -> dict:
    """Everything standing between this basket and an order.

    Returns {"empty", "price_changes", "unavailable", "inactive",
    "can_checkout"}. Gathered in one pass and ALL reported together:
    telling a shopper about one problem, letting them fix it, then
    revealing the next is how a checkout gets abandoned.
    """
    inactive = []
    for item in items:
        product = products.get(_text(item.get("product_id")))
        if product is None:
            inactive.append({"product_id": _text(item.get("product_id")),
                             "reason": "no longer in the catalogue"})
        elif _text(product.get("is_active")).lower() in ("false", "0", "no", "off"):
            inactive.append({"product_id": _text(item.get("product_id")),
                             "description": _text(product.get("name")),
                             "reason": "no longer for sale"})

    changes = price_changes(items, products)
    short = availability(items, on_hand, tracked=tracked)
    empty = not [i for i in items if _num(i.get("quantity")) > 0]
    return {
        "empty": empty,
        "price_changes": changes,
        "unavailable": short,
        "inactive": inactive,
        "can_checkout": not (empty or changes or short or inactive),
    }
