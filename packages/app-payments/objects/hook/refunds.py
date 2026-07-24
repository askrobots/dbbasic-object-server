"""Pre-write hook for refunds: the invariant gate and the stamp.

Two doctrines in one hook (docs/logic-decisions.md):
- #3 money moves: a refund is a compensating record; this gate keeps the
  movement honest — amount <= payment.amount minus prior refunds, and no
  refunding a bounced payment. Sums refund records directly (authoritative,
  never derived values).
- #1 stamp point-in-time facts: invoice_id is copied from the payment at
  write time (a transform), never trusted from the client — so invoices can
  roll refunds up directly and the pointer can never disagree with the
  payment it compensates.
"""

import os

import object_records


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def BEFORE_WRITE(request):
    if request.get("action") != "create":
        return None
    record = dict(request.get("record") or {})

    try:
        amount = int(record.get("amount_cents") or 0)
    except ValueError:
        return None
    if amount <= 0:
        return {"error": "Refund amount must be a positive number of cents.", "status": 400}

    payment_id = record.get("payment_id")
    if not payment_id:
        return None  # required + relation validation own this
    base = _base_dir()
    try:
        payment = object_records.get_collection_record("payments", payment_id, base_dir=base)
    except Exception:
        return None
    if (payment.get("status") or "received") == "bounced":
        return {"error": "Cannot refund a bounced payment.", "status": 409}

    prior = 0
    for r in object_records.read_collection_records("refunds", base_dir=base):
        if r.get("payment_id") == payment_id:
            try:
                prior += int(r.get("amount_cents") or 0)
            except ValueError:
                continue
    try:
        paid = int(payment.get("amount_cents") or 0)
    except ValueError:
        paid = 0
    refundable = paid - prior
    if amount > refundable:
        return {
            "error": (
                f"Refund of {amount} cents exceeds the refundable "
                f"{max(refundable, 0)} cents on payment {payment_id} "
                f"({paid} paid, {prior} already refunded)."
            ),
            "status": 409,
        }

    # Stamp: invoice_id always comes from the payment, never the client.
    record["invoice_id"] = payment.get("invoice_id", "")
    return {"record": record}
