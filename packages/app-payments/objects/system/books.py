"""system_books -- HANDLES payments/refunds/invoices writes; composes journal
entries (the books spine, plan/books-spine-spec.md).

The billing ledger's reaction layer: when money moves operationally, the
matching double-entry lands in fin_journals -- posted, provenance-stamped,
idempotent. Placement per docs/logic-decisions.md #6: this is a REACTION
(post-commit, best-effort, never blocks or fails the source write), so it
lives in an event handler, not a hook.

Basis and account mapping are configuration (app_settings):
  payments.accounting_basis           cash (default) | accrual
  payments.journal.cash_account       fin_accounts id
  payments.journal.receivable_account fin_accounts id (accrual)
  payments.journal.revenue_account    fin_accounts id
Soft dependency: missing fin_journals collection or unmapped accounts ->
skip with a reason in the result; billing works without books.

Idempotency by provenance: every composed journal stamps generated_from
("payments/{id}", "refunds/{id}", "payments/{id}:bounced",
"invoices/{id}:issued"); an existing stamp means a replayed event composes
nothing. Entries are balanced BY CONSTRUCTION (one amount, two lines) and
this composer re-verifies before posting -- note: direct object_records
writes bypass the HTTP-only balance hook, so the composer carries its own
check; the hook remains the gate for human entries.
"""

import json
import os

import object_ids
import object_records

HANDLES = [
    "payments.record.created",
    "payments.record.updated",
    "refunds.record.created",
    "invoices.record.created",
    "invoices.record.updated",
]

ACTOR = "system_books"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _setting(base, key, default=""):
    try:
        for row in object_records.read_collection_records("app_settings", base_dir=base):
            if row.get("key") == key and row.get("value"):
                return row["value"].strip()
    except Exception:
        pass
    return default


def _journal_exists(base, generated_from):
    try:
        for row in object_records.read_collection_records("fin_journals", base_dir=base):
            if row.get("generated_from") == generated_from:
                return True
    except Exception:
        return True  # cannot tell -> do not risk a duplicate
    return False


def _books_ready(base):
    try:
        object_records.read_collection_records("fin_journals", base_dir=base)
        object_records.read_collection_records("fin_journal_lines", base_dir=base)
    except Exception:
        return False
    return True


def _compose(base, *, generated_from, date, description, debit_account,
             credit_account, amount_cents, owner_id, entity_id="", kind="standard"):
    """One balanced entry: DR debit_account / CR credit_account, posted."""
    if amount_cents <= 0:
        return {"ok": True, "skipped": "zero amount"}
    if _journal_exists(base, generated_from):
        return {"ok": True, "skipped": f"already composed: {generated_from}"}
    journal_id = object_ids.new_uuid4()
    journal = {
        "id": journal_id,
        "date": date or "",
        "description": description,
        "status": "draft",
        "generated_from": generated_from,
        "kind": kind,
        "owner_id": owner_id or "",
    }
    if entity_id:
        journal["entity_id"] = entity_id
    object_records.create_collection_record(
        "fin_journals", journal, base_dir=base, actor=ACTOR,
        allow_computed_submission=False,
    )
    for account_id, debit, credit in (
        (debit_account, str(amount_cents), "0"),
        (credit_account, "0", str(amount_cents)),
    ):
        line = {
            "id": object_ids.new_uuid4(),
            "journal_id": journal_id,
            "account_id": account_id,
            "debit_cents": debit,
            "credit_cents": credit,
            "owner_id": owner_id or "",
        }
        if entity_id:
            line["entity_id"] = entity_id
        object_records.create_collection_record(
            "fin_journal_lines", line, base_dir=base, actor=ACTOR
        )
    # Balanced by construction; verify anyway before posting (this path
    # bypasses the HTTP balance hook, so the composer carries its own check).
    lines = [l for l in object_records.read_collection_records("fin_journal_lines", base_dir=base)
             if l.get("journal_id") == journal_id]
    debits = sum(int(l.get("debit_cents") or 0) for l in lines)
    credits = sum(int(l.get("credit_cents") or 0) for l in lines)
    if debits == credits and debits > 0:
        object_records.update_collection_record(
            "fin_journals", journal_id, {"status": "posted"}, base_dir=base, actor=ACTOR
        )
        return {"ok": True, "journal_id": journal_id, "posted": True}
    return {"ok": True, "journal_id": journal_id, "posted": False,
            "note": "left draft: did not balance"}


