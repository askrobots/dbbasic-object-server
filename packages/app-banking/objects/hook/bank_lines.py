"""Pre-write hook for bank_lines: the duplicate gate on imported evidence.

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


def BEFORE_WRITE(request):
    if request.get("action") != "create":
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
