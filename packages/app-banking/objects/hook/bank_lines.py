"""Pre-write hook for bank_lines: evidence stays evidence.

Two jobs. On CREATE, the duplicate gate. On UPDATE, the rule that makes
this collection an anti-fraud control rather than just another table:
**the bank's words are immutable.** posted_on, amount_cents, description,
raw and the identity fields can never be edited -- a correction comes from
re-importing a corrected statement, never from reshaping the evidence to
agree with the books. What an operator MAY change is their own
reconciliation work: match_status, matched_to, resolved_as, suggestions.

Also gated: claiming a line is `matched` without saying what it matched is
a status with no substance, so matched_to must be present and well-formed.

A statement line must land exactly once per bank account. The bank's own
transaction id (OFX FITID) is the strongest key; CSV exports rarely carry
one, so we fall back to a content hash that is ordinal-disambiguated --
two identical $4.50 coffees on the same day are both real and both land,
but re-importing last month's file adds nothing (plan/bank-import-
reconciliation-spec.md section 2).

Gates only, per docs/business-logic-patterns.md: this rejects, it never
reacts. The importer action performs the same check itself (it writes at
the storage level, which never reaches this hook) -- this hook is what
protects the public HTTP write surface, so a client or agent POSTing lines
directly gets the same guarantee.
"""

import os

import object_banking


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _check_update(request):
    existing = request.get("existing") or {}
    changes = request.get("changes") or {}

    touched = object_banking.evidence_changes(existing, changes)
    if touched:
        return {
            "error": ("A bank line records what the bank said and cannot be edited "
                      f"({', '.join(touched)}). Import a corrected statement instead -- "
                      "reconciliation means explaining the difference between the bank "
                      "and the books, never editing one to match the other."),
            "status": 409,
        }

    if changes.get("match_status") == "matched":
        target = str(changes.get("matched_to") or existing.get("matched_to") or "").strip()
        if not target:
            return {"error": "Set matched_to (e.g. payments/<id>) when marking a line matched.",
                    "status": 400}
        if target.count("/") != 1 or not all(part.strip() for part in target.split("/")):
            return {"error": f"matched_to must look like collection/record_id, got {target!r}.",
                    "status": 400}
    return None


def BEFORE_WRITE(request):
    action = request.get("action")
    if action == "update":
        return _check_update(request)
    if action != "create":
        return None
    record = request.get("record") or {}

    if not str(record.get("bank_account_id") or "").strip():
        return None  # required-field validation owns this

    stamped = dict(record)
    if not str(stamped.get("line_hash") or "").strip():
        try:
            amount = int(stamped.get("amount_cents") or 0)
        except (TypeError, ValueError):
            return None  # schema validation reports the type error properly
        stamped["line_hash"] = object_banking.line_hash(
            str(stamped.get("posted_on") or ""), amount,
            str(stamped.get("description") or ""))

    if object_banking.is_duplicate_line(stamped, base_dir=_base_dir()):
        which = "transaction id" if stamped.get("external_id") else "date/amount/description"
        return {
            "error": (f"This statement line is already on file for this bank account "
                      f"(matched on {which}). Bank lines are append-only evidence: "
                      f"re-importing a statement never duplicates it."),
            "status": 409,
        }

    if stamped.get("line_hash") != record.get("line_hash"):
        return {"record": stamped}
    return None
