"""action_dropship_order -- the vendor ships straight to my customer.

POST {order_id, vendor_id, vendor_name?, vendor_price_cents?,
      prices?: {order_line_id: unit_price_cents}, number?, today?}

This is the composition test plan/fulfillment-logistics-spec.md set for
the whole fulfillment build, in its own words: "a sale order flagged
`fulfillment: dropship` produces a linked PURCHASE order to the vendor
carrying the customer's ship-to; receiving is skipped (goods never touch
our shelf); margin is already a read because both orders carry money. If
the shipment model can't express this cleanly, the model is wrong."

It passes, and the reason it passes is a decision made in v1 of this
schema for entirely different reasons: sale and purchase orders share one
collection. A drop-ship is not a new noun and gets no new table -- it is
two rows of a document type that already exists, pointing at each other.
The customer's order says what is owed and to whom; the vendor's order
says what we are buying and where to send it; both carry money, so margin
is subtraction rather than a project.

**The stock rule is the whole test.** A drop-ship order must never move
our stock, and its purchase order must never produce a receipt. Two
halves, and they are honest about being different:

- Receiving is skipped BY CONSTRUCTION. Nothing on this server creates a
  receipt on its own -- system_receipt_posting only ever reacts to a
  `receipts` row a human raised through action_receive_goods -- so a
  drop-ship PO that nobody checks in never touches a shelf. What is
  missing is a REFUSAL: nothing yet stops somebody receiving against a
  drop-ship PO by mistake, and that gate belongs in app-receiving.
- The sale side is NOT yet safe, and this docstring says so rather than
  hiding it. system_order_fulfillment (app-shipping) composes a `sale`
  move for every shipment line the moment a shipment reaches `shipped`,
  and it does not look at fulfillment_source. The order carries the fact
  -- the handler already re-reads the order, so no new plumbing is needed,
  only one condition -- but until that condition exists, recording the
  vendor's dispatch as a shipment against the sale order would decrement
  a shelf that never held the goods. tests/test_dropship.py holds that as
  a strict xfail so the day somebody fixes it, the acceptance test is
  already written.

So this action does the one thing it can do honestly: it writes the fact
both handlers need onto both orders, and it does not go anywhere near
app-shipping's or app-receiving's code to work around them.

**Every blocker is reported together**, checkout-style. Whoever is doing
this is usually deciding between two vendors with a customer waiting, and
revealing one problem at a time is how a screen gets abandoned.

**You cannot drop-ship what you already picked.** If any stock move
already exists against this order's shipments, the goods left our shelf
and the sale is ours to fulfill; raising a PO now would buy a second set
of the same units. The refusal names the moves, because "no" alone leaves
somebody guessing whether they picked the wrong order or the wrong day.

**Exactly one PO per sale order.** A second call finds linked_order_id
already set and refuses with the number of the PO that exists, rather
than quietly raising a duplicate commitment to buy -- the same class of
mistake as a double refund, with a supplier on the other end of it.

**The vendor's price is asked for, never invented.** Explicit per-line
prices win, then a flat vendor_price_cents per unit, then the product's
recorded cost_cents. When none of those exist the PO is raised at zero
with a WARNING naming the lines, because a cost of zero reads as 100%
margin and a cost copied from the sale price reads as none -- both are
lies, and the honest move is to say the number is missing.
"""

import os
from datetime import date
from decimal import Decimal, InvalidOperation

import object_ids
import object_records

ACTOR = "action_dropship_order"

DROPSHIP = "dropship"

# A draft is not a commitment and a cancelled order must not come back to
# life because somebody raised a PO against it. Everything else -- an
# order confirmed, in processing, even partially shipped from stock for
# its OTHER lines -- can still have a vendor take the rest.
UNTOUCHABLE_ORDER_STATUSES = {"draft", "cancelled"}


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _cents(value):
    """None means NOT SAID, which is a different fact from zero.

    The whole vendor-price ladder below turns on that difference: a price
    somebody typed as 0 is a decision, and a price nobody typed is a gap
    to report. Collapsing the two into 0 is how the fallback chain would
    silently stop at its first rung.
    """
    text = _text(value)
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _quantity(value):
    """Decimal, never a bare float -- quantities may be fractional."""
    try:
        return Decimal(_text(value) or "0")
    except InvalidOperation:
        return Decimal(0)


def _number(value):
    return format(value.normalize(), "f")


def _rows(collection, base):
    try:
        return object_records.read_collection_records(collection, base_dir=base)
    except Exception:
        return None


