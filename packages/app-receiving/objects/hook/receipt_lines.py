"""Pre-write hook for receipt_lines: you cannot receive what was not
ordered, and you cannot add to a delivery that has already been signed for.

Four gates, all of them things the schema has no way to say.

**Cumulative, not per-line.** The interesting failure is never one line
claiming ten of an order line that ordered eight -- it is the second
delivery quietly claiming three more after the first already took eight.
So the count is over EVERY receipt line pointing at that order line, and
the refusal shows all three numbers ("ordered 8, already received 8, this
would make 11"), because a gate that only says "no" leaves the person on
the dock guessing which of the two deliveries is the wrong one. Receiving
more than was ordered is not a clerical curiosity: it is the shape of an
over-shipment nobody agreed to pay for, and finding it at the door is
cheap where finding it at invoice-matching time is not.

Lines belonging to CANCELLED receipts do not count. A receipt raised and
then abandoned before anything was counted describes no goods, and a shop
that had to re-check-in a delivery after voiding a mistyped receipt would
otherwise be told it was over-receiving on the second attempt -- refused
by its own paperwork for doing exactly the right thing.

**A settled receipt is settled.** Once a receipt is `received`, its lines
are the record of what the driver handed over. A carton found at the back
of the van is a NEW receipt against the same PO, not a retroactive edit to
what was already signed for; otherwise the delivery note in the supplier's
file and the row in the database stop agreeing, and only one of them can
be shown to a judge.

**Quantities must not be negative.** A negative receipt line would be a
correction pretending to be a fact (docs/logic-decisions.md #3); the
correction for over-counting a pallet is an adjustment move, which says
what it is.

**A line that received nothing must SAY why.** quantity_received of zero
with no rejected quantity and no discrepancy note is indistinguishable
from a half-finished data entry -- and the two need opposite responses:
one is a short delivery to chase the supplier about, the other is a row
somebody has not finished typing. Refusing the ambiguous one is how the
distinction survives to the day the supplier's invoice arrives.

Trusted server-side writes bypass hooks by design
(docs/validation-and-logic.md), so action_receive_goods carries the same
cumulative check itself and reports every blocker at once, checkout-style.
That duplication is deliberate and named in both files: the hook guards
the generic HTTP write path a form or an API client uses, the action
guards the friendly path, and neither can be removed without opening the
other's door. app-shipping's hook/action pair says the same thing about
the sales side; this is the mirror of it.
"""

import os
from decimal import Decimal, InvalidOperation

import object_records

# A receipt abandoned before anything was counted describes no goods, so
# its lines consume none of what was ordered.
NOT_RECEIVED_STATUSES = {"cancelled"}

# Once a receipt is in one of these, the delivery has been signed for and
# its contents are history, not a draft.
SETTLED_STATUSES = {"received"}


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _quantity(value):
    """Decimal, never a bare float: quantities may be fractional (1.5 kg of
    coffee) and binary-float arithmetic would make an exact-fit delivery
    round itself into an over-receipt."""
    try:
        return Decimal(_text(value) or "0")
    except InvalidOperation:
        return None


def _number(value):
    """Render a Decimal the way a human wrote it: 8 not 8.000, 1.5 not 1.50.
    The refusal message is read by whoever has to fix the receipt, so the
    numbers in it have to look like the numbers on their screen."""
    return format(value.normalize(), "f")


def BEFORE_WRITE(request):
    action = _text(request.get("action"))
    if action not in ("create", "update"):
        return None
    record = request.get("record") or {}
    base = _base_dir()

    received = _quantity(record.get("quantity_received"))
    rejected = _quantity(record.get("quantity_rejected"))
    if received is None or rejected is None:
        return None                 # schema validation owns the type error
    if received < 0 or rejected < 0:
        return {"error": ("A receipt line cannot receive or reject a negative "
                          "quantity. Nothing arrived is what a zero means, and "
                          "a miscount is corrected by an adjustment move, "
                          "never by a negative row."),
                "status": 400}

    if received == 0 and rejected == 0 and not _text(
            record.get("discrepancy_note")):
        return {"error": ("This line received nothing and says nothing about "
                          "why. A short delivery has to be recorded as one -- "
                          "give a rejected quantity or a discrepancy note, "
                          "because a blank zero is indistinguishable from a "
                          "row somebody has not finished typing, and the two "
                          "need opposite answers when the supplier's invoice "
                          "arrives."),
                "status": 400}

    receipt_id = _text(record.get("receipt_id"))
    try:
        receipt = object_records.get_collection_record(
            "receipts", receipt_id, base_dir=base)
    except Exception:
        return None                 # relation validation owns a missing receipt
    receipt_status = _text(receipt.get("status"))
    if receipt_status in SETTLED_STATUSES:
        return {"error": (f"Receipt {receipt_id} is already {receipt_status}. "
                          f"A delivery is settled once it has been signed for "
                          f"-- a carton found afterwards is a NEW receipt "
                          f"against the same purchase order, not a change to "
                          f"what the driver already handed over."),
                "status": 409}

    order_line_id = _text(record.get("order_line_id"))
    try:
        order_line = object_records.get_collection_record(
            "order_lines", order_line_id, base_dir=base)
    except Exception:
        return {"error": (f"No such order line: {order_line_id or '(blank)'}. "
                          f"A receipt line has to say which ordered thing "
                          f"turned up, or nothing can tell whether the "
                          f"supplier sent what we bought."),
                "status": 400}

    ordered = _quantity(order_line.get("quantity")) or Decimal(0)

    try:
        existing_lines = object_records.read_collection_records(
            "receipt_lines", base_dir=base)
    except Exception:
        return {"error": ("Receipt history unreadable; refusing to receive "
                          "against an unknown history."), "status": 503}
    try:
        receipts = {row["id"]: row for row in
                    object_records.read_collection_records("receipts",
                                                           base_dir=base)}
    except Exception:
        receipts = {}

    record_id = _text(record.get("id"))
    already = Decimal(0)
    for line in existing_lines:
        if _text(line.get("order_line_id")) != order_line_id:
            continue
        if record_id and _text(line.get("id")) == record_id:
            continue           # an update replaces its own earlier quantity
        parent = receipts.get(_text(line.get("receipt_id"))) or {}
        if _text(parent.get("status")) in NOT_RECEIVED_STATUSES:
            continue
        already += _quantity(line.get("quantity_received")) or Decimal(0)

    would_be = already + received
    if would_be > ordered:
        return {"error": (f"That would receive more than was ordered: ordered "
                          f"{_number(ordered)}, already received "
                          f"{_number(already)}, this would make "
                          f"{_number(would_be)}. Amend the purchase order "
                          f"first if the supplier really sent more, so what "
                          f"we agreed to pay for and what is on the shelf go "
                          f"on saying the same thing."),
                "status": 409}

    return None
