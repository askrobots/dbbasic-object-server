"""system_expense_books -- HANDLES expenses writes; posts the journal.

An approved expense composes one balanced entry:

    DR  the expense account        (what it was for)
    CR  the clearing account       (company card or cash), OR
    CR  the reimbursable liability (when a person paid personally)

That second credit is the whole reason paid_by exists as a field. An
employee's own card creates a DEBT the business owes them; booking it
against the company's cash account claims money left an account it never
left, and the person is left chasing a reimbursement the ledger has no
record of. Most expense tools model this as a flag on a report; here it
is the account the credit lands in, which is the only place it can
actually be true.

Placement follows docs/logic-decisions.md #6: this is a REACTION
(post-commit, best-effort, never blocks the write), so it lives in an
event handler rather than the hook. Somebody recording that they bought a
plane ticket must not be stopped because nobody has mapped a chart of
accounts yet -- an unconfigured books mapping skips with a reason and the
expense stands.

Configuration (app_settings):
  expenses.journal.expense_account       default DR when an expense names none
  expenses.journal.paid_account          CR for company-paid spend
  expenses.journal.reimbursable_account  CR for personally-paid spend

Idempotency by provenance: generated_from "expenses/{id}", so a replayed
event composes nothing (doctrine #7).
"""

import os

import object_finance
import object_records

HANDLES = [
    "expenses.record.created",
    "expenses.record.updated",
]

ACTOR = "system_expense_books"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _setting(base, key, default=""):
    try:
        for row in object_records.read_collection_records("app_settings", base_dir=base):
            if row.get("key") == key and str(row.get("value") or "").strip():
                return row["value"].strip()
    except Exception:
        pass
    return default


def _int(value):
    try:
        return int(str(value or "0").strip() or 0)
    except (TypeError, ValueError):
        return 0


def _text(value):
    return str(value if value is not None else "").strip()


def _record_for(request, base):
    record = request.get("record")
    if isinstance(record, dict) and record.get("id"):
        return record
    record_id = _text(request.get("record_id") or request.get("id"))
    if not record_id:
        return None
    try:
        return object_records.get_collection_record("expenses", record_id, base_dir=base)
    except Exception:
        return None


def POST(request):
    base = _base_dir()
    expense = _record_for(request, base)
    if not expense:
        return {"ok": True, "skipped": "no expense in the event"}
    if _text(expense.get("status")) not in ("approved", "billed"):
        # Nothing is owed and nothing was authorised until somebody
        # approved it; a draft expense is a note to self.
        return {"ok": True, "skipped": "not approved"}

    amount = _int(expense.get("amount_cents"))
    if amount <= 0:
        return {"ok": True, "skipped": "zero amount"}

    debit = (_text(expense.get("account_id"))
             or _setting(base, "expenses.journal.expense_account"))
    personal = _text(expense.get("paid_by")) == "personal"
    credit = _setting(
        base,
        "expenses.journal.reimbursable_account" if personal
        else "expenses.journal.paid_account")
    if not debit or not credit:
        return {"ok": True, "skipped": "accounts unconfigured",
                "needs": ["expenses.journal.expense_account",
                          "expenses.journal.reimbursable_account" if personal
                          else "expenses.journal.paid_account"]}

    memo = ("reimbursable to " + _text(expense.get("owner_id")) if personal
            else "paid by the company")
    return object_finance.compose_posted_journal(
        base,
        generated_from=f"expenses/{expense['id']}",
        date=_text(expense.get("incurred_on")),
        description=f"Expense: {_text(expense.get('description')) or expense['id']}",
        lines=[
            {"account_id": debit, "debit_cents": amount, "credit_cents": 0,
             "memo": _text(expense.get("description"))},
            {"account_id": credit, "debit_cents": 0, "credit_cents": amount,
             "memo": memo},
        ],
        owner_id=_text(expense.get("owner_id")),
        entity_id=_text(expense.get("entity_id")),
        actor=ACTOR,
    )
