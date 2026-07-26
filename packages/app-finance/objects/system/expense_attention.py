"""system_expense_attention -- how much spending is waiting for approval.

COUNT {} -> {count, detail}

The same gate as time logs, over money instead of hours: an expense at
`submitted` has been claimed and not yet decided, and the person who has
to decide is by design not the person who claimed it. Until somebody
looks, the books do not have it and the claimant is out of pocket, which
is the kind of queue people find out about through a complaint rather
than through a screen.

The detail is the total, in whole currency units, because approving four
coffees and approving a laptop are the same row count and very different
afternoons.

COUNT rather than GET or POST: the verb is the contract with the daemon's
attention pass. Nothing here writes. A missing collection reads as zero;
a genuine failure raises, so the rollup records the error and keeps the
last count rather than reporting an empty queue nobody emptied.
"""

import os

import object_records

ACTOR = "system_expense_attention"

# draft is still being written, approved/rejected have had a decision,
# billed is already on an invoice. `submitted` is the waiting room.
WAITING_STATUS = "submitted"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _cents(value):
    try:
        return int(_text(value) or 0)
    except ValueError:
        # A hand-edited amount must not take the count down with it.
        return 0


def COUNT(request):
    try:
        rows = object_records.read_collection_records("expenses", base_dir=_base_dir())
    except Exception:
        return {"count": 0}

    waiting = [row for row in rows if _text(row.get("status")) == WAITING_STATUS]
    if not waiting:
        return {"count": 0}

    total = sum(_cents(row.get("amount_cents")) for row in waiting)
    detail = f"{total / 100:,.2f} awaiting approval" if total else ""
    return {"count": len(waiting), "detail": detail}
