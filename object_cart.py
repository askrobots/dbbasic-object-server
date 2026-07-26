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

**An instruction is not a product.** A large latte is genuinely its own
product with its own SKU, price and stock, and variants-as-products
already handles that. "No onions" is not a product: it has no SKU, it is
true of one line of one order, and a catalogue row per instruction would
be thousands of dead products nobody can sell. So a line carries
`line_note` and `modifier_cents` -- "oat milk +60c" is the same
instruction with a price delta, which is why the delta lives on the line
rather than in the price book. The delta is money and goes through the
same single fold as everything else; the note is not money and moves no
total anywhere.

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


def line_total_cents(quantity: Any, unit_price_cents: Any,
                     modifier_cents: Any = 0) -> int:
    """One line's money, rounded half-up once.

    Fractional quantities are real (1.5 kg, 2.5 hours of setup), so this
    is a Decimal multiplication rather than an integer one -- and the
    rounding happens here, per line, so the basket total always equals the
    sum of the lines a shopper can see.

    `modifier_cents` is the per-line instruction's price delta -- "oat
    milk +60c" -- and it is added to the UNIT price rather than to the
    line: two oat lattes are two lots of oat milk, and a delta applied
    once to a line of six would be a shop giving away five of them. It is
    inside this number rather than beside it because every total
    downstream (basket, checkout preview, order, invoice) is a sum of
    line totals, and money that reaches one of those and not another is
    the bug this whole codebase is organised against.
    """
    total = _num(quantity) * (_num(unit_price_cents) + _num(modifier_cents))
    if total <= 0:
        return 0
    return int(total.to_integral_value(rounding=ROUND_HALF_UP))