def _vendor_name(base, vendor_id, given):
    """A printable name for the vendor, asked of the places one might be
    filed and never invented.

    vendor_id is plain text precisely because a vendor may be a contact,
    an organization, or neither; so this looks in both and falls back to
    the id itself, which at least identifies the counterparty rather than
    leaving a required field blank.
    """
    if given:
        return given
    for row in _rows("contacts", base) or []:
        if _text(row.get("id")) != vendor_id:
            continue
        full = _text(row.get("full_name"))
        if full:
            return full
        parts = [_text(row.get("first_name")), _text(row.get("last_name"))]
        joined = " ".join(part for part in parts if part)
        if joined:
            return joined
    for row in _rows("organizations", base) or []:
        if _text(row.get("id")) == vendor_id and _text(row.get("name")):
            return _text(row["name"])
    return vendor_id


def _moves_against(base, order_id):
    """Stock moves already composed for this order's shipments.

    The marker system_order_fulfillment stamps is
    `shipments/{shipment_id}:line/{line_id}`, so the honest question is
    asked of the moves themselves rather than of a status somebody could
    have typed: goods that left the shelf are the fact, and the order's
    status is only ever a read of it.
    """
    shipments = _rows("shipments", base)
    moves = _rows("stock_moves", base)
    if not shipments or moves is None:
        return [], []
    ours = [_text(row.get("id")) for row in shipments
            if _text(row.get("order_id")) == order_id]
    if not ours:
        return [], []
    hits = [row for row in moves
            if any(f"shipments/{shipment_id}:" in _text(row.get("reference"))
                   for shipment_id in ours)]
    return ours, hits


