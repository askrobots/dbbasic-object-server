"""Pre-write hook for fin_journals: balance before posting, and closed
periods stay closed.

Two gates, both cross-record, both therefore hook territory
(plan/pre-write-hook-spec.md):

1. **A journal must balance before it posts.** The aggregate block on the
   journal detail SHOWS debits vs credits, but nothing else stops an
   unbalanced draft -> posted move -- the predecessor's own documented
   weak spot.

2. **A closed period is closed.** Once a stretch of the books is settled
   (fin_closed_periods), no journal may be created dated inside it, no
   journal dated inside it may be edited, and none may be moved in or out
   by changing its date. Without this, last year's "final" numbers stay
   quietly editable forever -- the filed statements and the live books
   drift apart with nobody deciding they should. Reopening is a deliberate
   act (delete the period row, attributed in the change log), never a
   side effect of an edit.

Rejection-only; never transforms. Ordinary edits to a draft journal in an
open period pass untouched. Machine composers write at the storage level
and bypass hooks by design -- the period gate is for human hands; a
composer landing in a closed period is a case that should surface, not be
silently swallowed here (tracked in the package description).
"""

import os

import object_finance
import object_records


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _period_error(period, date):
    reason = str(period.get("reason") or "").strip()
    label = f" ({reason})" if reason else ""
    return {
        "error": (f"The books from {period.get('start_date')} to {period.get('end_date')} "
                  f"are closed{label}; {date} falls inside. Corrections to a settled "
                  "period are new journals dated in the current period -- or, "
                  "deliberately, reopen the period by removing its fin_closed_periods "
                  "row (that act is attributed in the change log)."),
        "status": 409,
    }


def BEFORE_WRITE(request):
    action = request.get("action")
    record = request.get("record") or {}
    existing = request.get("existing") or {}
    changes = request.get("changes") or {}
    base = _base_dir()

    # --- gate 2: closed periods ------------------------------------------
    owner = record.get("owner_id") or existing.get("owner_id") or ""
    entity = record.get("entity_id") or existing.get("entity_id") or ""

    if action == "create":
        period = object_finance.closed_period_for(
            record.get("date"), base_dir=base, owner_id=owner, entity_id=entity)
        if period is not None:
            return _period_error(period, record.get("date"))

    if action == "update":
        # Editing anything about a journal dated in a closed period is
        # rewriting settled books; moving a date INTO one is backdating.
        for date in (existing.get("date"), changes.get("date")):
            if not date:
                continue
            period = object_finance.closed_period_for(
                date, base_dir=base, owner_id=owner, entity_id=entity)
            if period is not None:
                return _period_error(period, date)

    # --- gate 1: balance before posting -----------------------------------
    if action != "update":
        return None
    if changes.get("status") != "posted" or existing.get("status") == "posted":
        return None

    journal_id = record.get("id") or existing.get("id")
    lines = [
        line
        for line in object_records.read_collection_records("fin_journal_lines", base_dir=base)
        if line.get("journal_id") == journal_id
    ]
    if not lines:
        return {"error": "Cannot post an empty journal - add lines first.", "status": 409}

    debits = sum(object_finance.to_cents(line.get("debit_cents")) for line in lines)
    credits = sum(object_finance.to_cents(line.get("credit_cents")) for line in lines)
    if debits != credits:
        return {
            "error": (
                "Journal must balance before posting: "
                f"debits {debits} != credits {credits} (cents)."
            ),
            "status": 409,
        }
    return None
