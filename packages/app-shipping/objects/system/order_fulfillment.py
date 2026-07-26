"""system_order_fulfillment -- the box left, so the shelf is lighter and
the order says so.

HANDLES shipments and shipment_lines writes. Two jobs, both of them reads
turned into facts:

**Stock moves when goods physically move.** One `sale` move PER SHIPMENT
LINE at the moment a shipment reaches `shipped`, from shop.stock_location
to shop.customer_location. Per line rather than per shipment because a
level is a fold over moves per product, and per order rather than per
line was only ever right while an order could not be split -- the whole
point of the shipment noun is that it can.

**The order's status is derived, never set.** Some of the ordered quantity
on shipped-or-beyond shipments is `partial`; all of it is `shipped`; all of
it delivered is `delivered` (docs/logic-decisions.md #1: the QUANTITIES on
shipment lines are the facts, the order's status is a read of them). A
human never types `partial` and no surface computes it at read time; it is
stored, so the order list, the pick list and a report cannot disagree.

Placement follows docs/logic-decisions.md #6 -- a REACTION, post-commit,
best-effort, never blocking the write that triggered it. A warehouse that
cannot record a stock move must still be able to send the parcel; the
discrepancy is visible and fixable, whereas a refused shipment is a
customer waiting for goods that are sitting in a box by the door.

Idempotency by provenance (#7): each move is stamped
`shipments/{shipment_id}:line/{line_id}` in its reference, PER LINE. A
replayed event -- and events are replayed here, by design: the change
dispatcher promises at-least-once (object_change_dispatch.py) -- finds its
own marker and moves nothing. Per line and not per shipment because a
shipment can gain a line while still open, and a shipment-level marker
would silently skip the goods added after the first pass.

Missing settings do not cost anybody a shipment. If shop.stock_location is
unconfigured the parcel still ships, the order status still derives, and
the gap is reported in `warning` -- the same posture the payment-side
handler this one took the stock work over from has always had.
"""

import os
from datetime import date
from decimal import Decimal, InvalidOperation

import object_ids
import object_records

HANDLES = [
    "shipments.record.created",
    "shipments.record.updated",
    "shipment_lines.record.created",
]

ACTOR = "system_order_fulfillment"

# The box is with a courier or a customer: the goods have left the shelf.
SHIPPED_ONWARD = {"shipped", "in_transit", "delivered"}

# Never reached the customer -- the order still owes those goods, so these
# shipments' lines count for nothing when deriving what has been fulfilled.
NOT_DELIVERED_STATUSES = {"lost", "returned_to_sender"}

