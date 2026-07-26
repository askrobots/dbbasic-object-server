"""system_shop_fulfillment -- money arrived, so the sale is real and the
box goes out.

HANDLES payment writes. When a payment lands against an invoice raised for
a web order, this confirms the order and -- unless the shop has taken over
its own packing bench -- ships it.

The ordering is still the whole point. Checkout decrements nothing: an
order that is never paid for must not have consumed stock, or every
abandoned card leaves a phantom sale behind and the shop shows "sold out"
for goods sitting on the shelf. Stock moves when money moves.

**What changed, and why this object got smaller.** It used to write the
stock moves itself, one per order line, at payment. That was right while
an order could only be fulfilled all at once; it became wrong the moment
shipments existed, because "the goods left" is a fact about a BOX, not
about a payment, and two objects claiming the same fact is how a system
learns to disagree with itself. So the old behavior now travels through
the shipment noun: this handler creates a shipment for everything
unshipped and marks it packed, then shipped, and app-shipping's
system_order_fulfillment does what it always does when a shipment leaves
-- one sale move per line, idempotent per line, order status derived.
Nothing about the zero-touch shop's experience changes; there is simply a
document now saying what went out, and a packing slip to put in the box.

**shop.auto_fulfill (default TRUE)** is the seam. A shop with no warehouse
wants payment to mean shipped and never wants to see a pick list; a shop
that picks and packs sets it false, and this handler then confirms the
order and stops, leaving the shipment to action_create_shipment and a
human with a trolley. Defaulting true is what keeps the proven chain
working for every existing installation without anybody configuring
anything.

Placement follows docs/logic-decisions.md #6 -- a REACTION (post-commit,
best-effort, never blocks the payment). A shop that cannot record a
shipment must still be able to take the money; the discrepancy is visible
and fixable, whereas a refused payment is a lost sale nobody can recover.
Every failure below therefore lands in the RESULT, not in an exception:
no shipping app installed, no stock app installed, no configured
locations -- the order still confirms and the response says what did not
happen.

Idempotency by provenance (#7): the shipment carries "orders/{id}" in its
notes and, more usefully, the remaining-quantity arithmetic in
action_create_shipment means a replayed payment finds nothing left to ship
and returns ok with a note rather than raising a second empty parcel. The
stock moves are stamped per shipment line by system_order_fulfillment, so
a replay moves nothing twice there either. The books entry is handled by
app-payments' system_books, unchanged -- this object deliberately does not
post anything itself.
"""

import os

import object_execution
import object_records
import python_object_runtime

HANDLES = [
    "payments.record.created",
    "payments.record.updated",
]

ACTOR = "system_shop_fulfillment"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _truthy(value, default):
    text = _text(value).lower()
    if not text:
        return default
    return text in ("true", "1", "yes", "on")


def _setting(base, key, default=""):
    try:
        for row in object_records.read_collection_records("app_settings", base_dir=base):
            if row.get("key") == key and _text(row.get("value")):
                return _text(row["value"])
    except Exception:
        pass
    return default


def _payment_for(request, base):
    record = request.get("record")
    if isinstance(record, dict) and record.get("id"):
        return record
    payment_id = _text(request.get("record_id") or request.get("id"))
    if not payment_id:
        return None
    try:
        return object_records.get_collection_record("payments", payment_id,
                                                    base_dir=base)
    except Exception:
        return None


def _invoice_is_settled(base, invoice_id):
    """(True, "") when the bill is fully paid, else (False, why).

    Trusts the invoice's own status where it is decisive, and falls back
    to comparing what has been paid against the total -- because a shop
    with app-payments' status handler absent would otherwise never ship
    anything, and "your money arrived and nothing happened" is a worse
    failure than shipping a shade early.
    """
    if not invoice_id:
        return True, ""          # nothing to settle; not a web order's bill
    try:
        invoice = object_records.get_collection_record(
            "invoices", invoice_id, base_dir=base)
    except Exception:
        # Cannot read the bill: do not hold the goods hostage to a lookup
        # failure. The stock move is idempotent and visible either way.
        return True, ""
    if _text(invoice.get("status")) in ("paid", "void"):
        return True, ""
    total = _int(invoice.get("total_cents"))
    if not total:
        return True, ""

    # Sum the payments HERE rather than trusting invoice.amount_paid_cents.
    # That field is derived by a rollup, and this handler runs ON the write
    # that just created the payment -- so at this instant the invoice has
    # not been told about the money that triggered us. Reading it would
    # mean the first payment never ships anything and the SECOND one ships
    # everything, which is a bug that only appears for customers who pay
    # in instalments.
    paid = 0
    try:
        for row in object_records.read_collection_records("payments", base_dir=base):
            if (_text(row.get("invoice_id")) == invoice_id
                    and _text(row.get("status")) == "received"):
                paid += _int(row.get("amount_cents"))
    except Exception:
        return True, ""          # cannot tell; do not hold the goods
    try:
        for row in object_records.read_collection_records("refunds", base_dir=base):
            if _text(row.get("invoice_id")) == invoice_id:
                paid -= _int(row.get("amount_cents"))
    except Exception:
        pass                     # no refunds app installed is not a refund

    if paid >= total:
        return True, ""
    return False, f"invoice not settled: {paid} of {total} paid"


def _int(value):
    try:
        return int(_text(value) or 0)
    except (TypeError, ValueError):
        return 0


def _order_for_invoice(base, invoice_id):
    if not invoice_id:
        return None
    try:
        orders = object_records.read_collection_records("orders", base_dir=base)
    except Exception:
        return None
    for order in orders:
        if _text(order.get("invoice_id")) == invoice_id:
            return order
    return None


