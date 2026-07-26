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

**Tax and postage are computed here, once.** They are arithmetic on a
sale, so they belong beside the rest of the sale's arithmetic rather
than in the checkout action, the invoice writer and the basket page
separately. `checkout_totals` is the single fold every one of those
callers restates; nothing else is allowed to add up its own version.
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


def tax_cents(taxable_cents: Any, rate_bps: Any) -> int:
    """Tax on a taxable base, rounded half-up ONCE.

    Basis points, matching invoice_lines' own convention: 1500 = 15%,
    825 = 8.25%. 0 means the shop charges no tax, which is a real answer
    and not a missing setting -- plenty of small sellers genuinely owe
    none, and a shop that cannot express that would have to lie.

    Rounded once, on the whole taxable amount, rather than per line. Tax
    is owed on a sale, not on each row of it, and rounding each row first
    accumulates a cent of error per line that nobody can reconcile: four
    fifty-cent lines at 5% are 10c of tax, not the 12c that four
    separately-rounded 2.5c lines come to. The single rounding is also
    the only version a customer can check with a calculator.
    """
    base = _num(taxable_cents)
    rate = _num(rate_bps)
    if base <= 0 or rate <= 0:
        return 0
    return int((base * rate / Decimal(10000)).to_integral_value(
        rounding=ROUND_HALF_UP))


def shipping_cents(subtotal_cents: Any, flat_cents: Any,
                   free_over_cents: Any = 0) -> int:
    """What the shop charges to send this basket: flat, or nothing.

    One flat rate, because a real carrier quote needs an address, a
    weight and a connector, and a shop that cannot charge postage at all
    until that exists is a shop losing money on every parcel today. A
    flat rate is honest about being a flat rate; a fake calculated one
    would not be.

    `free_over_cents` is the free-shipping threshold, and it is here in
    v1 on purpose: it is the one discount every small shop actually
    runs. "Free delivery over $50" is a pricing lever, a basket-size
    nudge and a thing customers look for by name, and leaving it out
    would push every seller into faking it with a discount code.

    flat_cents of 0 means shipping is disabled entirely -- a digital-only
    shop charges no postage and must not be made to show a zero line.
    """
    flat = _int(flat_cents)
    if flat <= 0:
        return 0
    threshold = _int(free_over_cents)
    if threshold > 0 and _int(subtotal_cents) >= threshold:
        return 0
    return flat


def checkout_totals(items: list[dict], *, tax_rate_bps: Any = 0,
                    tax_shipping: bool = False,
                    shipping_flat_cents: Any = 0,
                    free_over_cents: Any = 0) -> dict:
    """The whole bill: {"lines", "subtotal_cents", "shipping_cents",
    "tax_cents", "total_cents", "shipping_free", "count"}.

    ONE function, so the arithmetic of a sale exists in exactly one
    place. The order, the invoice, the basket page and the preview all
    call this and then RESTATE what it said -- none of them may add up
    their own version. Money that is computed twice is money that
    disagrees with itself eventually, and the disagreement always
    surfaces as a customer holding an invoice whose lines do not come to
    its total.

    `subtotal_cents` is goods only. Shipping is a separate charge and a
    separate invoice LINE, so the invoice total stays the sum of its own
    lines plus tax, and a customer can see what the postage cost instead
    of finding it baked into a total they cannot check.

    `tax_shipping` decides whether the postage is part of the taxable
    base. Jurisdictions genuinely disagree about this -- some tax
    delivery on a taxable order, some never do -- so it is a setting the
    seller chooses, not a rule this file invents for them.

    `shipping_free` says the threshold was MET rather than that shipping
    happens to be zero. The page needs to tell somebody they earned free
    delivery, and a bare 0 cannot distinguish "you saved the postage"
    from "this shop does not post things".
    """
    summary = totals(items)
    subtotal = summary["subtotal_cents"]
    postage = shipping_cents(subtotal, shipping_flat_cents, free_over_cents)
    taxable = subtotal + (postage if tax_shipping else 0)
    tax = tax_cents(taxable, tax_rate_bps)
    return {
        "lines": summary["lines"],
        "count": summary["count"],
        "subtotal_cents": subtotal,
        "shipping_cents": postage,
        "shipping_free": bool(_int(shipping_flat_cents) > 0 and postage == 0),
        "tax_cents": tax,
        "total_cents": subtotal + postage + tax,
    }


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
