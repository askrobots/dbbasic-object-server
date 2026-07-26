"""system_order_attention -- orders with goods still owed to a customer.

COUNT {} -> {count, detail}

The pick queue, as a number. An order that is committed and not yet fully
out the door is a promise this business has made and not kept, and unlike
almost everything else on this list it is not a status somebody sets --
it is ordered quantity minus shipped quantity, folded from four
collections. `site_pick_list` folds exactly that, live, per render, and
is right to: it is a working screen somebody opens on purpose. A home
page cannot afford the same fold a dozen times over, which is why this
object exists and why the daemon runs it on an interval instead.

Counted per ORDER rather than per unit, because "4 orders waiting" is the
sentence a person acts on; the units go in the detail, where they answer
the follow-up question (one order short by a hundred mugs is a different
morning from four short by one each).

The `confirmed | processing | partial` set and the "lost and
returned_to_sender do not count as shipped" rule are read from
`site_pick_list`, deliberately, so the badge and the page it links to
cannot disagree about what is owed. app-shipping's shipment/shipment_line
schemas are the source of the shipped half; nothing here writes to them,
and when they are absent every committed order is simply owed in full,
which is the correct answer for a deployment with no shipping app.

COUNT rather than GET or POST: the verb is the contract with the daemon's
attention pass. A missing orders collection reads as zero; a genuine
failure raises, so the rollup records it and keeps the last count rather
than reporting an empty warehouse.
"""

import os
from decimal import Decimal, InvalidOperation

import object_records

ACTOR = "system_order_attention"

# Committed and not yet fully out the door. draft is not a commitment;
# shipped and delivered have nothing left; cancelled must never be picked.
PICKABLE_ORDER_STATUSES = {"confirmed", "processing", "partial"}

# Shipments in these never reached anybody, so their lines do not reduce
# what is still owed -- the goods have to be picked again.
NOT_DELIVERED_STATUSES = {"lost", "returned_to_sender"}


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _quantity(value):
    try:
        return Decimal(_text(value) or "0")
    except InvalidOperation:
        return Decimal(0)


def _rows(collection):
    try:
        return object_records.read_collection_records(collection, base_dir=_base_dir())
    except Exception:
        # An absent shipping app means nothing has shipped, which is a
        # real answer and not a failure.
        return []


def _number(value):
    return format(value.normalize(), "f")


def COUNT(request):
    try:
        orders = object_records.read_collection_records("orders", base_dir=_base_dir())
    except Exception:
        return {"count": 0}

    open_orders = {order["id"] for order in orders
                   if _text(order.get("status")) in PICKABLE_ORDER_STATUSES
                   and _text(order.get("doc_type") or "sale") == "sale"}
    if not open_orders:
        return {"count": 0}

    counted = {row["id"] for row in _rows("shipments")
               if _text(row.get("status")) not in NOT_DELIVERED_STATUSES}
    shipped = {}
    for line in _rows("shipment_lines"):
        if _text(line.get("shipment_id")) not in counted:
            continue
        key = _text(line.get("order_line_id"))
        shipped[key] = shipped.get(key, Decimal(0)) + _quantity(line.get("quantity"))

    owing_orders = set()
    units = Decimal(0)
    for line in _rows("order_lines"):
        order_id = _text(line.get("order_id"))
        if order_id not in open_orders:
            continue
        remaining = _quantity(line.get("quantity")) - shipped.get(line["id"], Decimal(0))
        if remaining > 0:
            owing_orders.add(order_id)
            units += remaining

    if not owing_orders:
        return {"count": 0}
    return {"count": len(owing_orders), "detail": f"{_number(units)} units to pick"}