def POST(request):
    base = _base_dir()
    order_id = _text(request.get("order_id"))
    if not order_id:
        return {"status": 400, "error": "order_id is required"}

    try:
        order = object_records.get_collection_record("orders", order_id,
                                                     base_dir=base)
    except Exception:
        return {"status": 404, "error": f"No such order: {order_id}"}

    blockers = {"wrong_document": "", "order_status": "", "already_linked": "",
                "stock_moved": [], "missing_vendor": "", "no_lines": ""}

    if _text(order.get("doc_type")) != "sale":
        blockers["wrong_document"] = (
            "That is a purchase order. Drop-shipping is something you do to "
            "a SALE order -- it spawns the purchase order, and a purchase "
            "order spawning another one is a supply chain nobody asked for.")

    status = _text(order.get("status"))
    if status in UNTOUCHABLE_ORDER_STATUSES:
        blockers["order_status"] = (
            f"This order is {status}. A draft is not a commitment and a "
            f"cancelled order must not come back to life because somebody "
            f"raised a purchase order against it -- confirm it first, or "
            f"leave it alone.")

    already = _text(order.get("linked_order_id"))
    if already:
        number = ""
        try:
            number = _text(object_records.get_collection_record(
                "orders", already, base_dir=base).get("number"))
        except Exception:
            number = already
        blockers["already_linked"] = (
            f"This order is already drop-shipped on purchase order {number} "
            f"({already}). One sale order spawns exactly one PO; raising a "
            f"second would commit us to buying the same goods twice, from a "
            f"supplier who will happily send them.")

    shipment_ids, moved = _moves_against(base, order_id)
    if moved:
        blockers["stock_moved"] = [
            {"stock_move_id": _text(row.get("id")),
             "product_id": _text(row.get("product_id")),
             "quantity": _text(row.get("quantity")),
             "reason": _text(row.get("reason")),
             "reference": _text(row.get("reference"))}
            for row in moved]

    vendor_id = _text(request.get("vendor_id"))
    if not vendor_id:
        blockers["missing_vendor"] = (
            "A drop-ship has to name who is shipping it. The purchase order "
            "is a commitment to buy from somebody, and 'a vendor' is not "
            "somebody anybody can send an order to.")

    lines = [row for row in _rows("order_lines", base) or []
             if _text(row.get("order_id")) == order_id]
    if not lines:
        blockers["no_lines"] = (
            "This order has no lines, so there is nothing to buy on its "
            "behalf. A purchase order for an empty order would commit us to "
            "nothing and tell the vendor nothing.")

    if any(blockers.values()):
        return {"status": 409,
                "error": "This order cannot be drop-shipped.",
                "shipments": shipment_ids,
                **blockers}

    # --- the purchase order ------------------------------------------------
    owner = (_text(order.get("owner_id"))
             or _text((request.get("_identity") or {}).get("user_id")))
    when = _text(request.get("today")) or date.today().isoformat()
    purchase_id = object_ids.new_uuid4()
    number = (_text(request.get("number"))
              or f"PO-{_text(order.get('number'))}")
    vendor_name = _vendor_name(base, vendor_id,
                               _text(request.get("vendor_name")))

    object_records.create_collection_record(
        "orders",
        {
            "id": purchase_id,
            "doc_type": "purchase",
            "number": number,
            # The counterparty, exactly as every other purchase order in
            # this repo names it: customer_name on a PO has always been the
            # supplier, and flipping that meaning for one kind of PO would
            # make the order list read differently row by row.
            "customer_name": vendor_name,
            "currency": _text(order.get("currency")) or "USD",
            # Confirmed, not draft: the operator choosing a vendor IS the
            # commitment to buy, and a drop-ship PO sitting in draft is a
            # customer waiting on a parcel nobody has ordered.
            "status": "confirmed",
            "order_date": when,
            "expected_date": _text(order.get("expected_date")),
            "fulfillment_source": DROPSHIP,
            "linked_order_id": order_id,
            "vendor_id": vendor_id,
            # The customer's ship-to, carried onto the vendor's document.
            # This is the one place in this repo where the counterparty on
            # an order and the destination of its goods are different
            # people, and it is why those two fields exist at all.
            "ship_to_name": (_text(order.get("ship_to_name"))
                             or _text(order.get("customer_name"))
                             or _text(order.get("customer_email"))),
            "ship_to_address": _text(order.get("ship_to_address")),
            # The vendor is the packer now, so the packer's instructions and
            # the gift message travel with the goods rather than staying on
            # a document the person packing the box will never see.
            "customer_note": _text(order.get("customer_note")),
            "gift_message": _text(order.get("gift_message")),
            "notes": (f"Generated by {ACTOR} [orders/{order_id}] -- drop-ship "
                      f"to the customer; goods never touch our shelf"),
            "owner_id": owner,
        },
        base_dir=base, actor=ACTOR)

    prices = request.get("prices")
    prices = prices if isinstance(prices, dict) else {}
    flat = _cents(request.get("vendor_price_cents"))
    products = {_text(row.get("id")): row for row in _rows("products", base) or []}

    created = []
    unpriced = []
    for line in lines:
        unit = _cents(prices.get(_text(line.get("id"))))
        if unit is None:
            unit = flat
        if unit is None:
            product = products.get(_text(line.get("product_id"))) or {}
            unit = _cents(product.get("cost_cents"))
        if unit is None or unit <= 0:
            # Zero reads as 100% margin and the sale price reads as none.
            # Both are lies; saying the number is missing is not.
            unit = 0
            unpriced.append(_text(line.get("id")))
        quantity = _quantity(line.get("quantity")) or Decimal(1)
        line_id = object_ids.new_uuid4()
        object_records.create_collection_record(
            "order_lines",
            {
                "id": line_id,
                "order_id": purchase_id,
                "product_id": _text(line.get("product_id")),
                # Stamped from the sale line: what the vendor is being asked
                # to send is what the customer bought, described the way the
                # customer saw it.
                "description": _text(line.get("description")),
                "quantity": _number(quantity),
                "unit_price_cents": str(unit),
                "line_total_cents": str(
                    int((quantity * Decimal(unit)).to_integral_value())),
                "tax_rate_bps": "0",
                "line_tax_cents": "0",
                "owner_id": owner,
            },
            base_dir=base, actor=ACTOR)
        created.append({"order_line_id": _text(line.get("id")),
                        "purchase_line_id": line_id,
                        "quantity": _number(quantity),
                        "unit_price_cents": str(unit)})

    # --- and the sale order learns what it is ------------------------------
    # Both ends carry the fact, so neither has to walk the link to answer
    # "is this a drop-ship?" -- which is the question a stock handler asks,
    # and a handler that had to join to find out would eventually not.
    object_records.update_collection_record(
        "orders", order_id,
        {"fulfillment_source": DROPSHIP, "linked_order_id": purchase_id,
         "vendor_id": vendor_id},
        base_dir=base, actor=ACTOR)

    result = {"ok": True, "order_id": order_id,
              "purchase_order_id": purchase_id, "number": number,
              "vendor_id": vendor_id, "vendor_name": vendor_name,
              "lines": len(created), "purchase_lines": created,
              "ship_to_name": (_text(order.get("ship_to_name"))
                               or _text(order.get("customer_name"))),
              "date": when,
              "note": "the goods never touch our shelf: nothing has moved, no "
                      "receipt is expected, and the vendor's dispatch is the "
                      "fulfillment"}
    if unpriced:
        result["warning"] = (
            f"{len(unpriced)} line(s) carry no vendor price, so the purchase "
            f"order totals zero and margin will read as pure profit until "
            f"somebody types what the vendor actually charges. Pass "
            f"vendor_price_cents or prices, or record cost_cents on the "
            f"product.")
        result["unpriced_lines"] = unpriced
    return result
