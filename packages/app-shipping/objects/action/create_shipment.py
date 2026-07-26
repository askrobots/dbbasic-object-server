"""action_create_shipment -- the box, before it is a box.

POST {order_id, lines?: [{order_line_id, quantity}], carrier?, service?,
      notes?, today?}

Defaults to EVERYTHING still unshipped on the order, because that is what
the overwhelming majority of shipments are: a small shop picks the whole
order, packs it, and sends it. Passing `lines` is the partial case -- two
of the three mugs are here and the third is on back-order -- and the
remainder simply stays unshipped until a second shipment carries it. That
is the entire mechanism behind "partial fulfillment": a second document,
not a flag, and orders.status follows from counting rather than from
somebody remembering to set it.

Every blocker is reported TOGETHER, the way checkout does it. Revealing
one problem, letting the packer fix it, then revealing the next is how a
warehouse screen gets abandoned in favour of a spreadsheet.

Nothing left to ship is deliberately NOT an error. A double-clicked
"Ship it" button, or a handler that fires twice, must not produce a 500
and must not produce a second empty shipment: it returns ok with a note
saying the order is fully shipped already. Idempotency by observable
state, which is the only kind that survives a retry (docs/logic-decisions.md #7).

The over-shipping arithmetic here is a deliberate second copy of
hook_shipment_lines' gate, named in both files: trusted server-side
writes bypass hooks by design, so the action that writes the lines has to
carry the check, and the hook has to keep it for the generic HTTP write
path. What the action adds is the friendly all-at-once report; what the
hook adds is that no other door is open.
"""

import os
from datetime import date
from decimal import Decimal, InvalidOperation

import object_ids
import object_records
import object_stock

ACTOR = "action_create_shipment"

# Statuses whose lines never reached the customer: the order still owes
# those goods, so they do not consume the ordered quantity.
NOT_DELIVERED_STATUSES = {"lost", "returned_to_sender"}

SERVICES = ("ground", "express", "freight", "pickup", "digital", "other")


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _quantity(value):
    try:
        return Decimal(_text(value) or "0")
    except InvalidOperation:
        return None


def _number(value):
    return format(value.normalize(), "f")


def _truthy(value):
    return _text(value).strip().lower() in ("true", "1", "yes", "on")


def _remaining_by_line(base, order_id, order_lines):
    """How much of each order line is still owed: ordered minus everything
    already sitting on a shipment that has not been lost or bounced back.
    """
    try:
        shipments = object_records.read_collection_records("shipments",
                                                           base_dir=base)
    except Exception:
        shipments = []
    counted = {row["id"] for row in shipments
               if _text(row.get("status")) not in NOT_DELIVERED_STATUSES}
    try:
        lines = object_records.read_collection_records("shipment_lines",
                                                       base_dir=base)
    except Exception:
        lines = []

    shipped = {}
    for line in lines:
        if _text(line.get("shipment_id")) not in counted:
            continue
        key = _text(line.get("order_line_id"))
        shipped[key] = shipped.get(key, Decimal(0)) + (
            _quantity(line.get("quantity")) or Decimal(0))

    # A BACKORDERED line is one the shop openly agreed it did not have. It
    # is owed, not packable: putting it in a parcel would send the customer
    # a short box AND drive the stock ledger negative on goods nobody could
    # pick, breaking both halves of the promise `backorder_policy: allow`
    # makes. So its shippable remainder is capped at what is actually on
    # the shelf -- the rest simply stays owed until stock arrives and a
    # second shipment carries it, which is the ordinary partial-fulfilment
    # path this action already supports.
    on_hand = _available_by_product(base, order_lines)

    remaining = {}
    for line in order_lines:
        ordered = _quantity(line.get("quantity")) or Decimal(0)
        owed = ordered - shipped.get(line["id"], Decimal(0))
        if _truthy(line.get("backordered")):
            product_id = _text(line.get("product_id"))
            available = on_hand.get(product_id, Decimal(0))
            owed = min(owed, available if available > 0 else Decimal(0))
            on_hand[product_id] = available - owed
        remaining[line["id"]] = owed
    return remaining


