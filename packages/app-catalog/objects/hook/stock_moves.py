"""Pre-write hook for stock_moves: loss-shape gates + cost stamping.

Two jobs (plan/inventory-adjustments-spec.md):

- GATES the schema can't express: quantity must be positive (direction
  comes from from/to locations, corrections are new compensating moves --
  never negative quantities; the numeric-range repeat counter ticks again,
  see docs/logic-decisions.md #4), and a LOSS move (waste/breakage/theft/
  expiry/damage/disaster) must leave a real location and have no
  destination -- you cannot waste goods INTO a shelf.

- STAMPS unit_cost_cents when the client omitted it (docs/logic-decisions.md
  #1: the cost at the moment of movement; editing the product's cost
  tomorrow must not reprice yesterday's loss). A client-supplied cost is
  kept -- purchases carry their invoice cost. A SALE is stamped with the
  moving weighted average of stock actually on hand; everything else, and
  a sale with no priced acquisitions behind it, falls back to
  products.cost_cents.

Create-only: stock_moves is an append collection, so updates never carry
business meaning here.
"""

import os
from decimal import Decimal, InvalidOperation

import object_records
import object_stock

LOSS_REASONS = {"waste", "breakage", "theft", "expiry", "damage", "disaster"}


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def BEFORE_WRITE(request):
    if request.get("action") != "create":
        return None
    record = request.get("record") or {}

    try:
        quantity = Decimal(str(record.get("quantity") or "0"))
    except InvalidOperation:
        return None  # schema validation reports the type error properly
    if quantity <= 0:
        return {"error": "Stock move quantity must be positive -- direction "
                         "comes from the from/to locations, and corrections "
                         "are new compensating moves, never negative rows.",
                "status": 400}

    reason = (record.get("reason") or "").strip()
    if reason in LOSS_REASONS:
        if not (record.get("from_location_id") or "").strip():
            return {"error": f"A {reason} move must say which location the "
                             "goods left (from_location_id is required for "
                             "loss reasons).",
                    "status": 400}
        if (record.get("to_location_id") or "").strip():
            return {"error": f"A {reason} move cannot have a destination -- "
                             "lost goods leave the system. Clear "
                             "to_location_id, or use reason=transfer.",
                    "status": 400}

    if not str(record.get("unit_cost_cents") or "").strip():
        product_id = record.get("product_id") or ""

        # A SALE is valued, not priced. The cost that leaves with the goods
        # is the moving weighted average of what is actually on the shelf
        # (object_stock.weighted_average_cost_cents) -- computed here,
        # BEFORE this move joins the log, so it is the average at the
        # moment of sale and excludes the sale itself.
        #
        # products.cost_cents is a standard cost: a planning figure someone
        # typed, not what was paid. It stays the fallback for a product
        # with no priced acquisitions yet, and it stays the primary for
        # losses -- writing off a broken mug at standard cost is fine,
        # while reporting cost of SALES at a typed-in number is how gross
        # margin quietly becomes fiction.
        if reason == "sale":
            try:
                average = object_stock.average_cost_for_product(
                    product_id, base_dir=_base_dir())
            except Exception:
                average = None
            if average:
                stamped = dict(record)
                stamped["unit_cost_cents"] = str(average)
                return {"record": stamped}

        try:
            product = object_records.get_collection_record(
                "products", product_id, base_dir=_base_dir())
        except Exception:
            return None  # relation validation owns a missing product
        cost = str(product.get("cost_cents") or "").strip()
        if cost:
            stamped = dict(record)
            stamped["unit_cost_cents"] = cost
            return {"record": stamped}
    return None
