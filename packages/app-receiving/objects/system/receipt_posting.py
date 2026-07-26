"""system_receipt_posting -- the pallet landed, so the shelf is heavier and
the purchase order says so.

HANDLES receipts and receipt_lines writes. Two jobs, both of them reads
turned into facts, mirroring system_order_fulfillment on the sales side.

**Stock moves when goods physically move.** One `purchase` move PER
RECEIPT LINE at the moment a receipt reaches `received`, from
receiving.supplier_location to receiving.stock_location (falling back to
shop.stock_location, because a one-warehouse shop has already configured
that and should not have to say the same thing twice). Per line rather
than per receipt because a level is a fold over moves per product, and a
delivery routinely carries several products with different fates.

**Rejected quantities compose NOTHING.** Goods being sent back never
entered stock: the carton was opened at the door, judged crushed, and put
straight on the driver's trolley. A move for it would put damaged goods on
the shelf and then require a compensating move to take them off again --
two lies that happen to cancel, which is worse than one honest silence.
The rejection is still RECORDED, on the receipt line, where the argument
with the supplier can find it.

**unit_cost_cents is stamped from the PO line's unit_price_cents.** This
is the move that makes inventory valuation possible later: a `sale` move
today carries no cost at all, which is exactly why COGS-on-sale cannot be
composed (see tests/test_cogs_on_sale.py, which specifies that gap as a
strict xfail -- its point 3 says in as many words that "nothing stamps
that cost on a sale move today, and where a sale move carries no cost, no
honest journal can be composed at all"). FIFO or weighted average both
need a cost on the way IN, per layer, per location; stamping the price we
actually agreed to pay at the moment the goods arrive is the input both
methods consume, and it costs nothing to record now and cannot be
reconstructed later once the supplier's price list has moved on. Choosing
BETWEEN FIFO and average stays out of this handler on purpose -- picking a
valuation method by accident inside a receiving reaction would be worse
than the current silence.

**The purchase order's status is derived, never set.** Some of the ordered
quantity received is `partial`; all of it is `received`
(docs/logic-decisions.md #1: the QUANTITIES on receipt lines are the
facts, the order's status is a read of them). A human never types either
one and no surface computes it at read time; it is stored, so the PO list,
the receiving sheet and a report cannot disagree.

`received` is a NEW value on the shared SO/PO status enum (orders v3), and
it is deliberately not a reuse of `delivered`. `delivered` on a purchase
order reads as a claim about something WE sent -- in a mixed order list,
filtered by status rather than by doc_type, it is actively misleading, and
"the goods completed their journey" is a gloss nobody applies at a glance.
`received` can only ever mean one thing in either direction: the goods
turned up HERE. The spec says the same (plan/fulfillment-logistics-spec.md:
"PO status derives: partial | received"), and the cost of the alternative
is a word that means the opposite of itself depending on a field two
columns away.

Placement follows docs/logic-decisions.md #6 -- a REACTION, post-commit,
best-effort, never blocking the write that triggered it. A dock that
cannot record a stock move must still be able to sign for the delivery;
the discrepancy is visible and fixable, whereas a refused receipt is a
driver standing at the door with a pallet nobody can accept.

Idempotency by provenance (#7): each move is stamped
`receipts/{receipt_id}:line/{line_id}` in its reference, PER LINE. A
replayed event -- and events are replayed here, by design: the change
dispatcher promises at-least-once (object_change_dispatch.py) -- finds its
own marker and moves nothing. Per line and not per receipt because a
receipt can gain a line while still open, and a receipt-level marker would
silently skip the goods added after the first pass.

Missing settings do not cost anybody the record that goods arrived. If no
stock location is configured the receipt still stands, the PO status still
derives, and the gap is reported in `warning` -- the same posture
system_order_fulfillment has always had, for the same reason: a missing
location is a five-second fix, and a lost delivery record is not.
"""

import os
from datetime import date
from decimal import Decimal, InvalidOperation

import object_ids
import object_records

HANDLES = [
    "receipts.record.created",
    "receipts.record.updated",
    "receipt_lines.record.created",
]

ACTOR = "system_receipt_posting"

# The delivery has been signed for: the goods are ours and on the shelf.
RECEIVED_STATUSES = {"received"}

# A receipt abandoned before anything was counted describes no goods, so
# its lines count for nothing when deriving what has arrived.
NOT_RECEIVED_STATUSES = {"cancelled"}

