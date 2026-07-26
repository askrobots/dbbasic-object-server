"""system_receiving_attention -- purchase orders somebody shorted us on.

COUNT {} -> {count, detail}

A purchase order at `partial` is a supplier who sent some of it. Nobody
typed that status: `system_receipt_posting` derives it by counting
receipt lines against order lines, which means the fact is already
computed, already correct, and already thrown away -- the PO sits at
`partial` and no screen on this server ever mentions it again. Somebody
has to chase the balance or close the order, and neither happens until
somebody remembers.

The detail names the outstanding QUANTITY, because that is the question
the buyer actually asks. Three POs short by one unit each is a phone call
worth making on Friday; three short by two hundred is one worth making
now, and the row count cannot tell those apart. The arithmetic is the
same as `action_receive_goods._outstanding_by_line` -- ordered minus
everything received on a receipt that was not cancelled -- read here
rather than shared, because these two files must be free to disagree
about nothing else.

COUNT rather than GET or POST: the verb is the contract with the daemon's
attention pass. Nothing here writes. Missing collections read as zero;
a genuine failure raises, so the rollup records it and keeps the last
count rather than reporting a warehouse with nothing outstanding.
"""

import os
from decimal import Decimal, InvalidOperation

import object_records

ACTOR = "system_receiving_attention"

# Some of it arrived and some of it did not. `open`/`confirmed` are still
# entirely owed and nobody has been shorted yet; `received` is complete;
# `cancelled` is off the table.
SHORT_STATUS = "partial"

# A receipt abandoned before anything was counted describes no goods, so
# its lines do not consume the ordered quantity (the same rule
# action_receive_goods applies).
NOT_RECEIVED_STATUSES = {"cancelled"}


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _quantity(value):
    try:
        return Decimal(_text(value) or "0")
    except InvalidOperation:
        # A hand-edited quantity contributes nothing rather than taking
        # the whole count down with it.
        return Decimal(0)


def _rows(collection):
    try:
        return object_records.read_collection_records(collection, base_dir=_base_dir())
    except Exception:
        return []


def _number(value):
    return format(value.normalize(), "f")


def COUNT(request):
    short = {row["id"]: row for row in _rows("orders")
             if _text(row.get("doc_type") or "sale") == "purchase"
             and _text(row.get("status")) == SHORT_STATUS}
    if not short:
        return {"count": 0}

    counted = {row["id"] for row in _rows("receipts")
               if _text(row.get("status")) not in NOT_RECEIVED_STATUSES}
    received = {}
    for line in _rows("receipt_lines"):
        if _text(line.get("receipt_id")) not in counted:
            continue
        key = _text(line.get("order_line_id"))
        received[key] = received.get(key, Decimal(0)) + _quantity(line.get("quantity_received"))

    outstanding = Decimal(0)
    for line in _rows("order_lines"):
        if _text(line.get("order_id")) not in short:
            continue
        remaining = _quantity(line.get("quantity")) - received.get(line["id"], Decimal(0))
        if remaining > 0:
            outstanding += remaining

    detail = f"{_number(outstanding)} units still owed" if outstanding > 0 else ""
    return {"count": len(short), "detail": detail}
