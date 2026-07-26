"""system_scan_attention -- how many receipts are waiting for a person.

COUNT {} -> {count, detail}

A scan at `extracted` is the whole intake pipeline's honest stopping
point: the OCR ran, the extractor produced fields, and the machine
declined to turn somebody's money into an expense record on its own. That
is a waiting room with a person's name on it, and until now nothing said
so out loud -- `system_scan_processor` has been returning a key literally
called `needing_a_human` since the day it was written, and no surface has
ever read it. This object is that number, declared in app-intake's
manifest so the home page can fold it without knowing anything about
scans.

COUNT rather than GET or POST because the verb is the contract: the
daemon's attention pass calls COUNT, and a provider stays free to expose
the other methods to a human without either use accidentally answering
the other. Nothing here writes.

Degrades to zero when the collection is absent (app-intake schema not
installed on this deployment) rather than raising, because the pass
should not log an error every five minutes about an app nobody installed.
A provider that genuinely FAILS is a different thing and does raise --
the rollup records that as an error and keeps the last count, so a broken
counter looks broken instead of looking calm.
"""

import os
from datetime import datetime, timezone

import object_records

ACTOR = "system_scan_attention"

# The one status that means "a machine finished and a human has not".
# pending/processing are the pipeline's own business; confirmed, ignored
# and error have all had a decision made about them.
WAITING_STATUS = "extracted"

# A receipt nobody has confirmed within a week is not just queued, it is
# aging -- and the aging is the sentence a bare number cannot say.
STALE_DAYS = 7


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _age_days(value):
    """Days since an ISO timestamp, or None when it will not parse.

    A hand-edited or empty created_at must not take the count down with
    it: the row still counts, it just contributes nothing to the detail.
    """
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
        rows = object_records.read_collection_records("scans", base_dir=_base_dir())
    except Exception:
        return {"count": 0}

    waiting = [row for row in rows if _text(row.get("status")) == WAITING_STATUS]
    if not waiting:
        return {"count": 0}

    stale = sum(1 for row in waiting
                if (_age_days(row.get("created_at")) or 0) >= STALE_DAYS)
    detail = f"{stale} over a week old" if stale else ""
    return {"count": len(waiting), "detail": detail}
