"""Pre-write hook for fin_journal_lines: a settled journal's lines are
settled too.

The closed-period gate on fin_journals stops the journal record itself
from being touched -- but a journal's AMOUNTS live in its lines, and
without this hook a line added to (or edited on) a journal dated inside a
closed period would change settled books while the journal record sat
innocently untouched. Same period, same rule, checked from the line's
side: look up the parent journal, and if its date falls in a closed
period, refuse.

Rejection-only. Lines on journals in open periods pass untouched --
including the ordinary compose flow of drafting a journal and adding its
lines. A line whose journal_id does not resolve is left to relation
validation, which owns missing parents.
"""

import os

import object_finance
import object_records


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def BEFORE_WRITE(request):
    if request.get("action") not in ("create", "update"):
        return None
    record = request.get("record") or {}
    existing = request.get("existing") or {}
    journal_id = str(record.get("journal_id") or existing.get("journal_id") or "").strip()
    if not journal_id:
        return None  # required/relation validation owns this

    base = _base_dir()
    try:
        journal = object_records.get_collection_record("fin_journals", journal_id, base_dir=base)
    except Exception:
        return None  # relation validation owns a missing journal

    period = object_finance.closed_period_for(
        journal.get("date"), base_dir=base,
        owner_id=journal.get("owner_id") or "",
        entity_id=journal.get("entity_id") or "")
    if period is None:
        return None

    reason = str(period.get("reason") or "").strip()
    label = f" ({reason})" if reason else ""
    return {
        "error": (f"This line belongs to a journal dated {journal.get('date')}, inside "
                  f"the closed period {period.get('start_date')} to "
                  f"{period.get('end_date')}{label}. Settled amounts are corrected by a "
                  "new journal in the current period, never by editing the old one's "
                  "lines."),
        "status": 409,
    }
