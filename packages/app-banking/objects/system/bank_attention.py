"""system_bank_attention -- how many bank lines nothing has matched.

COUNT {} -> {count, detail}

`match_status: unmatched` is the reconciliation queue in one field: money
moved in the real world and this server has no book record that explains
it. `system_bank_matcher` has already had its go and either found nothing
or found nothing it was confident enough to accept, so what is left is
precisely the set a person has to look at.

`suggested` is deliberately NOT counted. A suggestion is the machine
having done its job; the line is answerable in one click from /reconcile
and does not need the front page to say so. Only `unmatched` -- nothing
proposed, nobody has looked -- is a queue in the sense this layer means.

The detail names how many have been sitting there long enough that
`system_bank_escalation` would already have raised them, because a line
unmatched this morning and a line unmatched since March are the same row
count and completely different problems.

COUNT rather than GET or POST: the verb is the contract with the daemon's
attention pass. Nothing here writes. A missing collection reads as zero;
a genuine failure raises, so the rollup records it and keeps the last
count rather than reporting a reconciled bank that nobody reconciled.
"""

import os
from datetime import date

import object_records

ACTOR = "system_bank_attention"

# Nothing proposed and nobody has looked. `suggested` has a candidate,
# `matched` and `resolved` have an answer.
WAITING_STATUS = "unmatched"

# The same silence window system_bank_escalation defaults to, so the two
# surfaces cannot disagree about what "stale" means on one deployment.
STALE_DAYS = 7


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _day(value):
    try:
        year, month, day = (int(part) for part in _text(value).split("-"))
        return date(year, month, day)
    except (ValueError, AttributeError):
        # A line with no posting date still counts; it just says nothing
        # about age.
        return None


def COUNT(request):
    try:
        rows = object_records.read_collection_records("bank_lines", base_dir=_base_dir())
    except Exception:
        return {"count": 0}

    today = date.today()
    waiting = [row for row in rows
               if _text(row.get("match_status") or WAITING_STATUS) == WAITING_STATUS]
    if not waiting:
        return {"count": 0}

    stale = 0
    for row in waiting:
        posted = _day(row.get("posted_on"))
        if posted is not None and (today - posted).days >= STALE_DAYS:
            stale += 1
    detail = f"{stale} older than {STALE_DAYS} days" if stale else ""
    return {"count": len(waiting), "detail": detail}
