"""Reverse a posted journal: compose and post its mirror entry.

Posted journals are immutable by design (transitions: posted -> []); every
correction is a new compensating record (docs/logic-decisions.md #3). This
action is the one-click version of that doctrine -- and the first real
instance of a schema-derived action (the record's real verb, not an edit).

POST payload: {"journal_id": "..."}. Allowed for the journal's owner or an
admin (the executing identity arrives in request["_identity"]). Refuses
non-posted journals and journals already reversed (generated_from scan).
The mirror swaps every line's debit/credit, stamps kind=reversing +
generated_from="reversal:{id}", and posts (a mirror of a balanced entry is
balanced by construction; re-verified anyway).
"""

import os

import object_finance
import object_records

ACTOR = "action_reverse_journal"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def POST(request):
    identity = request.get("_identity") or {}
    user_id = identity.get("user_id") or ""
    roles = identity.get("roles") or []
    journal_id = str(request.get("journal_id") or "").strip()
    if not journal_id:
        return {"status": 400, "error": "journal_id is required"}

    base = _base_dir()
    try:
        journal = object_records.get_collection_record("fin_journals", journal_id, base_dir=base)
    except Exception:
        return {"status": 404, "error": f"Journal not found: {journal_id}"}

    is_admin = "admin" in roles
    if not is_admin and (not user_id or journal.get("owner_id") != user_id):
        return {"status": 403, "error": "Only the journal's owner (or an admin) may reverse it."}
    if journal.get("status") != "posted":
        return {"status": 409, "error": "Only a posted journal can be reversed; edit the draft instead."}

    marker = f"reversal:{journal_id}"
    existing = object_finance.find_journal_by_provenance(base, marker)
    if existing is not None:
        return {"status": 409, "error": f"Already reversed by journal {existing.get('id')}."}

    lines = [
        l for l in object_records.read_collection_records("fin_journal_lines", base_dir=base)
        if l.get("journal_id") == journal_id
    ]
    if not lines:
        return {"status": 409, "error": "Journal has no lines to reverse."}

    composed = object_finance.compose_posted_journal(
        base,
        generated_from=marker,
        date=journal.get("date", ""),
        description=f"Reversal of {journal.get('reference') or journal.get('description') or journal_id}",
        lines=[
            {
                "account_id": line.get("account_id", ""),
                "debit_cents": line.get("credit_cents", "0"),   # swapped
                "credit_cents": line.get("debit_cents", "0"),   # swapped
                "memo": line.get("memo", ""),
                "entity_id": line.get("entity_id", ""),
            }
            for line in lines
        ],
        owner_id=journal.get("owner_id", ""),
        entity_id=journal.get("entity_id", ""),
        kind="reversing",
        actor=ACTOR,
    )
    if not composed.get("ok"):
        return {"status": 409, "error": composed.get("error", "could not compose the mirror")}
    if composed.get("skipped"):
        return {"status": 409, "error": f"Already reversed ({composed.get('journal_id', 'unknown')})."}
    if composed.get("posted"):
        return {"status": 200, "reversal_id": composed["journal_id"], "posted": True}
    return {"status": 200, "reversal_id": composed["journal_id"], "posted": False,
            "note": "left draft: mirror did not balance"}