def _get(base, collection, record_id):
    try:
        return object_records.get_collection_record(collection, record_id, base_dir=base)
    except Exception:
        return None


def EVENT(request):
    """request = {"event","collection","record_id","action"} (no body --
    refetch post-commit). Best-effort: every failure returns a reason, never
    raises into the dispatcher."""
    collection = str(request.get("collection") or "")
    record_id = str(request.get("record_id") or "")
    # The dispatcher's payload carries the RAW action ("create"); the event
    # NAME uses the participle ("...record.created"). Accept both.
    action = str(request.get("action") or "")
    action = {"create": "created", "update": "updated", "delete": "deleted"}.get(action, action)
    if not record_id:
        return {"ok": True, "skipped": "no record id"}

    base = _base_dir()
    if not _books_ready(base):
        return {"ok": True, "skipped": "books not installed (fin_journals absent)"}

    basis = _setting(base, "payments.accounting_basis", "cash")
    cash = _setting(base, "payments.journal.cash_account")
    receivable = _setting(base, "payments.journal.receivable_account")
    revenue = _setting(base, "payments.journal.revenue_account")

    try:
        if collection == "payments":
            payment = _get(base, "payments", record_id)
            if payment is None:
                return {"ok": True, "skipped": "payment gone"}
            invoice = _get(base, "invoices", payment.get("invoice_id", "")) or {}
            amount = int(payment.get("amount_cents") or 0)
            entity = invoice.get("entity_id", "")
            counter = receivable if basis == "accrual" else revenue
            if not cash or not counter:
                return {"ok": True, "skipped": "accounts unconfigured"}
            if action == "created" and (payment.get("status") or "received") == "received":
                return _compose(
                    base, generated_from=f"payments/{record_id}",
                    date=payment.get("received_on"),
                    description=f"Payment {payment.get('reference') or record_id} "
                                f"for {invoice.get('number') or payment.get('invoice_id')}",
                    debit_account=cash, credit_account=counter,
                    amount_cents=amount, owner_id=payment.get("owner_id"),
                    entity_id=entity,
                )
            if action == "updated" and payment.get("status") == "bounced":
                if not _journal_exists(base, f"payments/{record_id}"):
                    return {"ok": True, "skipped": "no original entry to reverse"}
                return _compose(
                    base, generated_from=f"payments/{record_id}:bounced",
                    date=payment.get("received_on"),
                    description=f"Bounce reversal of payment {record_id}",
                    debit_account=counter, credit_account=cash,
                    amount_cents=amount, owner_id=payment.get("owner_id"),
                    entity_id=entity, kind="reversing",
                )
            return {"ok": True, "skipped": "no entry for this payment event"}

        if collection == "refunds" and action == "created":
            refund = _get(base, "refunds", record_id)
            if refund is None:
                return {"ok": True, "skipped": "refund gone"}
            invoice = _get(base, "invoices", refund.get("invoice_id", "")) or {}
            counter = receivable if basis == "accrual" else revenue
            if not cash or not counter:
                return {"ok": True, "skipped": "accounts unconfigured"}
            return _compose(
                base, generated_from=f"refunds/{record_id}",
                date=refund.get("refunded_on"),
                description=f"Refund on payment {refund.get('payment_id')}",
                debit_account=counter, credit_account=cash,
                amount_cents=int(refund.get("amount_cents") or 0),
                owner_id=refund.get("owner_id"),
                entity_id=invoice.get("entity_id", ""),
            )

        if collection == "invoices":
            if basis != "accrual":
                return {"ok": True, "skipped": "cash basis: no issue entry"}
            invoice = _get(base, "invoices", record_id)
            if invoice is None or invoice.get("status") != "sent":
                return {"ok": True, "skipped": "not an issued invoice"}
            if not receivable or not revenue:
                return {"ok": True, "skipped": "accounts unconfigured"}
            return _compose(
                base, generated_from=f"invoices/{record_id}:issued",
                date=invoice.get("issue_date"),
                description=f"Invoice {invoice.get('number') or record_id} issued",
                debit_account=receivable, credit_account=revenue,
                amount_cents=int(invoice.get("total_cents") or 0),
                owner_id=invoice.get("owner_id"),
                entity_id=invoice.get("entity_id", ""),
            )
    except Exception as exc:  # never break the dispatcher
        return {"ok": False, "error": str(exc)[:200]}
    return {"ok": True, "skipped": "unhandled event"}