def _available_by_product(base, order_lines):
    """On-hand quantity for the products this order touches.

    Read once for the whole order rather than per line, and only consulted
    for backordered lines -- an ordinary line's shippable quantity is what
    was ordered, exactly as before, because deciding availability at
    pack time for every line would quietly turn this action into a second
    stock gate competing with the one at checkout.
    """
    available = {}
    for line in order_lines:
        product_id = _text(line.get("product_id"))
        if not product_id or product_id in available:
            continue
        try:
            available[product_id] = object_stock.total_quantity(
                product_id, base_dir=base)
        except Exception:
            available[product_id] = Decimal(0)
    return available


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

    order_status = _text(order.get("status"))
    if order_status in ("draft", "cancelled"):
        return {"status": 409,
                "error": (f"This order is {order_status}. Commit before you "
                          f"pack -- shipping against a draft would send goods "
                          f"for a sale nobody has agreed to, and against a "
                          f"cancelled one would send them to somebody who "
                          f"asked us not to.")}

    try:
        all_lines = object_records.read_collection_records("order_lines",
                                                           base_dir=base)
    except Exception:
        all_lines = []
    order_lines = [line for line in all_lines
                   if _text(line.get("order_id")) == order_id]
    if not order_lines:
        return {"status": 409,
                "error": "This order has no lines, so there is nothing to put "
                         "in a box."}

    by_id = {line["id"]: line for line in order_lines}
    remaining = _remaining_by_line(base, order_id, order_lines)

    requested = request.get("lines")
    blockers = {"unknown_lines": [], "over_ship": [], "bad_quantities": []}

    if isinstance(requested, list) and requested:
        wanted = []
        for entry in requested:
            if not isinstance(entry, dict):
                blockers["bad_quantities"].append(
                    {"order_line_id": "", "reason": "line is not an object"})
                continue
            line_id = _text(entry.get("order_line_id"))
            if line_id not in by_id:
                blockers["unknown_lines"].append(line_id)
                continue
            quantity = _quantity(entry.get("quantity"))
            if quantity is None or quantity <= 0:
                blockers["bad_quantities"].append(
                    {"order_line_id": line_id,
                     "quantity": _text(entry.get("quantity")),
                     "reason": "a shipment line must ship a positive quantity"})
                continue
            left = remaining.get(line_id, Decimal(0))
            if quantity > left:
                ordered = _quantity(by_id[line_id].get("quantity")) or Decimal(0)
                blockers["over_ship"].append({
                    "order_line_id": line_id,
                    "description": _text(by_id[line_id].get("description")),
                    "ordered": _number(ordered),
                    "already_on_shipments": _number(ordered - left),
                    "asked_for": _number(quantity),
                    "would_make": _number(ordered - left + quantity),
                })
                continue
            wanted.append((by_id[line_id], quantity))
    else:
        # The default and the common case: everything still owed.
        wanted = [(by_id[line_id], left)
                  for line_id, left in remaining.items() if left > 0]
        wanted.sort(key=lambda pair: _text(pair[0].get("description")))

    if any(blockers.values()):
        return {"status": 409,
                "error": "Some of those lines cannot be shipped.",
                **blockers}

    if not wanted:
        # Not an error. A second click, or a handler that fired twice, must
        # not 500 and must not raise an empty second shipment.
        return {"ok": True, "order_id": order_id, "shipment_id": "",
                "lines": 0,
                "note": "nothing left to ship on this order; every ordered "
                        "quantity is already on a shipment"}

    owner = (_text(order.get("owner_id"))
             or _text((request.get("_identity") or {}).get("user_id")))
    service = _text(request.get("service")) or "ground"
    if service not in SERVICES:
        return {"status": 400,
                "error": f"Unknown service {service!r}; "
                         f"expected one of {', '.join(SERVICES)}."}

    shipment_id = object_ids.new_uuid4()
    today = _text(request.get("today")) or date.today().isoformat()
    notes = _text(request.get("notes"))
    provenance = f"Generated by {ACTOR} [orders/{order_id}]"

    object_records.create_collection_record(
        "shipments",
        {
            "id": shipment_id,
            "order_id": order_id,
            "direction": "outbound",
            "status": "open",
            "carrier": _text(request.get("carrier")),
            "service": service,
            # Stamped, never re-read: an address is what it was when the box
            # left. .get() throughout because the storefront does not collect
            # a shipping address yet -- a shipment that says nothing is
            # honest, one that invents a plausible address is not.
            "ship_to_name": (_text(order.get("customer_name"))
                             or _text(order.get("customer_email"))),
            "ship_to_address": (_text(order.get("ship_to_address"))
                                or _text(order.get("shipping_address"))
                                or _text(order.get("customer_address"))),
            "notes": f"{notes}\n{provenance}".strip() if notes else provenance,
            "owner_id": owner,
            "entity_id": _text(order.get("entity_id")),
        },
        base_dir=base, actor=ACTOR)

    created = []
    for order_line, quantity in wanted:
        line_id = object_ids.new_uuid4()
        object_records.create_collection_record(
            "shipment_lines",
            {
                "id": line_id,
                "shipment_id": shipment_id,
                "order_line_id": order_line["id"],
                "product_id": _text(order_line.get("product_id")),
                # Stamped from the order line: a slip printed next year must
                # still say what the packer actually put in the box.
                "description": _text(order_line.get("description")),
                "quantity": _number(quantity),
                "owner_id": owner,
            },
            base_dir=base, actor=ACTOR)
        created.append({"id": line_id, "order_line_id": order_line["id"],
                        "description": _text(order_line.get("description")),
                        "quantity": _number(quantity)})

    still_owed = sum(
        (left for line_id, left in remaining.items() if left > 0),
        Decimal(0)) - sum((q for _, q in wanted), Decimal(0))

    return {"ok": True, "order_id": order_id, "shipment_id": shipment_id,
            "status_of_shipment": "open", "lines": len(created),
            "shipment_lines": created,
            "still_unshipped": _number(still_owed),
            "slip_path": f"/shipments/{shipment_id}/slip",
            "date": today,
            "note": "shipment raised open; stock moves when it reaches "
                    "shipped, one move per line"}
