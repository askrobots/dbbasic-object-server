"""action_apply_count -- reconcile a physical count against derived stock.

The inventory adjusting entry proper (plan/inventory-adjustments-spec.md
section 4): POST {product_id, location_id, counted_qty, optional counted_on}.
Reads derived on-hand (object_stock -- levels are NEVER stored), computes
the variance, and writes ONE compensating adjustment move: shortage moves
stock OUT of the location, overage moves it IN, zero variance is a no-op.
reason=adjustment + reference "count variance" keeps count corrections
distinguishable from the named loss reasons (waste/theft/... say what
happened; a count variance says only that the book number was wrong).

The variance journal is composed here as well (DR shrinkage / CR inventory
for a shortage, the mirror for an overage): this action writes at the
storage level, which never reaches the event dispatcher, so it cannot rely
on system_stock_books firing. The shared generated_from marker
("stock_moves/{id}") keeps the two paths idempotent against each other.

Deliberately per-product v1: count-sheet workflows (a counts collection +
a runner) arrive when someone actually counts more than a shelf at a time
(docs/logic-decisions.md #4).
"""

import os
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import object_finance
import object_ids
import object_records
import object_stock

ACTOR = "action_apply_count"


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


def _compose_variance_journal(base, move, direction):
    inventory_acct = _setting(base, "inventory.journal.inventory_account")
    shrinkage_acct = _setting(base, "inventory.journal.shrinkage_account")
    if not inventory_acct or not shrinkage_acct:
        return {"ok": True, "skipped": "accounts unconfigured"}
    try:
        quantity = Decimal(str(move.get("quantity") or "0"))
        unit_cost = int(move.get("unit_cost_cents") or 0)
    except (InvalidOperation, ValueError):
        return {"ok": True, "skipped": "no stamped cost; nothing to book"}
    amount = int((quantity * unit_cost).to_integral_value(rounding=ROUND_HALF_UP))
    if amount <= 0:
        return {"ok": True, "skipped": "no stamped cost; nothing to book"}
    if direction == "shortage":
        debit_acct, credit_acct, what = shrinkage_acct, inventory_acct, "Count shortage"
    else:
        debit_acct, credit_acct, what = inventory_acct, shrinkage_acct, "Count overage"
    return object_finance.compose_posted_journal(
        base,
        generated_from=f"stock_moves/{move['id']}",
        date=str(move.get("occurred_at") or "")[:10],
        description=f"{what}: product {move.get('product_id')} x {move.get('quantity')} (count variance)",
        lines=[
            {"account_id": debit_acct, "debit_cents": amount, "credit_cents": 0},
            {"account_id": credit_acct, "debit_cents": 0, "credit_cents": amount},
        ],
        owner_id=move.get("owner_id", ""),
        entity_id=move.get("entity_id", ""),
        actor=ACTOR,
    )


def POST(request):
    identity = request.get("_identity") or {}
    user_id = identity.get("user_id") or ""
    if not user_id:
        return {"status": 403, "error": "Sign in to apply a count."}

    product_id = str(request.get("product_id") or "").strip()
    location_id = str(request.get("location_id") or "").strip()
    if not product_id or not location_id:
        return {"status": 400, "error": "product_id and location_id are required."}
    try:
        counted = Decimal(str(request.get("counted_qty")))
    except (InvalidOperation, TypeError):
        return {"status": 400, "error": "counted_qty must be a number."}
    if counted < 0:
        return {"status": 400, "error": "counted_qty must not be negative."}

    base = _base_dir()
    try:
        product = object_records.get_collection_record("products", product_id, base_dir=base)
    except Exception:
        return {"status": 404, "error": f"Product not found: {product_id}"}

    on_hand = object_stock.quantity_at_location(product_id, location_id, base_dir=base)
    variance = counted - on_hand
    if variance == 0:
        return {"status": 200, "variance": "0",
                "note": "count matches the book quantity; no move written"}

    counted_on = str(request.get("counted_on") or "").strip() or date.today().isoformat()
    move = {
        "id": object_ids.new_uuid4(),
        "product_id": product_id,
        "from_location_id": location_id if variance < 0 else "",
        "to_location_id": location_id if variance > 0 else "",
        "quantity": str(abs(variance)),
        "unit_cost_cents": str(product.get("cost_cents") or "").strip(),
        "reason": "adjustment",
        "reference": "count variance",
        "occurred_at": counted_on,
        "owner_id": user_id,
    }
    if product.get("entity_id"):
        move["entity_id"] = product["entity_id"]
    object_records.create_collection_record("stock_moves", move, base_dir=base, actor=ACTOR)

    direction = "shortage" if variance < 0 else "overage"
    journal = _compose_variance_journal(base, move, direction)
    return {"status": 200, "move_id": move["id"], "variance": str(variance),
            "direction": direction, "on_hand_before": str(on_hand),
            "journal": journal}
