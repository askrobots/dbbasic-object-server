"""system_invoice_status -- HANDLES payment/refund writes; flips invoice
paid/partial state the moment money moves.

The event-driven half of invoice aging (plan/payments-spec.md slice 2):
paid/partial react to payments IMMEDIATELY (a customer who just paid must not
stay "overdue" until tomorrow's daemon pass); the time-driven half (sent ->
overdue, dunning escalation) lives in system_invoice_aging, per
docs/logic-decisions.md #2.

Reads the invoice's DERIVED truth (amount_paid_cents -- the payments/refunds
rollup chain) after the write that changed it; the storage layer has already
recomputed it by dispatch time. Only touches workflow states (sent, partial,
overdue, paid); draft and void are never auto-flipped.
"""

import os

import object_records

HANDLES = [
    "payments.record.created",
    "payments.record.updated",
    "payments.record.deleted",
    "refunds.record.created",
]

ACTOR = "system_invoice_status"
_FLIPPABLE = {"sent", "partial", "overdue", "paid"}


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def EVENT(request):
    collection = str(request.get("collection") or "")
    record_id = str(request.get("record_id") or "")
    if not record_id:
        return {"ok": True, "skipped": "no record id"}
    base = _base_dir()

    invoice_id = ""
    try:
        if collection in ("payments", "refunds"):
            try:
                row = object_records.get_collection_record(collection, record_id, base_dir=base)
                invoice_id = row.get("invoice_id", "")
            except Exception:
                # deleted payment: the change log's before-image isn't in the
                # payload; recompute every invoice is overkill -- skip, the
                # daily aging pass trues it up.
                return {"ok": True, "skipped": "source row gone"}
        if not invoice_id:
            return {"ok": True, "skipped": "no invoice"}
        invoice = object_records.get_collection_record("invoices", invoice_id, base_dir=base)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}

    status = invoice.get("status") or ""
    if status not in _FLIPPABLE:
        return {"ok": True, "skipped": f"status {status!r} not auto-flipped"}
    try:
        total = int(invoice.get("total_cents") or 0)
        paid = int(invoice.get("amount_paid_cents") or 0)
    except ValueError:
        return {"ok": True, "skipped": "non-numeric amounts"}
    if total <= 0:
        return {"ok": True, "skipped": "zero-total invoice"}

    if paid >= total:
        new_status = "paid"
    elif paid > 0:
        # partially paid; keep overdue sticky (time cleared only by payment
        # in full or the aging pass re-evaluating)
        new_status = "overdue" if status == "overdue" else "partial"
    else:
        new_status = "overdue" if status == "overdue" else "sent"

    if new_status == status:
        return {"ok": True, "skipped": "status already correct"}
    try:
        object_records.update_collection_record(
            "invoices", invoice_id, {"status": new_status}, base_dir=base, actor=ACTOR
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}
    return {"ok": True, "invoice": invoice_id, "status": new_status}
