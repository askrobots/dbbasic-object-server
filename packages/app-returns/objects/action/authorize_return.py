"""action_authorize_return -- saying yes, in writing, with a deadline.

POST {order_id, lines: [{order_line_id, quantity}], reason, reason_note?,
      expires_days?, today?}

Two records come out of one decision: a return_authorizations row that
says WHO agreed and until WHEN, and an INBOUND shipment in `authorized`
carrying the lines the customer may send back. They are two records
because they are two different facts -- the offer and the parcel -- and
the parcel is the same noun that has always carried goods in this system,
with the sign reversed (plan/fulfillment-logistics-spec.md: "an RMA IS an
inbound shipment"). Building a second returns-lines collection would only
produce a table that could disagree with shipment_lines about what is in
the box, and the disagreement would surface on the day somebody counts.

Nothing is un-shipped. The outbound shipment stays exactly as it is: it
is a true statement about what left the building on a particular day, and
it stays true whatever comes back afterwards (docs/logic-decisions.md
#3). Everything about this return is therefore additive -- a new
document pointing at the old one.

Every blocker is reported TOGETHER, checkout-style. The person doing this
is usually on the phone to the customer, and revealing one problem at a
time is how a screen gets abandoned in favour of "just send it back and
we'll sort it out", which is the exact condition this package exists to
end.

**You cannot return what never left.** The order has to be shipped,
delivered or partial. A draft or confirmed order has goods still on our
own shelf, and "returning" them is a cancellation, a different document
with different money attached.

**You cannot return more than was shipped.** The arithmetic is the mirror
image of hook_shipment_lines' over-ship gate and refuses in the same
shape, with all the numbers: shipped 3, already authorized 1, asked for
3, would make 4. A gate that only says "no" leaves somebody guessing
which of two returns is the wrong one. Quantities already authorized
count even though the goods may not be back yet -- an RMA is a claim on
those units, and issuing a second RMA for the same mug is how two refunds
get paid for one sale. An `expired` inbound shipment releases its claim
again, exactly as a `lost` outbound one releases its hold on the order.

**A known interaction, named rather than discovered later:**
hook_shipment_lines counts EVERY shipment line against the ordered
quantity regardless of direction, so an inbound line written through the
generic HTTP path would be refused as an over-ship. This action writes
its lines server-side, where hooks are bypassed by design, which is why
the RMA door is this object and not a form posting straight at
shipment_lines. The hook belongs to app-shipping and is not this slice's
to edit; the cost is that returns have exactly one entrance, which is
where the authorization argument wanted them anyway.
"""

import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import object_ids
import object_records

ACTOR = "action_authorize_return"

# The goods reached the customer (or are on their way to them): there is
# something out there to send back. Deliberately the same three statuses
# system_order_fulfillment counts as shipped -- and note what is NOT here:
# a `lost` or `returned_to_sender` parcel never reached the customer, so
# its lines are not units anybody could be sending back.
SHIPPED_ONWARD = {"shipped", "in_transit", "delivered"}

# An inbound shipment in one of these has released its claim on the units:
# the offer lapsed and nothing ever came back, so the customer may be
# granted a fresh RMA for the same goods.
RELEASED_INBOUND_STATUSES = {"expired"}

# An order whose goods have left. Anything else is a cancellation, not a
# return.
RETURNABLE_ORDER_STATUSES = {"shipped", "delivered", "partial"}

REASONS = ("damaged", "wrong_item", "not_as_described", "no_longer_wanted",
           "arrived_late", "other")

DEFAULT_WINDOW_DAYS = 30


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _quantity(value):
    """Decimal, never a bare float -- quantities may be fractional, and
    binary-float arithmetic would turn an exact-fit return into an
    over-return."""
    try:
        return Decimal(_text(value) or "0")
    except InvalidOperation:
        return None


def _number(value):
    return format(value.normalize(), "f")


def _setting(base, key, default=""):
    """Duplicated on purpose, same as every other package that reads
    app_settings: there is no shared settings module in this codebase yet,
    and inventing one for a sixth copy is the layer this house rule
    (docs/logic-decisions.md #4) says to wait on."""
    try:
        for row in object_records.read_collection_records("app_settings",
                                                          base_dir=base):
            if row.get("key") == key and _text(row.get("value")):
                return _text(row["value"])
    except Exception:
        pass
    return default


