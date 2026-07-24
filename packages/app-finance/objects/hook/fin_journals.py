"""Pre-write hook for fin_journals: a journal must balance before it posts.

The documented "displayed but not enforced" gap, closed: the aggregate block
on the journal detail SHOWS debits vs credits, but nothing stopped an
unbalanced journal from moving draft -> posted. That rule is cross-collection
(it sums fin_journal_lines) so the schema can't express it -- exactly what
`hooks.before_write` exists for (plan/pre-write-hook-spec.md).

Runs synchronously inside the generic write path, after permission checks and
the transition guard's own subject check, before persist. Rejection-only: it
never transforms the record. Ordinary edits to a draft journal (description,
date, lines-in-progress) pass through untouched -- only the draft -> posted
move is gated.
"""

import os

import object_records


def BEFORE_WRITE(request):
    if request.get("action") != "update":
        return None
    changes = request.get("changes") or {}
    existing = request.get("existing") or {}
    if changes.get("status") != "posted" or existing.get("status") == "posted":
        return None

    journal_id = (request.get("record") or {}).get("id") or existing.get("id")
    base_dir = os.environ.get("DBBASIC_DATA_DIR", "data")
    lines = [
        line
        for line in object_records.read_collection_records("fin_journal_lines", base_dir=base_dir)
        if line.get("journal_id") == journal_id
    ]
    if not lines:
        return {"error": "Cannot post an empty journal - add lines first.", "status": 409}

    debits = sum(int(line.get("debit_cents") or 0) for line in lines)
    credits = sum(int(line.get("credit_cents") or 0) for line in lines)
    if debits != credits:
        return {
            "error": (
                "Journal must balance before posting: "
                f"debits {debits} != credits {credits} (cents)."
            ),
            "status": 409,
        }
    return None