# An order in one of these is not something fulfillment may touch: a draft
# is not a commitment and a cancelled order must not quietly come back to
# life because somebody shipped against it by mistake.
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
    and inventing one for a fourth copy is the layer this house rule
    (docs/logic-decisions.md #4) says to wait on."""
    try:
        for row in object_records.read_collection_records("app_settings",
                                                          base_dir=base):
            if row.get("key") == key and _text(row.get("value")):
                return _text(row["value"])
    except Exception:
        pass
    return default


def _shipment_for(request, base):
    """The shipment this event is about, whichever collection fired it.

    A shipment_lines event names a line; the thing that matters is its
    parent, because a line added to an already-shipped shipment is goods
    that still have to leave the shelf.

    Both the record itself and a bare record_id are accepted: the HTTP
    dispatcher and the change-log dispatcher both send record_id, while an
    operator poking this by hand (or a sibling handler calling it in
    process) has the row already.
    """
    collection = _text(request.get("collection"))
    record = request.get("record")
    record_id = _text(request.get("record_id") or request.get("id"))

    if collection == "shipment_lines":
        line = record if isinstance(record, dict) and record.get("id") else None
        if line is None and record_id:
            try:
                line = object_records.get_collection_record(
                    "shipment_lines", record_id, base_dir=base)
            except Exception:
                return None
        if not line:
            return None
        record_id = _text(line.get("shipment_id"))
        record = None

    if isinstance(record, dict) and record.get("id") and collection != "shipment_lines":
        # Re-read rather than trust the payload: an event carrying a stale
        # copy (dispatched from the change log, minutes later) must not make
        # this handler act on a status the shipment has since moved past.
        record_id = _text(record.get("id"))

    if not record_id:
        return None
    try:
        return object_records.get_collection_record("shipments", record_id,
                                                    base_dir=base)
    except Exception:
        return None


def _lines_of(base, shipment_id):
    try:
        rows = object_records.read_collection_records("shipment_lines",
                                                      base_dir=base)
    except Exception:
        return []
    return [row for row in rows if _text(row.get("shipment_id")) == shipment_id]


def _move_stock(base, shipment, order, moves):
    """One sale move per line, idempotent per line by provenance marker.

    Returns (moved, skipped_reason). `moves` is the already-read stock_moves
    log, or None when the collection does not exist at all (a shop selling
    services and downloads has nothing to move and is not broken).
    """
    if moves is None:
        return 0, "stock not installed; nothing to move"

    from_location = _setting(base, "shop.stock_location")
    to_location = _setting(base, "shop.customer_location")
    existing = [_text(move.get("reference")) for move in moves]

    moved = 0
    warning = ""
    today = _text(shipment.get("shipped_on")) or date.today().isoformat()
    for line in _lines_of(base, shipment["id"]):
        product_id = _text(line.get("product_id"))
        if not product_id:
            continue                      # a service never sat on a shelf
        marker = f"shipments/{shipment['id']}:line/{line['id']}"
        if any(marker in reference for reference in existing):
            continue                      # this line already moved
        if not from_location:
            warning = ("shop.stock_location is not configured, so nothing "
                       "left the shelf; the shipment still stands")
            break
        object_records.create_collection_record(
            "stock_moves",
            {
                "id": object_ids.new_uuid4(),
                "product_id": product_id,
                "from_location_id": from_location,
                "to_location_id": to_location,
                "quantity": _text(line.get("quantity")) or "1",
                "reason": "sale",
                "reference": f"{marker} {_text(order.get('number'))}".strip(),
                "occurred_at": today,
                "owner_id": _text(shipment.get("owner_id")),
                "entity_id": _text(shipment.get("entity_id")),
            },
            base_dir=base, actor=ACTOR)
        existing.append(marker)
        moved += 1
    return moved, warning


def _derive_order_status(base, order):
    """What the shipment lines say this order's status is.

    Returns "" when the facts say nothing yet (nothing shipped), so the
    order is left exactly as a human last set it rather than being dragged
    backwards by a machine that knows less than they do.
    """
    try:
        order_lines = [line for line in object_records.read_collection_records(
            "order_lines", base_dir=base)
            if _text(line.get("order_id")) == order["id"]]
    except Exception:
        return ""
    if not order_lines:
        return ""

    try:
        shipments = [row for row in object_records.read_collection_records(
            "shipments", base_dir=base)
            if _text(row.get("order_id")) == order["id"]
            and _text(row.get("direction")) != "inbound"]
    except Exception:
        return ""

    counted = {row["id"]: _text(row.get("status")) for row in shipments
               if _text(row.get("status")) not in NOT_DELIVERED_STATUSES}
    if not counted:
        return ""

    shipped_lines = [line for line in _all_shipment_lines(base)
                     if counted.get(_text(line.get("shipment_id")), "")
                     in SHIPPED_ONWARD]

    shipped_by_line = {}
    for line in shipped_lines:
        key = _text(line.get("order_line_id"))
        shipped_by_line[key] = shipped_by_line.get(key, Decimal(0)) + \
            _quantity(line.get("quantity"))

    total_shipped = sum(shipped_by_line.values(), Decimal(0))
    if total_shipped <= 0:
        return ""

    everything = all(
        shipped_by_line.get(line["id"], Decimal(0)) >= _quantity(line.get("quantity"))
        for line in order_lines)
    if not everything:
        return "partial"

    # Everything is on a shipment that has left. "delivered" is the further
    # claim that every one of those shipments arrived -- never inferred from
    # "it has been a while", which is how systems learn to lie about parcels.
    if all(status == "delivered" for status in counted.values()):
        return "delivered"
    return "shipped"


def _all_shipment_lines(base):
    try:
        return object_records.read_collection_records("shipment_lines",
                                                      base_dir=base)
    except Exception:
        return []


def EVENT(request):
    base = _base_dir()
    shipment = _shipment_for(request, base)
    if not shipment:
        return {"ok": True, "skipped": "no shipment in the event"}

    if _text(shipment.get("direction")) == "inbound":
        # A return has not decided whether it is restock or waste yet, and
        # guessing would put damaged goods back on the shelf. The
        # disposition flow is a later slice, deliberately absent.
        return {"ok": True, "skipped": "inbound shipments are dispositioned "
                                       "by hand, not by this handler",
                "shipment_id": shipment["id"]}

    try:
        order = object_records.get_collection_record(
            "orders", _text(shipment.get("order_id")), base_dir=base)
    except Exception:
        return {"ok": True, "skipped": "no order for this shipment",
                "shipment_id": shipment["id"]}

    order_status = _text(order.get("status"))
    if order_status in UNTOUCHABLE_ORDER_STATUSES:
        return {"ok": True, "skipped": f"order is {order_status}",
                "shipment_id": shipment["id"], "order_id": order["id"]}

    result = {"ok": True, "shipment_id": shipment["id"], "order_id": order["id"],
              "moved": 0}

    if _text(shipment.get("status")) in SHIPPED_ONWARD:
        try:
            moves = object_records.read_collection_records("stock_moves",
                                                           base_dir=base)
        except Exception:
            moves = None
        moved, warning = _move_stock(base, shipment, order, moves)
        result["moved"] = moved
        if warning:
            # The parcel stands and the gap is visible, which is the right
            # way round: a missing setting must not cost a customer their
            # goods, and an invisible discrepancy is the one nobody fixes.
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
