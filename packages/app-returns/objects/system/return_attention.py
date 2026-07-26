"""system_return_attention -- returned goods sitting undecided.

COUNT {} -> {count, detail}

An inbound shipment at `received` is a box that arrived back and has not
been judged: nobody has yet said whether it goes back on the shelf, into
the bin, or back to the supplier. Until somebody does, the customer has
no refund, the stock has no truth, and the box has a physical location on
somebody's floor. It is the most literally physical queue on this list
and the easiest one to forget, because a returned parcel makes no noise.

The RMA (`return_authorizations`) is deliberately NOT what is counted.
An authorization is a promise to accept goods; the goods themselves are
the shipment, and a shipment that arrived is the thing with a person
waiting on it. Counting authorizations would count intentions, several of
which will never turn into a parcel at all.

The `received` / `dispositioned` pair lives in app-shipping's shipments
status enum -- inbound and outbound share one document because a return
is a shipment travelling the other way. This object reads that collection
and writes nothing to it.

COUNT rather than GET or POST: the verb is the contract with the daemon's
attention pass. A missing collection reads as zero; a genuine failure
raises, so the rollup records it and keeps the last count rather than
reporting a clear returns bench nobody cleared.
"""

import os
from datetime import datetime, timezone

import object_records

ACTOR = "system_return_attention"

# The box is here and nobody has judged it. `authorized` has not arrived
# yet, `dispositioned` has been judged, `expired` never came.
WAITING_STATUS = "received"
INBOUND = "inbound"

# A return sitting a week is a refund somebody is waiting on.
STALE_DAYS = 7


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _age_days(value):
    text = _text(value).replace("Z", "+00:00")
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).days


def COUNT(request):
    try:
        rows = object_records.read_collection_records("shipments", base_dir=_base_dir())
    except Exception:
        return {"count": 0}

    waiting = [row for row in rows
               if _text(row.get("direction")) == INBOUND
               and _text(row.get("status")) == WAITING_STATUS]
    if not waiting:
        return {"count": 0}

    stale = sum(1 for row in waiting
                if (_age_days(row.get("created_at")) or 0) >= STALE_DAYS)
    detail = f"{stale} waiting over a week" if stale else ""
    return {"count": len(waiting), "detail": detail}