# A purchase order in one of these is not something receiving may touch: a
# draft is not a commitment, and a cancelled PO must not quietly come back
# to life because a supplier delivered against it anyway.
UNTOUCHABLE_ORDER_STATUSES = {"draft", "cancelled"}


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _quantity(value):
    """Decimal, never a bare float -- quantities may be fractional."""
    try:
        return Decimal(_text(value) or "0")
    except InvalidOperation:
        return Decimal(0)


def _number(value):
    return format(value.normalize(), "f")


def _setting(base, key, default=""):
    """Duplicated on purpose, same as every other package that reads
    app_settings: there is no shared settings module in this codebase yet,
    and inventing one for a fifth copy is the layer this house rule
    (docs/logic-decisions.md #4) says to wait on."""
    try:
        for row in object_records.read_collection_records("app_settings",
                                                          base_dir=base):
            if row.get("key") == key and _text(row.get("value")):
                return _text(row["value"])
    except Exception:
        pass
    return default


def _receipt_for(request, base):
    """The receipt this event is about, whichever collection fired it.

    A receipt_lines event names a line; the thing that matters is its
    parent, because a line added to an already-received receipt is goods
    that still have to land on the shelf.

    Both the record itself and a bare record_id are accepted: the HTTP
    dispatcher and the change-log dispatcher both send record_id, while an
    operator poking this by hand (or a sibling handler calling it in
    process) has the row already.
    """
    collection = _text(request.get("collection"))
    record = request.get("record")
    record_id = _text(request.get("record_id") or request.get("id"))

    if collection == "receipt_lines":
        line = record if isinstance(record, dict) and record.get("id") else None
        if line is None and record_id:
            try:
                line = object_records.get_collection_record(
                    "receipt_lines", record_id, base_dir=base)
            except Exception:
                return None
        if not line:
            return None
        record_id = _text(line.get("receipt_id"))
        record = None

    if isinstance(record, dict) and record.get("id") and collection != "receipt_lines":
        # Re-read rather than trust the payload: an event carrying a stale
        # copy (dispatched from the change log, minutes later) must not make
        # this handler act on a status the receipt has since moved past.
        record_id = _text(record.get("id"))

    if not record_id:
        return None
    try:
        return object_records.get_collection_record("receipts", record_id,
                                                    base_dir=base)
    except Exception:
        return None


def _lines_of(base, receipt_id):
    try:
        rows = object_records.read_collection_records("receipt_lines",
                                                      base_dir=base)
    except Exception:
        return []
    return [row for row in rows if _text(row.get("receipt_id")) == receipt_id]


def _order_lines_of(base, order_id):
    try:
        rows = object_records.read_collection_records("order_lines",
                                                      base_dir=base)
    except Exception:
        return []
    return [row for row in rows if _text(row.get("order_id")) == order_id]


def _move_stock(base, receipt, order, moves):
    """One purchase move per line, idempotent per line by provenance marker.

    Returns (moved, warning). `moves` is the already-read stock_moves log,
    or None when the collection does not exist at all (a shop buying
    services has nothing to shelve and is not broken).
    """
    if moves is None:
        return 0, "stock not installed; nothing to move"

    from_location = _setting(base, "receiving.supplier_location")
    to_location = (_setting(base, "receiving.stock_location")
                   or _setting(base, "shop.stock_location"))
    existing = [_text(move.get("reference")) for move in moves]

    # The agreed price per ordered thing, for the cost stamp. Read from the
    # PO line rather than from the product: what we are about to own it for
    # is what this purchase actually cost, not what the catalog guesses.
    prices = {row["id"]: _text(row.get("unit_price_cents"))
              for row in _order_lines_of(base, order["id"])}

    moved = 0
    warning = ""
    when = _text(receipt.get("received_on")) or date.today().isoformat()
    for line in _lines_of(base, receipt["id"]):
        product_id = _text(line.get("product_id"))
        if not product_id:
            continue                      # a service never sat on a shelf
        quantity = _quantity(line.get("quantity_received"))
        if quantity <= 0:
            # A rejected-only line, or a wholly short delivery. Nothing
            # entered stock, so nothing moves; the discrepancy lives on the
            # receipt line where the supplier argument can find it.
            continue
        marker = f"receipts/{receipt['id']}:line/{line['id']}"
        if any(marker in reference for reference in existing):
            continue                      # this line already landed
        if not to_location:
            warning = ("receiving.stock_location (or shop.stock_location) is "
                       "not configured, so nothing landed on a shelf; the "
                       "receipt still stands")
            break
        move = {
            "id": object_ids.new_uuid4(),
            "product_id": product_id,
            "from_location_id": from_location,
            "to_location_id": to_location,
            "quantity": _number(quantity),
            "reason": "purchase",
            "reference": f"{marker} {_text(order.get('number'))}".strip(),
            "occurred_at": when,
            "owner_id": _text(receipt.get("owner_id")),
            "entity_id": _text(receipt.get("entity_id")),
        }
        unit_cost = prices.get(_text(line.get("order_line_id")), "")
        if unit_cost:
            # The cost on the way IN, stamped at the moment of movement --
            # the input FIFO and weighted average both consume, and the one
            # that cannot be reconstructed once the price list moves on.
            move["unit_cost_cents"] = unit_cost
        object_records.create_collection_record("stock_moves", move,
                                                base_dir=base, actor=ACTOR)
        existing.append(marker)
        moved += 1
    return moved, warning