def totals(items: list[dict]) -> dict:
    """What the basket comes to: {"lines", "subtotal_cents", "count"}.

    Each line carries its instruction (`line_note`) and its delta
    (`modifier_cents`) through to every caller, because the callers are
    the order writer, the invoice writer and the page -- and a note that
    stops here is a note the cook never reads. Both are read with .get:
    a basket written before those columns existed is still a basket.
    """
    lines = []
    subtotal = 0
    count = Decimal(0)
    for item in items:
        quantity = _num(item.get("quantity"))
        if quantity <= 0:
            continue
        modifier = _int(item.get("modifier_cents"))
        amount = line_total_cents(quantity, item.get("unit_price_cents"),
                                  modifier)
        lines.append({
            "cart_item_id": _text(item.get("id")),
            "product_id": _text(item.get("product_id")),
            "description": _text(item.get("description")),
            "quantity": str(quantity),
            "unit_price_cents": _int(item.get("unit_price_cents")),
            "modifier_cents": modifier,
            "line_note": _text(item.get("line_note")),
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
                    free_over_cents: Any = 0,
                    discount_cents: Any = 0,
                    discount_on: str = "goods",
                    credit_cents: Any = 0) -> dict:
    """The whole bill: {"lines", "subtotal_cents", "shipping_cents",
    "tax_cents", "discount_cents", "total_cents", "credit_applied_cents",
    "amount_due_cents", "shipping_free", "count"}.

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

    **A DISCOUNT COMES OFF THE TAXABLE BASE BEFORE TAX IS COMPUTED.** That
    is not a preference; it is what every jurisdiction that levies a sales
    or value-added tax actually requires. Tax is owed on the CONSIDERATION
    -- what the buyer actually gives up -- and a seller's own price
    reduction reduces it. Charging tax on the pre-discount price collects
    money from the customer that the shop then either remits on revenue it
    never earned or keeps, and both of those are the kind of small
    systematic error an audit finds by multiplying one rate by one column.
    So the discount is subtracted here, before tax_cents is called, and
    never afterwards.

    `discount_on` says which half of the bill the reduction comes off --
    "goods" (a percentage or a fixed amount) or "shipping" (a free-postage
    promotion) -- because where a jurisdiction taxes delivery, free
    postage must remove that tax too. The discount is CLAMPED to the base
    it comes off, so no combination of arguments can produce a negative
    total: a shop does not owe money to somebody for shopping.

    `credit_cents` is TENDER, not a price reduction -- a gift card or
    store credit -- and it therefore lands AFTER tax and changes no
    taxable base at all. A gift card is money the customer already gave
    the shop; treating it as a discount would let a shop sell $100 of
    goods and remit tax on $60, which is fraud with a rounding error's
    face. So `total_cents` stays the value of the SALE (what the order is
    worth, what the books see) and `amount_due_cents` is what is left to
    pay after the credit is applied. Where no credit is used the two are
    identical, which is every existing caller.
    """
    summary = totals(items)
    subtotal = summary["subtotal_cents"]
    postage = shipping_cents(subtotal, shipping_flat_cents, free_over_cents)

    wanted = max(0, _int(discount_cents))
    on_shipping = _text(discount_on).lower() == "shipping"
    goods_off = 0 if on_shipping else min(wanted, subtotal)
    postage_off = min(wanted, postage) if on_shipping else 0
    discount = goods_off + postage_off

    taxable = (subtotal - goods_off) + ((postage - postage_off)
                                        if tax_shipping else 0)
    tax = tax_cents(taxable, tax_rate_bps)
    total = subtotal + postage - discount + tax
    credit = min(max(0, _int(credit_cents)), total)
    return {
        "lines": summary["lines"],
        "count": summary["count"],
        "subtotal_cents": subtotal,
        "shipping_cents": postage,
        "shipping_free": bool(_int(shipping_flat_cents) > 0 and postage == 0),
        "discount_cents": discount,
        "discount_on": "shipping" if on_shipping else "goods",
        "taxable_cents": taxable,
        "tax_cents": tax,
        "total_cents": total,
        "credit_applied_cents": credit,
        "amount_due_cents": total - credit,
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


BACKORDER_POLICIES = ("refuse", "allow", "notify")


def backorder_policy(product: dict | None) -> str:
    """What this product's seller has said about selling it when the shelf
    is empty: "refuse" (the default and today's behaviour), "allow" or
    "notify".

    Absent, blank or unreadable means REFUSE. That is the whole safety of
    this feature: a catalogue written before the column existed, a product
    somebody imported, or a policy typed wrong all behave exactly as the
    shop behaved yesterday, and nothing starts selling stock it does not
    have because a field was missing.
    """
    if not product:
        return "refuse"
    policy = _text(product.get("backorder_policy")).lower()
    return policy if policy in BACKORDER_POLICIES else "refuse"


def checkout_blockers(items: list[dict], products: dict[str, dict],
                      on_hand: dict[str, Any],
                      *, tracked: set[str] | None = None) -> dict:
    """Everything standing between this basket and an order.

    Returns {"empty", "price_changes", "unavailable", "inactive",
    "backordered", "notify", "can_checkout"}. Gathered in one pass and ALL
    reported together: telling a shopper about one problem, letting them
    fix it, then revealing the next is how a checkout gets abandoned.

    **Out of stock is three answers, not one.** Real shops sell the
    incoming shipment, and a hard refusal on every empty shelf is a shop
    turning away money it could take. The seller decides per product
    (products.backorder_policy):

      refuse  the line blocks the checkout, exactly as it always has.
              The default, so a catalogue that has never heard of this
              field behaves identically.
      allow   the line is ACCEPTED and lands in `backordered`. The order
              is placed, the customer is told, and the goods ship when
              stock exists -- never before.
      notify  the line still blocks, and lands in `notify` as well, so
              the shop can record the interest and tell somebody when it
              is back. Refusing while remembering beats refusing and
              forgetting, which is the current behaviour and loses the
              customer twice.

    `backordered` does NOT stop the checkout; `notify` and `unavailable`
    do. A notify line appears in both lists on purpose: it is a blocker
    that also happens to be worth writing down, and a caller that only
    read `unavailable` still refuses correctly.
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

    refused, backordered, notify = [], [], []
    for line in short:
        policy = backorder_policy(products.get(line["product_id"]))
        stamped = {**line, "backorder_policy": policy}
        if policy == "allow":
            backordered.append(stamped)
            continue
        refused.append(stamped)
        if policy == "notify":
            notify.append(stamped)

    return {
        "empty": empty,
        "price_changes": changes,
        "unavailable": refused,
        "inactive": inactive,
        "backordered": backordered,
        "notify": notify,
        "can_checkout": not (empty or changes or refused or inactive),
    }