def _returnable_by_line(base, order_id, order_lines):
    """How much of each order line the customer may still send back.

    Shipped on a parcel that reached them, minus everything already
    claimed by an inbound shipment that has not expired. Both halves are
    folds over shipment_lines, because that is where the quantities are;
    nothing on the order itself is asked what it thinks.
    """
    try:
        shipments = object_records.read_collection_records("shipments",
                                                           base_dir=base)
    except Exception:
        shipments = []
    outbound = {row["id"] for row in shipments
                if _text(row.get("order_id")) == order_id
                and _text(row.get("direction")) != "inbound"
                and _text(row.get("status")) in SHIPPED_ONWARD}
    inbound = {row["id"] for row in shipments
               if _text(row.get("order_id")) == order_id
               and _text(row.get("direction")) == "inbound"
               and _text(row.get("status")) not in RELEASED_INBOUND_STATUSES}

    try:
        lines = object_records.read_collection_records("shipment_lines",
                                                       base_dir=base)
    except Exception:
        lines = []

    shipped = {}
    authorized = {}
    for line in lines:
        parent = _text(line.get("shipment_id"))
        key = _text(line.get("order_line_id"))
        amount = _quantity(line.get("quantity")) or Decimal(0)
        if parent in outbound:
            shipped[key] = shipped.get(key, Decimal(0)) + amount
        elif parent in inbound:
            authorized[key] = authorized.get(key, Decimal(0)) + amount

    returnable = {}
    for line in order_lines:
        returnable[line["id"]] = (shipped.get(line["id"], Decimal(0))
                                  - authorized.get(line["id"], Decimal(0)))
    return returnable, shipped, authorized


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
    if order_status not in RETURNABLE_ORDER_STATUSES:
        return {"status": 409,
                "error": (f"This order is {order_status}, so nothing has left "
                          f"the building yet -- you cannot return what was "
                          f"never sent. Goods still on our own shelf come off "
                          f"an order by cancelling or amending it, which is a "
                          f"different document with different money attached.")}

    reason = _text(request.get("reason"))
    try:
        all_lines = object_records.read_collection_records("order_lines",
                                                           base_dir=base)
    except Exception:
        all_lines = []
    order_lines = [line for line in all_lines
                   if _text(line.get("order_id")) == order_id]
    by_id = {line["id"]: line for line in order_lines}
    returnable, shipped, _claimed = _returnable_by_line(base, order_id,
                                                        order_lines)

    blockers = {"unknown_lines": [], "over_return": [], "bad_quantities": [],
                "missing_reason": ""}
    if not reason:
        blockers["missing_reason"] = (
            "A return has to say why. The reason decides what happens to the "
            "goods (damaged ones are disposed of as damage, not waste), "
            "whether the mistake was ours, and whether a restocking fee is "
            "fair -- and a reason asked for a week later is a guess.")
    elif reason not in REASONS:
        blockers["missing_reason"] = (
            f"Unknown reason {reason!r}; expected one of "
            f"{', '.join(REASONS)}.")

    requested = request.get("lines")
    wanted = []
    if not isinstance(requested, list) or not requested:
        blockers["bad_quantities"].append(
            {"order_line_id": "",
             "reason": "name at least one line to return; an RMA for "
                       "'something from that order' is not something anybody "
                       "can count a parcel against"})
    else:
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
                     "reason": "a return line must return a positive quantity"})
                continue
            left = returnable.get(line_id, Decimal(0))
            if quantity > left:
                was_shipped = shipped.get(line_id, Decimal(0))
                blockers["over_return"].append({
                    "order_line_id": line_id,
                    "description": _text(by_id[line_id].get("description")),
                    "shipped": _number(was_shipped),
                    "already_authorized": _number(was_shipped - left),
                    "asked_for": _number(quantity),
                    "would_make": _number(was_shipped - left + quantity),
                })
                continue
            wanted.append((by_id[line_id], quantity))

    if any(blockers.values()):
        return {"status": 409,
                "error": "Some of those lines cannot be returned.",
                **blockers}

    owner = (_text(order.get("owner_id"))
             or _text((request.get("_identity") or {}).get("user_id")))
    actor_user = _text((request.get("_identity") or {}).get("user_id")) or owner

    today = _text(request.get("today")) or date.today().isoformat()
    try:
        window = int(_text(request.get("expires_days"))
                     or _setting(base, "returns.window_days")
                     or DEFAULT_WINDOW_DAYS)
    except ValueError:
        window = DEFAULT_WINDOW_DAYS
    if window < 0:
        window = DEFAULT_WINDOW_DAYS
    try:
        expires_on = (date.fromisoformat(today)
                      + timedelta(days=window)).isoformat()
    except ValueError:
        # A caller-supplied `today` that is not a date must not cost anybody
        # their RMA; the window is simply measured from the real one.
        expires_on = (date.today() + timedelta(days=window)).isoformat()

    shipment_id = object_ids.new_uuid4()
    rma_id = object_ids.new_uuid4()
    provenance = (f"Generated by {ACTOR} "
                  f"[orders/{order_id} return_authorizations/{rma_id}]")

    object_records.create_collection_record(
        "shipments",
        {
            "id": shipment_id,
            "order_id": order_id,
            "direction": "inbound",
            # Authorized, not open: the goods are still with the customer,
            # and `open` on an outbound shipment means a box we are filling.
            "status": "authorized",
            "service": "other",
            # Where it is coming FROM, stamped like every other address on
            # this collection: a return label printed next month must send
            # the parcel to where we were when we promised it.
            "ship_to_name": (_text(order.get("customer_name"))
                             or _text(order.get("customer_email"))),
            "notes": provenance,
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
                # Stamped from the order line, same as an outbound parcel: a
                # return sheet printed next year must still say what the
                # customer was told to put in the box.
                "description": _text(order_line.get("description")),
                "quantity": _number(quantity),
                "owner_id": owner,
            },
            base_dir=base, actor=ACTOR)
        created.append({"id": line_id, "order_line_id": order_line["id"],
                        "description": _text(order_line.get("description")),
                        "quantity": _number(quantity)})

    object_records.create_collection_record(
        "return_authorizations",
        {
            "id": rma_id,
            "order_id": order_id,
            "shipment_id": shipment_id,
            # Stamped: the customer of this return is whoever bought the
            # goods, whatever the order record says next month.
            "customer_email": _text(order.get("customer_email")),
            "reason": reason,
            "reason_note": _text(request.get("reason_note")),
            "status": "authorized",
            "authorized_by": actor_user,
            "authorized_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
            "expires_on": expires_on,
            "owner_id": owner,
        },
        base_dir=base, actor=ACTOR)

    return {"ok": True, "order_id": order_id, "return_id": rma_id,
            "shipment_id": shipment_id, "status_of_return": "authorized",
            "status_of_shipment": "authorized", "lines": len(created),
            "return_lines": created, "reason": reason,
            "expires_on": expires_on, "date": today,
            "note": "return authorized; nothing has moved and no money has "
                    "gone back -- the parcel arriving is one event and a human "
                    "deciding what is in it is another (action_disposition_"
                    "return)"}
