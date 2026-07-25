"""Pre-write hook for expenses: the gate between spending and the books.

The same shape as hook_time_logs, because it is the same problem --
somebody other than the person who incurred the cost has to say yes
before it becomes a journal entry or a line on a client's bill.

One difference from time, and it matters: approval here does NOT require
the expense to be billable. Unbillable time is simply not billed;
unbillable spending still left the bank account, still belongs in the
ledger, and still has to be approved by somebody. Refusing to approve it
would leave real money unrecorded, which is a worse failure than an
unbilled hour.

Approval stamps the markup and the billable amount (docs/logic-decisions.md
#1), so re-running a bill can never apply this quarter's markup policy to
last quarter's approved costs. After that the expense is frozen -- a
mistake is corrected by a compensating entry or a credit, never by
editing the record of what was spent (#5, #8). The journal itself is a
REACTION and lives in system_expense_books (#6), not here: a books
mapping that is not configured yet must never block somebody from
recording that they bought a plane ticket.
"""

import os
from datetime import datetime, timezone

import object_rates
import object_records

_ALLOWED_AFTER_APPROVAL = {"status", "invoice_id", "notes", "receipt_ref"}


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _truthy(value):
    return str(value or "").strip().lower() in ("true", "1", "yes", "on")


def _int(value):
    try:
        return int(str(value or "0").strip() or 0)
    except (TypeError, ValueError):
        return 0


def _setting(base, key, default=""):
    try:
        for row in object_records.read_collection_records("app_settings", base_dir=base):
            if row.get("key") == key and str(row.get("value") or "").strip():
                return row["value"].strip()
    except Exception:
        pass
    return default


def BEFORE_WRITE(request):
    action = request.get("action")
    record = dict(request.get("record") or {})
    existing = request.get("existing") or {}
    changes = request.get("changes") or {}
    subject = request.get("subject") or {}
    actor = str(subject.get("user_id") or "")

    was = str(existing.get("status") or "") if action == "update" else ""
    now = str(record.get("status") or existing.get("status") or "draft")
    merged = {**existing, **record}

    if action == "create" and _int(merged.get("amount_cents")) <= 0:
        return {"error": ("An expense needs an amount. A refund or a credit "
                          "note is its own positive record on the other side, "
                          "never a negative spend."), "status": 400}

    # --- frozen once approved -------------------------------------------
    if action == "update" and was in ("approved", "billed"):
        touched = [field for field in changes
                   if field not in _ALLOWED_AFTER_APPROVAL]
        if touched:
            return {"error": (
                f"This expense is {was}; what was spent is settled and its "
                f"journal is already posted. Correct it with a compensating "
                f"entry or a credit, not by editing "
                f"{', '.join(sorted(touched))}."), "status": 409}
        if was == "billed" and now != "billed":
            return {"error": ("A billed expense cannot be reopened. A dispute "
                              "is a credit on the invoice, not a rewind."),
                    "status": 409}

    # --- submitting ------------------------------------------------------
    if now == "submitted" and was != "submitted":
        if _int(merged.get("amount_cents")) <= 0:
            return {"error": "An expense with no amount has nothing to approve.",
                    "status": 400}
        if not str(merged.get("incurred_on") or "").strip():
            return {"error": ("An expense needs the date it was incurred: that "
                              "is the period its journal belongs to."),
                    "status": 400}

    # --- approving -------------------------------------------------------
    if now == "approved" and was != "approved":
        owner = str(merged.get("owner_id") or "")
        if actor and owner and actor == owner:
            return {"error": ("Expenses are approved by somebody other than the "
                              "person who incurred them. Approving your own "
                              "spending is not an approval."), "status": 403}

        base = _base_dir()
        markup = merged.get("markup_bps")
        if markup in (None, ""):
            markup = _setting(base, "billing.expense_markup_bps", "0")
        markup_bps = _int(markup)
        amount = _int(merged.get("amount_cents"))

        record["markup_bps"] = str(markup_bps)
        record["billable_amount_cents"] = str(
            object_rates.with_markup(amount, markup_bps)
            if _truthy(merged.get("billable")) else 0)
        record["approved_by"] = actor
        record["approved_at"] = datetime.now(timezone.utc).isoformat()
        return {"record": record}

    return None