def _derive_order_status(base, order):
    """What the receipt lines say this purchase order's status is.

    Returns "" when the facts say nothing yet (nothing received), so the PO
    is left exactly as a human last set it rather than being dragged
    backwards by a machine that knows less than they do.
    """
    order_lines = _order_lines_of(base, order["id"])
    if not order_lines:
        return ""

    try:
        receipts = [row for row in object_records.read_collection_records(
            "receipts", base_dir=base)
            if _text(row.get("order_id")) == order["id"]]
    except Exception:
        return ""

    counted = {row["id"]: _text(row.get("status")) for row in receipts
               if _text(row.get("status")) not in NOT_RECEIVED_STATUSES}
    if not counted:
        return ""

    try:
        all_lines = object_records.read_collection_records("receipt_lines",
                                                           base_dir=base)
    except Exception:
        return ""

    received_by_line = {}
    for line in all_lines:
        if counted.get(_text(line.get("receipt_id")), "") not in RECEIVED_STATUSES:
            continue                      # an open receipt is a count in
        key = _text(line.get("order_line_id"))       # progress, not a fact
        received_by_line[key] = received_by_line.get(key, Decimal(0)) + \
            _quantity(line.get("quantity_received"))

    if sum(received_by_line.values(), Decimal(0)) <= 0:
        return ""

    everything = all(
        received_by_line.get(line["id"], Decimal(0)) >= _quantity(line.get("quantity"))
        for line in order_lines)
    if not everything:
        return "partial"
    return "received"


def EVENT(request):
    base = _base_dir()
    receipt = _receipt_for(request, base)
    if not receipt:
        return {"ok": True, "skipped": "no receipt in the event"}

    try:
        order = object_records.get_collection_record(
            "orders", _text(receipt.get("order_id")), base_dir=base)
    except Exception:
        return {"ok": True, "skipped": "no order for this receipt",
                "receipt_id": receipt["id"]}

    if _text(order.get("doc_type")) != "purchase":
        # Belt and braces behind action_receive_goods' refusal: a receipt
        # written straight through the HTTP path against a sales order must
        # not silently move stock the wrong way.
        return {"ok": True, "skipped": "not a purchase order",
                "receipt_id": receipt["id"], "order_id": order["id"]}

    order_status = _text(order.get("status"))
    if order_status in UNTOUCHABLE_ORDER_STATUSES:
        return {"ok": True, "skipped": f"order is {order_status}",
                "receipt_id": receipt["id"], "order_id": order["id"]}

    result = {"ok": True, "receipt_id": receipt["id"], "order_id": order["id"],
              "moved": 0}

    if _text(receipt.get("status")) in RECEIVED_STATUSES:
        try:
            moves = object_records.read_collection_records("stock_moves",
                                                           base_dir=base)
        except Exception:
            moves = None
        moved, warning = _move_stock(base, receipt, order, moves)
        result["moved"] = moved
        if warning:
            # The receipt stands and the gap is visible, which is the right
            # way round: a missing setting must not cost somebody the record
            # that goods arrived, and an invisible discrepancy is the one
            # nobody fixes.
            result["warning"] = warning

    derived = _derive_order_status(base, order)
    result["order_status"] = order_status
    if derived and derived != order_status:
        try:
            object_records.update_collection_record(
                "orders", order["id"], {"status": derived},
                base_dir=base, actor=ACTOR)
            result["order_status"] = derived
            result["order_status_changed"] = True
        except Exception as exc:
            # A ladder that refuses the derived move is a real answer, not a
            # crash: say so and leave the order where a human put it.
            result["order_status_error"] = str(exc)[:200]
    return result


# EVENT is the verb the change dispatcher calls handlers with (see
# object_change_dispatch); POST stays as an alias so an operator can poke
# the handler by hand over HTTP. The alias is not decoration -- a handler
# shipped with only POST silently matched nothing in production once, and
# every handler in this house has carried both ever since.
POST = EVENT
