"""system_time_attention -- how much logged time is waiting for an approver.

COUNT {} -> {count, detail}

`submitted` on a time log is a gate with a second person in it: the
approver is deliberately not the person who logged the hours, so this
queue can only ever be cleared by somebody who has no reason to know it
exists. That is exactly the shape of queue that quietly grows for a month
and then turns into an invoicing argument, and exactly what an attention
count is for.

The detail carries hours rather than a second count, because "9 entries"
and "62 hours" are different amounts of urgency and only the second one
tells an approver how much of their afternoon this is.

COUNT rather than GET or POST: the verb is the contract with the daemon's
attention pass. Nothing here writes. A missing collection reads as zero
(app-timers not installed here); a genuine failure raises, so the rollup
records an error and keeps the last count instead of reporting calm.
"""

import os

import object_records

ACTOR = "system_time_attention"

# draft is still being worked on, approved/rejected have had a decision,
# billed is on an invoice. `submitted` is the one that is waiting.
WAITING_STATUS = "submitted"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _seconds(value):
    try:
        return int(_text(value) or 0)
    except ValueError:
        # A hand-edited duration must not take the count down with it.
        return 0


def COUNT(request):
    try:
        rows = object_records.read_collection_records("time_logs", base_dir=_base_dir())
    except Exception:
        return {"count": 0}

    waiting = [row for row in rows if _text(row.get("status")) == WAITING_STATUS]
    if not waiting:
        return {"count": 0}

    hours = sum(_seconds(row.get("duration_seconds")) for row in waiting) / 3600.0
    detail = f"{hours:.1f} hours awaiting approval" if hours else ""
    return {"count": len(waiting), "detail": detail}
