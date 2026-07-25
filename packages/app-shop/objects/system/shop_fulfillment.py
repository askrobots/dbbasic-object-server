"""system_shop_fulfillment -- money arrived, so the sale is real.

HANDLES payment writes. When a payment lands against an invoice raised
for a web order, this confirms the order and moves the stock out.

The ordering is the whole point. Checkout decrements nothing: an order
that is never paid for must not have consumed stock, or every abandoned
card leaves a phantom sale behind and the shop shows "sold out" for goods
that are sitting on the shelf. Stock moves when money moves.

Placement follows docs/logic-decisions.md #6 -- this is a REACTION
(post-commit, best-effort, never blocks the payment), so it lives in an
event handler. A shop that cannot record a stock move must still be able
to take the money; the discrepancy is visible and fixable, whereas a
refused payment is a lost sale nobody can recover.

Idempotency by provenance (#7): each stock move is stamped
"orders/{id}:fulfil" in its reference, and a move already carrying that
stamp means a replayed payment event moves nothing. The books entry is
already handled by app-payments' system_books, unchanged -- this object
deliberately does not post anything itself.
"""

import os
from datetime import date

import object_ids
import object_records

HANDLES = [
    "payments.record.created",
    "payments.record.updated",
]

ACTOR = "system_shop_fulfillment"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


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


def POST(request):
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
    if _text(order.get("status")) not in ("draft", "confirmed"):
        return {"ok": True, "skipped": f"order already {_text(order.get('status'))}"}

    marker = f"orders/{order['id']}:fulfil"
    try:
        moves = object_records.read_collection_records("stock_moves", base_dir=base)
    except Exception:
        moves = None
    if moves is None:
        # No stock app installed: confirm the order anyway. A shop selling
        # services or downloads has nothing to move.
        object_records.update_collection_record(
            "orders", order["id"], {"status": "confirmed"},
            base_dir=base, actor=ACTOR)
        return {"ok": True, "order_id": order["id"], "confirmed": True,
                "moved": 0, "note": "stock not installed; nothing to move"}

    if any(marker in _text(move.get("reference")) for move in moves):
        return {"ok": True, "skipped": f"already fulfilled: {marker}",
                "order_id": order["id"]}

    from_location = _setting(base, "shop.stock_location")
    to_location = _setting(base, "shop.customer_location")
    try:
        lines = object_records.read_collection_records("order_lines", base_dir=base)
    except Exception:
        lines = []
    mine = [line for line in lines if _text(line.get("order_id")) == order["id"]]

    moved = 0
    skipped = []
    today = _text(request.get("today")) or date.today().isoformat()
    for line in mine:
        product_id = _text(line.get("product_id"))
        if not product_id:
            continue
        if not from_location:
            skipped.append("shop.stock_location is not configured")
            break
        object_records.create_collection_record(
            "stock_moves",
            {
                "id": object_ids.new_uuid4(),
                "product_id": product_id,
                "from_location_id": from_location,
                "to_location_id": to_location,
                "quantity": _text(line.get("quantity")) or "1",
                "reason": "sale",
                "reference": f"{marker} {_text(order.get('number'))}",
                "occurred_at": today,
                "owner_id": _text(order.get("owner_id")),
            },
            base_dir=base, actor=ACTOR)
        moved += 1

    object_records.update_collection_record(
        "orders", order["id"], {"status": "confirmed"},
        base_dir=base, actor=ACTOR)

    result = {"ok": True, "order_id": order["id"], "confirmed": True, "moved": moved}
    if skipped:
        # The sale stands and the gap is visible, which is the right way
        # round: a missing setting must not cost somebody a paid order.
        result["warning"] = skipped[0]
    return result
