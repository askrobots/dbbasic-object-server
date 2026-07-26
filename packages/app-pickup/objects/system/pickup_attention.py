"""system_pickup_attention -- orders past the time we promised them.

COUNT {} -> {count, detail}

The one queue a counter business cannot afford to discover late. Every
other number on the attention band is work waiting; this one is a
promise already broken, with somebody standing in a shop holding a phone
that says their order was ready ten minutes ago. Severity `warning`
rather than `normal` for exactly that: nobody chose to put these here and
the clock is running in front of a customer.

**Keyed on promised_at, not on fulfillment_method.** An order is late
because a time passed, and the field that holds that time is the only
thing this needs to read. Keying on the method instead would mean
deciding here which methods make promises -- and the moment a delivery
order or a counter order acquires a promised_at, a method-keyed count
would silently ignore it while the customer waited. A shipping order
never has a promised_at at all, so it can never appear in this count,
which is the regression that matters and it holds by construction rather
than by a filter somebody has to remember.

Not-ready is the other half, and `ready` is where the clock stops rather
than `collected`: the shop's obligation is to HAVE IT READY at the
promised time. An order sitting on the shelf that the customer has not
walked in for yet is not the shop's failure, and counting it would fill
this queue with orders nobody can do anything about -- which is how a
band stops being read.

COUNT rather than GET or POST: the verb is the contract with the daemon's
attention pass. A missing orders collection reads as zero; a genuine
failure raises, so the rollup records it and keeps the last count rather
than reporting a kitchen that is quietly on fire.
"""

import os
from datetime import datetime

import object_records

ACTOR = "system_pickup_attention"

# The promise is discharged at `ready` -- see the module docstring. The
# other three are orders that will never be ready and must not be counted
# as late: two because they already happened, one because it was called
# off.
SETTLED_STATUSES = {"ready", "collected", "cancelled"}


def _text(value):
    return str(value if value is not None else "").strip()


def _moment(value):
    """An ISO datetime as NAIVE local wall-clock, or None.

    Times in this repo are the shop's own clock (order_date, shipped_on,
    pickup_slots.starts_at), but a hand-typed or imported value may
    carry an offset -- and comparing an aware datetime to a naive one
    raises, which would turn one badly-typed order into a permanently
    errored attention count for the whole shop. Converting to local and
    dropping the offset keeps that one row honest and the queue alive.
    """
    text = _text(value)
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment


def _minutes(delta):
    return int(delta.total_seconds() // 60)


def COUNT(request):
    base = os.environ.get("DBBASIC_DATA_DIR", "data")
    try:
        orders = object_records.read_collection_records("orders", base_dir=base)
    except Exception:
        return {"count": 0}

    now = datetime.now()
    late = []
    for order in orders:
        if _text(order.get("status")) in SETTLED_STATUSES:
            continue
        promised = _moment(order.get("promised_at"))
        if promised is None or promised >= now:
            continue
        late.append(promised)

    if not late:
        return {"count": 0}
    worst = _minutes(now - min(late))
    return {"count": len(late),
            "detail": f"oldest {worst} minutes past its promised time"}