def _call(object_id, payload, *, method="POST"):
    """Run another installed object in process, the same way site_shop calls
    its siblings: this package owns the payment-to-order join and nothing
    about boxes, so it asks the object that does.

    Returns (result, error). A missing object -- app-shipping simply not
    installed -- is an error string, never an exception: see the module
    docstring's posture about what a failure here is allowed to cost.
    """
    try:
        runtime = python_object_runtime.PythonObjectRuntime()
        outcome = object_execution.execute_object(
            runtime,
            object_execution.ObjectExecutionRequest(
                object_id, method=method, payload=payload))
    except Exception as exc:
        return None, str(exc)[:200]
    if not outcome.ok:
        message = getattr(outcome.error, "message", "") if outcome.error else ""
        return None, _text(message)[:200] or f"{object_id} failed"
    return outcome.result, ""


def _ship_everything(base, order):
    """Create a shipment for everything still unshipped and send it.

    packed then shipped as two separate updates, deliberately: the schema's
    transition ladder is open -> packed -> shipped, and a handler that
    jumped straight to shipped would either be refused or would force the
    ladder to be loosened for everybody, which would let a real packing
    bench skip the step that means "somebody actually put this in a box".
    """
    created, error = _call("action_create_shipment", {"order_id": order["id"]})
    if error:
        return {"shipped": False, "warning":
                f"no shipment was created ({error}); the order still stands"}
    if not isinstance(created, dict) or not created.get("ok"):
        detail = _text((created or {}).get("error")) or "unknown reason"
        return {"shipped": False,
                "warning": f"no shipment was created ({detail}); the order still stands"}

    shipment_id = _text(created.get("shipment_id"))
    if not shipment_id:
        # Nothing left to ship: a replayed payment, or an order somebody
        # already packed by hand. Idempotent, and not a failure.
        return {"shipped": False, "note": _text(created.get("note"))}

    for status in ("packed", "shipped"):
        try:
            object_records.update_collection_record(
                "shipments", shipment_id, {"status": status},
                base_dir=base, actor=ACTOR)
        except Exception as exc:
            return {"shipped": False, "shipment_id": shipment_id,
                    "warning": (f"shipment {shipment_id} could not move to "
                                f"{status}: {str(exc)[:120]}")}

    # Fire the fulfillment handler in process rather than waiting for the
    # change-log dispatcher: this write came from a handler, not from an
    # HTTP request, so nothing dispatched it, and the stock move has to
    # happen now for exactly the reason it always did -- the goods have
    # gone. The handler is idempotent per line, so the dispatcher's later
    # at-least-once redelivery moves nothing twice.
    result, error = _call("system_order_fulfillment",
                          {"collection": "shipments", "record_id": shipment_id},
                          method="EVENT")
    out = {"shipped": True, "shipment_id": shipment_id,
           "lines": created.get("lines", 0),
           "slip_path": created.get("slip_path", "")}
    if error:
        out["warning"] = (f"the shipment went out but its stock move did not "
                          f"({error}); the goods left the shelf without the "
                          f"ledger saying so")
        return out
    if isinstance(result, dict):
        out["moved"] = result.get("moved", 0)
        if result.get("warning"):
            out["warning"] = result["warning"]
        if result.get("order_status"):
            out["order_status"] = result["order_status"]
    return out


def EVENT(request):
    base = _base_dir()
    payment = _payment_for(request, base)
    if not payment:
        return {"ok": True, "skipped": "no payment in the event"}
    if _text(payment.get("status")) != "received":
        return {"ok": True, "skipped": "payment not received"}

    order = _order_for_invoice(base, _text(payment.get("invoice_id")))
    if order is None:
        # Most payments are not for web orders. Saying so plainly beats a
        # silent return that looks identical to a bug.
        return {"ok": True, "skipped": "no web order for this payment"}

    # Goods leave when the BILL is settled, not when money merely arrives.
    #
    # This checked only that a payment existed and was received, so any
    # amount shipped the order: a 14.00 payment against a 20.00 invoice
    # fulfilled it in full, and the invoice was left sitting at `partial`
    # saying so. Found by paying the wrong amount by hand on the live shop.
    #
    # The invoice already knows the answer -- system_invoice_status folds
    # payments and refunds and flips it to paid or partial -- so this asks
    # the document rather than doing its own arithmetic on one payment,
    # which is also what makes a deposit followed by a balance work: the
    # second payment is what tips it to paid, and that is the event that
    # ships.
    settled, reason = _invoice_is_settled(base, _text(payment.get("invoice_id")))
    if not settled:
        return {"ok": True, "skipped": reason, "order_id": order["id"]}
    if _text(order.get("status")) not in ("draft", "confirmed"):
        return {"ok": True, "skipped": f"order already {_text(order.get('status'))}"}

    if _text(order.get("status")) == "draft":
        object_records.update_collection_record(
            "orders", order["id"], {"status": "confirmed"},
            base_dir=base, actor=ACTOR)

    result = {"ok": True, "order_id": order["id"], "confirmed": True}

    if not _truthy(_setting(base, "shop.auto_fulfill"), True):
        # A shop with a packing bench: the order is confirmed and appears on
        # the pick list, and a human decides what goes in which box.
        result["note"] = ("shop.auto_fulfill is off; this order is confirmed "
                          "and waiting on the pick list")
        result["shipped"] = False
        return result

    result.update(_ship_everything(base, order))
    return result


# EVENT is the verb the change dispatcher calls handlers with (see
# object_change_dispatch); it went unnoticed here because in-process tests
# invoked POST directly and the dispatcher's method-not-supported error
# only surfaced on the live box. POST stays as an alias so an operator can
# poke the handler by hand over HTTP.
POST = EVENT
