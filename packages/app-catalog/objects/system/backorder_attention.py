"""system_backorder_attention -- how many people are waiting on stock.

COUNT {} -> {count, detail}

The open backorder rows: goods a customer has already been sold and not
yet been sent, plus the interest recorded for people the shop refused and
promised to tell. `filled` and `cancelled` have both had an answer.

Severity `warning` rather than `normal` in the manifest, and the argument
is the one system_shipment_attention makes about a stuck parcel: nobody
chose to be in this queue, and the clock is running somewhere else -- in
this case in the inbox of somebody who has already paid. It is not
`urgent`, which is reserved for the server saying it stopped doing its
own work.

The detail names the OLDEST wait rather than a total, because that is the
row somebody should ring first: "9 waiting" is a number, "9 waiting, the
oldest 21 days" is a reason to open the page.

A row whose requested_on will not parse is counted but contributes no
age. An unreadable date must not silently become evidence that somebody
has been waiting no time at all, and must not take the whole count down
with it either.

Degrades to zero when backorders is absent, the same posture every other
provider on this box takes: a rollup pass should not log an error every
five minutes about a collection nobody installed.
"""

import os
from datetime import date

import object_records

ACTOR = "system_backorder_attention"

WAITING_STATUS = "open"


def _text(value):
    return str(value if value is not None else "").strip()


def _days_since(value, today):
    try:
        return (today - date.fromisoformat(_text(value))).days
    except ValueError:
        return None


def COUNT(request):
    base = os.environ.get("DBBASIC_DATA_DIR", "data")
    try:
        rows = object_records.read_collection_records("backorders",
                                                       base_dir=base)
    except Exception:
        return {"count": 0}

    waiting = [row for row in rows
               if _text(row.get("status")) == WAITING_STATUS]
    if not waiting:
        return {"count": 0}

    today = date.today()
    ages = [age for age in (_days_since(row.get("requested_on"), today)
                            for row in waiting)
            if age is not None and age >= 0]
    if not ages:
        return {"count": len(waiting)}
    return {"count": len(waiting), "detail": f"oldest waiting {max(ages)} days"}
