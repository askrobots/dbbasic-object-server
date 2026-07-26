"""system_scheduler_attention -- scheduled work that failed overnight.

COUNT {} -> {count, detail}

Severity `urgent` in the manifest, and it is the only provider on this
server that gets it, because every other queue on the home page is work
waiting for a person, and this one is the machine reporting that it
stopped doing its own. A failed nightly pass is invisible by
construction: the invoice aging that did not run raises nothing, emails
nobody, and looks exactly like a night with no overdue invoices. That is
the failure `scheduler_runs` was created to end -- runs became a RECORD
instead of a stdout line only ssh could reach -- and this object is the
half that makes somebody read them.

A 24-hour window rather than "since you last looked", because the counts
have no per-user state and never should: this is a fact about the server,
identical for everyone, and a window that depends on who is asking is a
number two people can argue about. Yesterday's failure that has since
succeeded still counts today; a pass that is broken will keep counting
tomorrow, and one that was transient ages out on its own.

The detail names how many distinct TASKS are affected, because one task
failing forty times is one broken pass and forty tasks failing once each
is an incident.

COUNT rather than GET or POST: the verb is the contract with the daemon's
attention pass. A missing collection reads as zero; a genuine failure
raises, so the rollup records it and keeps the last count rather than
reporting a healthy scheduler nobody checked.
"""

import os
from datetime import datetime, timedelta, timezone

import object_records

ACTOR = "system_scheduler_attention"

WINDOW_HOURS = 24
# An `ok` that is blank is unknown, not failed. Counting it would turn a
# hand-edited or half-written row into a fake incident, and the point of
# an urgent badge is that it is never crying wolf.
FALSE_TEXT = ("false", "0", "no", "off")


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _stamp(value):
    text = _text(value).replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def COUNT(request):
    try:
        rows = object_records.read_collection_records("scheduler_runs",
                                                      base_dir=_base_dir())
    except Exception:
        return {"count": 0}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    failures = []
    for row in rows:
        if _text(row.get("ok")).lower() not in FALSE_TEXT:
            continue
        # A run with no readable start time is still a run, but it cannot
        # be placed in the window, so it is left out rather than counted
        # forever.
        started = _stamp(row.get("started_at")) or _stamp(row.get("created_at"))
        if started is None or started < cutoff:
            continue
        failures.append(_text(row.get("task_id")) or _text(row.get("object_id")))

    if not failures:
        return {"count": 0}
    tasks = len({name for name in failures if name})
    detail = f"across {tasks} task(s) in the last {WINDOW_HOURS}h" if tasks else ""
    return {"count": len(failures), "detail": detail}
