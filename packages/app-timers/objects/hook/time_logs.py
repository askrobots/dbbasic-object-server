"""Pre-write hook for time_logs: the gate between hours and money.

Consulting hours are a usage metric with an approval gate, and this is
the gate. Three things happen here and nowhere else:

**Nothing is submitted while the clock is running.** An entry with no
ended_at has no duration, and billing a duration that is still growing
means the number on the invoice was never the number anyone approved.

**Nobody approves their own hours.** An approval you can grant yourself
is not an approval -- it is a formality with an audit trail. This is the
one rule here that exists for the reader of the invoice rather than for
the person entering time.

**Approval stamps the rate.** The card in force on the day the work
happened is resolved and written onto the entry (docs/logic-decisions.md
#1), so a rate change next quarter cannot reprice work already approved,
and "why am I being charged this?" stays answerable years later via
rate_card_id.

After that the entry is frozen. Correcting approved or billed time is a
compensating entry or a credit on the invoice, never an edit to the
record of what happened (#5, #8). Note the standing posture: hooks gate
the PUBLIC write surface, so the invoice generator -- a trusted
server-side writer going through object_records directly -- stamps
invoice_id and moves approved to billed without asking permission here.
"""

import os
from datetime import datetime, timezone

import object_rates
import object_records

# What an entry may still change once a human has approved it. Everything
# else about it is now evidence.
_ALLOWED_AFTER_APPROVAL = {"status", "invoice_id", "notes"}


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _truthy(value):
    return str(value or "").strip().lower() in ("true", "1", "yes", "on")


def _int(value):
    try:
        return int(str(value or "0").strip() or 0)
    except (TypeError, ValueError):
        return 0


def BEFORE_WRITE(request):
    action = request.get("action")
    record = dict(request.get("record") or {})
    existing = request.get("existing") or {}
    changes = request.get("changes") or {}
    subject = request.get("subject") or {}
    actor = str(subject.get("user_id") or "")

    was = str(existing.get("status") or "") if action == "update" else ""
    now = str(record.get("status") or existing.get("status") or "draft")

    # --- frozen once approved -------------------------------------------
    if action == "update" and was in ("approved", "billed"):
        touched = [field for field in changes
                   if field not in _ALLOWED_AFTER_APPROVAL]
        if touched:
            return {"error": (
                f"This time entry is {was} and its record of what happened is "
                f"settled. Correct it with a compensating entry or a credit on "
                f"the invoice, not by editing "
                f"{', '.join(sorted(touched))}."), "status": 409}
        if was == "billed" and now != "billed":
            return {"error": ("Billed time cannot be reopened. A disputed hour "
                              "is a credit on the invoice, not a rewind."),
                    "status": 409}

    # --- submitting ------------------------------------------------------
    if now == "submitted" and was != "submitted":
        ended = str(record.get("ended_at") or existing.get("ended_at") or "").strip()
        running = _truthy(record.get("is_running", existing.get("is_running")))
        if running or not ended:
            return {"error": ("Stop the timer before submitting. An entry that "
                              "is still running has no duration to approve."),
                    "status": 400}
        if _int(record.get("duration_seconds", existing.get("duration_seconds"))) <= 0:
            return {"error": "A time entry with no elapsed time has nothing to bill.",
                    "status": 400}

    # --- approving -------------------------------------------------------
    if now == "approved" and was != "approved":
        merged = {**existing, **record}
        if not _truthy(merged.get("billable")):
            return {"error": ("Only billable time can be approved for billing. "
                              "Mark it billable, or leave it as recorded work."),
                    "status": 400}
        owner = str(merged.get("owner_id") or "")
        if actor and owner and actor == owner:
            return {"error": ("Time is approved by somebody other than the "
                              "person who logged it. An approval you can grant "
                              "yourself is not an approval."), "status": 403}

        try:
            cards = object_records.read_collection_records(
                "rate_cards", base_dir=_base_dir())
        except Exception:
            cards = []
        rated = object_rates.rate_entry(merged, cards)
        if rated["unrated_reason"]:
            return {"error": (f"No rate applies to this time: "
                              f"{rated['unrated_reason']}. Add a rate card "
                              f"before approving it, or the hours would be "
                              f"approved at nothing."), "status": 409}

        record["hourly_rate_cents"] = str(rated["rate_cents"])
        record["amount_cents"] = str(rated["amount_cents"])
        record["rate_card_id"] = rated["rate_card_id"]
        record["approved_by"] = actor
        record["approved_at"] = datetime.now(timezone.utc).isoformat()
        return {"record": record}

    return None
