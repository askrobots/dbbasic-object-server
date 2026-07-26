"""system_invoice_attention -- how many invoices are past their due date.

COUNT {} -> {count, detail}

Time changes an invoice's meaning with no write happening
(system_invoice_aging's whole premise), so "overdue" is a queue that
appears out of the calendar rather than out of anybody's action. That is
the kind of pile nothing draws attention to: no record changed, no event
fired, and the only person who finds out is the one who happens to open
the invoice list.

Severity `warning` rather than `normal` in the manifest, because this
queue is not work that arrived, it is work that is already late.

Three deliberate exclusions, each of which would otherwise inflate the
number into one nobody trusts:

- `paid` and `void` are settled, which is the obvious half.
- `draft` is NOT overdue however old it is. An invoice nobody sent cannot
  be late; counting it would mean chasing a customer who was never
  billed, and the fix is to send it, not to dun it.
- `doc_type` other than `invoice`. A quote's date is an expiry and a
  credit note's is an issue date; neither is money somebody owes.

The detail names the worst case rather than the total, because "45 days"
is what decides whether this is a reminder email or a phone call.

COUNT rather than GET or POST: the verb is the contract with the daemon's
attention pass. Nothing here writes. A missing collection reads as zero;
a genuine failure raises, so the rollup records it and keeps the last
count rather than reporting that nobody owes anything.
"""

import os
from datetime import date

import object_records

ACTOR = "system_invoice_attention"

# Settled, or never sent. Everything else with a due date in the past is
# money somebody owes today.
SETTLED_STATUSES = {"paid", "void", "draft"}


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _day(value):
    """A YYYY-MM-DD cell as a date, or None when it will not parse.

    An invoice with no due date is not overdue -- it has no promise to
    break -- and a hand-edited one that will not parse must not take the
    whole count down with it.
    """
    try:
        year, month, day = (int(part) for part in _text(value).split("-"))
        return date(year, month, day)
    except (ValueError, AttributeError):
        return None


def COUNT(request):
    try:
        rows = object_records.read_collection_records("invoices", base_dir=_base_dir())
    except Exception:
        return {"count": 0}

    today = date.today()
    late = []
    for row in rows:
        if _text(row.get("doc_type") or "invoice") != "invoice":
            continue
        if _text(row.get("status")) in SETTLED_STATUSES:
            continue
        due = _day(row.get("due_date"))
        if due is not None and due < today:
            late.append((today - due).days)

    if not late:
        return {"count": 0}
    return {"count": len(late), "detail": f"oldest {max(late)} days late"}
