"""Pre-write hook for payments: the overpayment gate.

Cross-record rule the schema can't express (docs/business-logic-patterns.md):
a new payment may not take an invoice past its total — received payments minus
refunds plus this payment must stay <= invoice.total_cents. Configurable via
app_settings key `payments.overpayment_policy` ("reject", the default, or
"allow" for shops that keep customer credit balances). Also covers the
numeric-range gap: amount_cents must be positive (logic-decisions: count this
repeat toward a future declarative min/max rule).

Gates sum the records directly — never derived rollup values (a gate must be
authoritative even when a derived caption is stale or empty).
"""

import os

import object_records


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _setting(key, default):
    try:
        for row in object_records.read_collection_records("app_settings", base_dir=_base_dir()):
            if row.get("key") == key and row.get("value"):
                return row["value"]
    except Exception:
        pass
    return default


def BEFORE_WRITE(request):
    if request.get("action") != "create":
        return None
    record = request.get("record") or {}

    try:
        amount = int(record.get("amount_cents") or 0)
    except ValueError:
        return None  # schema validation reports the type error properly
    if amount <= 0:
        return {"error": "Payment amount must be a positive number of cents.", "status": 400}

    if (record.get("status") or "received") != "received":
        return None
    invoice_id = record.get("invoice_id")
    if not invoice_id:
        return None  # required + relation validation own this

    if _setting("payments.overpayment_policy", "reject") == "allow":
        return None

    base = _base_dir()
    try:
        invoice = object_records.get_collection_record("invoices", invoice_id, base_dir=base)
    except Exception:
        return None  # relation validation owns a missing invoice
    try:
        total = int(invoice.get("total_cents") or 0)
    except ValueError:
        return None

    received = 0
    for p in object_records.read_collection_records("payments", base_dir=base):
        if p.get("invoice_id") == invoice_id and (p.get("status") or "received") == "received":
            try:
                received += int(p.get("amount_cents") or 0)
            except ValueError:
                continue
    refunded = 0
    try:
        for r in object_records.read_collection_records("refunds", base_dir=base):
            if r.get("invoice_id") == invoice_id:
                try:
                    refunded += int(r.get("amount_cents") or 0)
                except ValueError:
                    continue
    except Exception:
        pass

    remaining = total - (received - refunded)
    if amount > remaining:
        return {
            "error": (
                f"Payment of {amount} cents exceeds the remaining balance of "
                f"{max(remaining, 0)} cents on invoice {invoice_id}. "
                "(Set app_settings payments.overpayment_policy=allow to permit "
                "customer credit balances.)"
            ),
            "status": 409,
        }
    return None
