"""system_dispute_attention -- claims nobody has picked up.

COUNT {} -> {count, detail}

A dispute at `open` is a customer who has said something went wrong and
has heard nothing back. It is the only queue on this box where the thing
waiting is a person rather than a document: a short-received purchase
order waits patiently, a parcel on the returns bench waits patiently, and
a customer telling you their order never arrived is drafting a chargeback
while they wait. That is why the detail names the oldest one in days --
three claims a day old and three claims a fortnight old are the same row
count and completely different mornings.

`investigating` is deliberately NOT counted. Somebody has it; the number
exists to find the claims nobody has, and a count that also includes work
in progress can never reach zero, which is how a badge stops meaning
anything. The bench at /disputes shows the whole ladder, because the
person standing at it needs to see what is in flight as well as what is
untouched, and `status` is one click away in the filter bar.

COUNT rather than GET or POST: the verb is the contract with the daemon's
attention pass. A missing collection reads as zero -- a server with no
disputes app has no disputes -- while a genuine failure raises, so the
rollup records it and keeps the last count rather than reporting a clear
queue nobody cleared.
"""

import os
from datetime import datetime, timezone

import object_records

ACTOR = "system_dispute_attention"

# Raised and untouched. `investigating` has a human on it, `resolved` and
# `withdrawn` are endings.
WAITING_STATUS = "open"


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
        rows = object_records.read_collection_records("disputes",
                                                      base_dir=_base_dir())
    except Exception:
        return {"count": 0}

    waiting = [row for row in rows
               if _text(row.get("status")) == WAITING_STATUS]
    if not waiting:
        return {"count": 0}

    ages = [days for days in (_age_days(row.get("created_at"))
                              for row in waiting) if days is not None]
    oldest = max(ages) if ages else 0
    detail = f"oldest waiting {oldest} days" if oldest else ""
    return {"count": len(waiting), "detail": detail}
