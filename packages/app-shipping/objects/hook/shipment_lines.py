"""Pre-write hook for shipment_lines: you cannot ship what was not ordered,
and you cannot add to a box that has already gone.

Three gates, all of them things the schema has no way to say.

**Cumulative, not per-line.** The interesting failure is never one line
claiming five of an order line that ordered three -- it is the second
shipment quietly claiming two more after the first already sent two. So
the count is over EVERY shipment line pointing at that order line, and
the refusal shows all three numbers ("ordered 3, already on shipments 2,
this would make 4"), because a gate that only says "no" leaves a packer
guessing which of the two shipments is the wrong one.

Lines belonging to shipments that were LOST or RETURNED TO SENDER do not
count. Those goods did not reach the customer, the order is still owed
them, and a shop that had to re-send a lost parcel would otherwise be
told it was over-shipping on the replacement -- refused by its own
paperwork for doing exactly the right thing.

**A settled manifest is settled.** Once a shipment is shipped (or beyond),
its lines are a record of what was physically in the box. A forgotten item
is a NEW shipment, not a retroactive edit to what a courier already took;
otherwise the packing slip in the customer's hands and the row in the
database stop agreeing, and only one of them can be shown to a judge.

**Quantity must be positive.** Direction is what the shipment's direction
field is for; a negative shipment line would be a correction pretending to
be a fact (docs/logic-decisions.md #3).

Trusted server-side writes bypass hooks by design (docs/validation-and-logic.md),
so action_create_shipment carries the same cumulative check itself and
reports every blocker at once, checkout-style. That duplication is
deliberate and named in both files: the hook guards the generic HTTP write
path a form or an API client uses, the action guards the friendly path,
and neither can be removed without opening the other's door.
"""

import os
from decimal import Decimal, InvalidOperation

import object_records

# A shipment in one of these never reached the customer, so its lines do
# not consume the order: the goods are still owed and still shippable.
NOT_DELIVERED_STATUSES = {"lost", "returned_to_sender"}

# Once a shipment is in one of these, the box is with a courier or a
# customer and its contents are history, not a draft.
SETTLED_STATUSES = {"shipped", "in_transit", "delivered",
                    "returned_to_sender", "lost"}


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _quantity(value):
    """Decimal, never a bare float: quantities may be fractional (1.5 kg)
    and binary-float arithmetic would make an exact-fit shipment round
    itself into an over-ship."""
    try:
        return Decimal(_text(value) or "0")
    except InvalidOperation:
        return None


def _number(value):
    """Render a Decimal the way a human wrote it: 3 not 3.000, 1.5 not 1.50.
    The refusal message is read by whoever has to fix the shipment, so the
    numbers in it have to look like the numbers on their screen."""
    quantized = value.normalize()
    text = format(quantized, "f")
    return text


def BEFORE_WRITE(request):
    action = _text(request.get("action"))
    if action not in ("create", "update"):
        return None
    record = request.get("record") or {}
    base = _base_dir()

    quantity = _quantity(record.get("quantity"))
    if quantity is None:
        return None                 # schema validation owns the type error
    if quantity <= 0:
        return {"error": ("A shipment line must ship a positive quantity. "
                          "Nothing physically moved is what an absent line "
                          "means, and a correction is a new document, never "
                          "a negative row."),
                "status": 400}

    shipment_id = _text(record.get("shipment_id"))
    try:
        shipment = object_records.get_collection_record(
            "shipments", shipment_id, base_dir=base)
    except Exception:
        return None                 # relation validation owns a missing shipment
    shipment_status = _text(shipment.get("status"))
    if shipment_status in SETTLED_STATUSES:
        return {"error": (f"Shipment {shipment_id} is already "
                          f"{shipment_status}. A manifest is settled once it "
                          f"leaves the dock -- a forgotten item is a NEW "
                          f"shipment against the same order, not a change to "
                          f"what the courier already took."),
                "status": 409}

    order_line_id = _text(record.get("order_line_id"))
    try:
        order_line = object_records.get_collection_record(
            "order_lines", order_line_id, base_dir=base)
    except Exception:
        return {"error": (f"No such order line: {order_line_id or '(blank)'}. "
                          f"A shipment line has to say which ordered thing it "
                          f"satisfies, or nothing can tell whether the "
                          f"customer got what they paid for."),
                "status": 400}

    ordered = _quantity(order_line.get("quantity")) or Decimal(0)

    try:
        existing_lines = object_records.read_collection_records(
            "shipment_lines", base_dir=base)
    except Exception:
        return {"error": ("Shipment history unreadable; refusing to ship "
                          "against an unknown history."), "status": 503}
    try:
        shipments = {row["id"]: row for row in
                     object_records.read_collection_records("shipments",
                                                            base_dir=base)}
    except Exception:
        shipments = {}

    record_id = _text(record.get("id"))
    already = Decimal(0)
    for line in existing_lines:
        if _text(line.get("order_line_id")) != order_line_id:
            continue
        if record_id and _text(line.get("id")) == record_id:
            continue           # an update replaces its own earlier quantity
        parent = shipments.get(_text(line.get("shipment_id"))) or {}
        if _text(parent.get("status")) in NOT_DELIVERED_STATUSES:
            continue
        already += _quantity(line.get("quantity")) or Decimal(0)

    would_be = already + quantity
    if would_be > ordered:
        return {"error": (f"That would ship more than was ordered: ordered "
                          f"{_number(ordered)}, already on shipments "
                          f"{_number(already)}, this would make "
                          f"{_number(would_be)}. Add the extra to the order "
                          f"first if the customer really wants more."),
                "status": 409}

    return None
