"""action_receive_goods -- the pallet, turned into a fact.

POST {order_id, lines?: [{order_line_id, quantity_received,
      quantity_rejected?, discrepancy_note?}], supplier_reference?,
      received_on?}

Defaults to EVERYTHING still outstanding on the purchase order, at the
full expected quantity, because that is what the overwhelming majority of
deliveries are: the supplier sent what was ordered and somebody wants one
button that says so. Passing `lines` is the interesting case -- eight of
the ten arrived and two cartons were crushed -- and the remainder simply
stays outstanding until a second delivery carries it. That is the entire
mechanism behind a partly-received PO: a second document, not a flag, and
orders.status follows from counting rather than from somebody remembering
to set it.

**A sales order is refused in words**, not with a validation code. Goods
LEAVE on a shipment and ARRIVE on a receipt; the two documents share a
schema (orders.doc_type) precisely so that the direction has to be stated
rather than assumed, and somebody who reached this action with an SO has a
model of the system worth correcting on the spot.

Every blocker is reported TOGETHER, the way checkout does it. Revealing
one problem, letting the receiver fix it, then revealing the next is how a
warehouse screen gets abandoned in favour of a spreadsheet -- and the
person doing this is standing up, in the cold, with a driver waiting.

Nothing outstanding is deliberately NOT an error. A double-clicked
"Receive" button, or a handler that fires twice, must not produce a 500
and must not produce a second empty receipt: it returns ok with a note
saying the PO is fully received already. Idempotency by observable state,
which is the only kind that survives a retry (docs/logic-decisions.md #7).

The receipt is raised `open` and then moved to `received` in the same
pass, rather than created `received` outright: the ladder is what
system_receipt_posting reacts to, and a document that was never open for
even an instant would make the check-in-in-progress state -- a real state,
where a pallet is on the floor and half-counted -- unreachable through the
one path everybody uses.

The over-receiving arithmetic here is a deliberate second copy of
hook_receipt_lines' gate, named in both files: trusted server-side writes
bypass hooks by design, so the action that writes the lines has to carry
the check, and the hook has to keep it for the generic HTTP write path.
What the action adds is the friendly all-at-once report; what the hook
adds is that no other door is open.
"""

import os
from datetime import date
from decimal import Decimal, InvalidOperation

import object_ids
import object_records

ACTOR = "action_receive_goods"

# A receipt abandoned before anything was counted describes no goods, so
# its lines do not consume the ordered quantity.
NOT_RECEIVED_STATUSES = {"cancelled"}


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


