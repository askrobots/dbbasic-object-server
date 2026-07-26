"""system_shipment_attention -- how many parcels stopped moving.

COUNT {} -> {count, detail}

A parcel that has been `in_transit` for longer than a fortnight is a
customer about to write in, and the shop nearly always finds out from the
customer rather than from the system. That asymmetry is the whole reason
this count exists: the facts were already on the shipment (the carrier's
own status, the day it was handed over, the day it was last read), and
nothing was folding them into the one sentence somebody can act on.

`in_transit` and nothing else, deliberately. `shipped` means we handed it
over and have heard nothing since -- for a manual shop that is EVERY parcel
forever, and a band that is permanently lit is a band nobody reads.
`in_transit` is the stronger fact: a carrier told us this parcel was moving,
and then it stopped. `delivered`, `lost` and `returned_to_sender` have all
had an answer, even if it was a bad one.

Age is measured from shipped_on -- the handover, stamped when the shipment
reaches `shipped` -- because that is the clock the customer is counting too.
A row with no handover date is not counted: "we do not know when this left"
is not a claim that it is late, and a queue padded with parcels whose age
nobody knows is a queue that gets ignored.

Severity `warning` rather than `normal` in the manifest: unlike a receipt
waiting to be confirmed, nobody chose to put this here, and the clock is
running somewhere else -- in a customer's inbox.

The threshold is one setting, `carrier.stuck_days`, default 10. Ten days is
long enough that ordinary ground post in a large country is not flagged and
short enough to beat the email.

Degrades to zero when shipments is absent (a deployment without app-shipping
installed) rather than raising, the same posture system_scan_attention takes,
because the pass should not log an error every five minutes about an app
nobody installed.
"""

import os
from datetime import date

import object_records

ACTOR = "system_shipment_attention"

# The one status that means a carrier said it was moving and then stopped.
WAITING_STATUS = "in_transit"

DEFAULT_STUCK_DAYS = 10


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _setting(base, key, default=""):
    """Duplicated on purpose, same as every other package that reads
    app_settings (docs/logic-decisions.md #4)."""
    try:
        for row in object_records.read_collection_records("app_settings",
                                                          base_dir=base):
            if row.get("key") == key and _text(row.get("value")):
                return _text(row["value"])
    except Exception:
        pass
    return default


def _days_since(value, today):
    """Whole days since a YYYY-MM-DD date, or None when it will not parse.

    A hand-typed or empty shipped_on returns None and the row is skipped
    rather than counted at age zero: an unreadable date must not silently
    become evidence that a parcel is fine.
    """
    try:
        return (today - date.fromisoformat(_text(value))).days
    except ValueError:
        return None


def COUNT(request):
    base = _base_dir()
    try:
        rows = object_records.read_collection_records("shipments", base_dir=base)
    except Exception:
        return {"count": 0}

    try:
        limit = int(_setting(base, "carrier.stuck_days", str(DEFAULT_STUCK_DAYS)))
    except ValueError:
        limit = DEFAULT_STUCK_DAYS

    today = date.today()
    ages = []
    for row in rows:
        if _text(row.get("direction")) == "inbound":
            continue
        if _text(row.get("status")) != WAITING_STATUS:
            continue
        age = _days_since(row.get("shipped_on"), today)
        if age is None or age < limit:
            continue
        ages.append(age)

    if not ages:
        return {"count": 0}
    return {"count": len(ages),
            "detail": f"oldest {max(ages)} days out"}
