"""Pre-write hook for usage_events: one call, one charge.

Metering's characteristic failure is the duplicate: a client times out,
retries, and the customer is billed twice for one request. The caller
supplies an event_id precisely so that retry is safe, and this gate is
what makes the promise real -- a repeat within the same owner records
nothing rather than quietly adding to the bill.

Also refuses non-positive quantities. Negative usage is not a
correction, it is a mistake or an attack: usage is measured, and a
correction to a bill is a credit on the invoice or a wallet entry, never
a negative measurement retroactively unmeasuring something that happened
(docs/logic-decisions.md #3).
"""

import os
from decimal import Decimal, InvalidOperation

import object_records


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def BEFORE_WRITE(request):
    if request.get("action") != "create":
        return None
    record = request.get("record") or {}

    try:
        quantity = Decimal(str(record.get("quantity") or "0"))
    except InvalidOperation:
        return None  # schema validation reports the type error properly
    if quantity <= 0:
        return {"error": ("Usage quantity must be positive. A correction is a credit "
                          "on the bill, never a negative measurement that unmeasures "
                          "something that already happened."),
                "status": 400}

    event_id = str(record.get("event_id") or "").strip()
    if not event_id:
        return None  # required-field validation owns this
    owner = str(record.get("owner_id") or "")

    try:
        rows = object_records.read_collection_records("usage_events", base_dir=_base_dir())
    except Exception:
        # Unknown history: refusing is the safe direction for money.
        return {"error": "Usage ledger unreadable; refusing to record against an "
                         "unknown history.", "status": 409}
    for row in rows:
        if row.get("event_id") == event_id and str(row.get("owner_id") or "") == owner:
            return {
                "error": (f"Usage event {event_id} was already recorded. A retried "
                          "request must not be billed twice -- that is what the "
                          "event key is for."),
                "status": 409,
            }
    return None