def _outstanding_by_line(base, order_lines):
    """How much of each PO line is still owed by the supplier: ordered minus
    everything already received on a receipt that was not cancelled.
    """
    try:
        receipts = object_records.read_collection_records("receipts",
                                                          base_dir=base)
    except Exception:
        receipts = []
    counted = {row["id"] for row in receipts
               if _text(row.get("status")) not in NOT_RECEIVED_STATUSES}
    try:
        lines = object_records.read_collection_records("receipt_lines",
                                                       base_dir=base)
    except Exception:
        lines = []

    received = {}
    for line in lines:
        if _text(line.get("receipt_id")) not in counted:
            continue
        key = _text(line.get("order_line_id"))
        received[key] = received.get(key, Decimal(0)) + (
            _quantity(line.get("quantity_received")) or Decimal(0))

    outstanding = {}
    for line in order_lines:
        ordered = _quantity(line.get("quantity")) or Decimal(0)
        outstanding[line["id"]] = ordered - received.get(line["id"], Decimal(0))
    return outstanding


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

    doc_type = _text(order.get("doc_type")) or "sale"
    if doc_type != "purchase":
        return {"status": 409,
                "error": ("This is a sales order -- goods leave on a shipment, "
                          "they do not arrive on a receipt. If a customer is "
                          "sending something back, that is an inbound shipment "
                          "against this order, not a receipt against a "
                          "purchase order.")}

    order_status = _text(order.get("status"))
    if order_status in ("draft", "cancelled"):
        return {"status": 409,
                "error": (f"This purchase order is {order_status}. Commit "
                          f"before you receive -- booking goods in against a "
                          f"draft would create stock for a purchase nobody has "
                          f"agreed to, and against a cancelled one would "
                          f"quietly accept a delivery we told the supplier to "
                          f"stop.")}

    try:
        all_lines = object_records.read_collection_records("order_lines",
                                                           base_dir=base)
    except Exception:
        all_lines = []
    order_lines = [line for line in all_lines
                   if _text(line.get("order_id")) == order_id]
    if not order_lines:
        return {"status": 409,
                "error": "This purchase order has no lines, so there is "
                         "nothing anybody could be delivering against it."}

    by_id = {line["id"]: line for line in order_lines}
    outstanding = _outstanding_by_line(base, order_lines)

    requested = request.get("lines")
    blockers = {"unknown_lines": [], "over_receive": [], "bad_quantities": []}

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
            received = _quantity(entry.get("quantity_received"))
            rejected = _quantity(entry.get("quantity_rejected"))
            note = _text(entry.get("discrepancy_note"))
            if received is None or rejected is None:
                blockers["bad_quantities"].append(
                    {"order_line_id": line_id,
                     "quantity_received": _text(entry.get("quantity_received")),
                     "reason": "quantities must be numbers"})
                continue
            if received < 0 or rejected < 0:
                blockers["bad_quantities"].append(
                    {"order_line_id": line_id,
                     "quantity_received": _number(received),
                     "reason": "a receipt line cannot receive or reject a "
                               "negative quantity"})
                continue
            if received == 0 and rejected == 0 and not note:
                blockers["bad_quantities"].append(
                    {"order_line_id": line_id,
                     "quantity_received": "0",
                     "reason": "a line that received nothing must say why: "
                               "give a rejected quantity or a discrepancy "
                               "note"})
                continue
            left = outstanding.get(line_id, Decimal(0))
            if received > left:
                ordered = _quantity(by_id[line_id].get("quantity")) or Decimal(0)
                blockers["over_receive"].append({
                    "order_line_id": line_id,
                    "description": _text(by_id[line_id].get("description")),
                    "ordered": _number(ordered),
                    "already_received": _number(ordered - left),
                    "asked_for": _number(received),
                    "would_make": _number(ordered - left + received),
                })
                continue
            wanted.append((by_id[line_id], received, rejected, note))
    else:
        # The default and the common case: the supplier sent what was
        # ordered, and everything still outstanding turned up today.
        wanted = [(by_id[line_id], left, Decimal(0), "")
                  for line_id, left in outstanding.items() if left > 0]
        wanted.sort(key=lambda entry: _text(entry[0].get("description")))

    if any(blockers.values()):
        return {"status": 409,
                "error": "Some of those lines cannot be received.",
                **blockers}

    if not wanted:
        # Not an error. A second click, or a handler that fired twice, must
        # not 500 and must not raise an empty second receipt.
        return {"ok": True, "order_id": order_id, "receipt_id": "", "lines": 0,
                "note": "nothing outstanding on this purchase order; every "
                        "ordered quantity has already been received"}

    owner = (_text(order.get("owner_id"))
             or _text((request.get("_identity") or {}).get("user_id")))
    receipt_id = object_ids.new_uuid4()
    received_on = _text(request.get("received_on")) or date.today().isoformat()
    provenance = f"Generated by {ACTOR} [orders/{order_id}]"

    object_records.create_collection_record(
        "receipts",
        {
            "id": receipt_id,
            "order_id": order_id,
            "status": "open",
            "received_on": received_on,
            "supplier_reference": _text(request.get("supplier_reference")),
            "notes": provenance,
            "owner_id": owner,
            "entity_id": _text(order.get("entity_id")),
        },
        base_dir=base, actor=ACTOR)

    created = []
    for order_line, received, rejected, note in wanted:
        line_id = object_ids.new_uuid4()
        expected = _quantity(order_line.get("quantity")) or Decimal(0)
        object_records.create_collection_record(
            "receipt_lines",
            {
                "id": line_id,
                "receipt_id": receipt_id,
                "order_line_id": order_line["id"],
                "product_id": _text(order_line.get("product_id")),
                # Stamped from the order line: a sheet printed next year must
                # still say what the dock counted off the pallet.
                "description": _text(order_line.get("description")),
                "quantity_expected": _number(expected),
                "quantity_received": _number(received),
                "quantity_rejected": _number(rejected),
                "discrepancy_note": note,
                "owner_id": owner,
            },
            base_dir=base, actor=ACTOR)
        created.append({"id": line_id, "order_line_id": order_line["id"],
                        "description": _text(order_line.get("description")),
                        "quantity_expected": _number(expected),
                        "quantity_received": _number(received),
                        "quantity_rejected": _number(rejected)})

    # Open first, then received: the ladder is what system_receipt_posting
    # reacts to, and check-in-in-progress is a real state this path must not
    # make unreachable.
    object_records.update_collection_record(
        "receipts", receipt_id, {"status": "received"},
        base_dir=base, actor=ACTOR)

    still_owed = sum(
        (left for left in outstanding.values() if left > 0),
        Decimal(0)) - sum((received for _, received, _, _ in wanted), Decimal(0))
    rejected_total = sum((rejected for _, _, rejected, _ in wanted), Decimal(0))

    return {"ok": True, "order_id": order_id, "receipt_id": receipt_id,
            "status_of_receipt": "received", "lines": len(created),
            "receipt_lines": created,
            "still_outstanding": _number(still_owed),
            "rejected": _number(rejected_total),
            "sheet_path": f"/purchase-orders/{order_id}/receive",
            "received_on": received_on,
            "note": "receipt signed off; stock moves one purchase move per "
                    "line for what was received, and nothing for what was "
                    "rejected"}
