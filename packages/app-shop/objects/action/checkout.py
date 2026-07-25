"""action_checkout -- a basket becomes an order, and nothing else yet.

POST {session_token, customer_email, customer_name?, confirm_prices?,
      preview?}

This is where browsing turns into a commitment, so it is where every
check happens at once:

- the products are still for sale,
- the prices still match what the shopper agreed to,
- the stock is there.

All of them are reported TOGETHER. Telling somebody about one problem,
letting them fix it, then revealing the next is how a checkout gets
abandoned -- and it is the easiest thing in the world to get wrong by
returning on the first failure.

Price disagreement is a hard stop with both numbers shown, not a silent
resolution in either direction. Accepting the new price on the shopper's
behalf is a bait-and-switch; honouring a three-week-old basket forever is
an open-ended liability. `confirm_prices: true` is the shopper saying yes
to the current prices, having seen them.

**Stock is checked here and moved nowhere.** The order is created as a
draft; stock moves and the order confirms when money actually arrives
(system_shop_fulfillment). Decrementing on checkout would make every
abandoned payment a phantom sale.

The oversell race is real and is not pretended away: two shoppers can
both pass this gate for the last unit. That is accepted deliberately at
this scale -- it surfaces at the stock move, which is visible and
refundable -- rather than paid for with a reservation ledger nobody has
needed yet. The same honest posture as hook_wallet_entries' documented
check-then-append race.
"""

import os
from datetime import date

import object_cart
import object_ids
import object_records
import object_stock

ACTOR = "action_checkout"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _truthy(value):
    return _text(value).lower() in ("true", "1", "yes", "on")


def _products(base, product_ids):
    out = {}
    for product_id in product_ids:
        try:
            row = object_records.get_collection_record(
                "products", product_id, base_dir=base)
        except Exception:
            row = None
        if row:
            out[product_id] = row
    return out


def _on_hand(base, products):
    """Levels for the products this basket touches, and which of them the
    shop actually tracks.

    A service or a digital download has no stock level, and treating "no
    level recorded" as "none available" would refuse to sell the things
    that are always available.
    """
    # products.product_type: physical | digital | service | subscription |
    # asset. Only the first and last are things that sit on a shelf and
    # can run out; a download or an hour of work never does.
    tracked = {pid for pid, row in products.items()
               if _text(row.get("product_type")) in ("physical", "asset", "")}
    levels = {}
    for product_id in tracked:
        try:
            levels[product_id] = object_stock.total_quantity(
                product_id, base_dir=base)
        except Exception:
            continue
    return levels, tracked


def POST(request):
    base = _base_dir()
    token = _text(request.get("session_token"))
    if not token:
        return {"status": 400, "error": "session_token is required"}

    try:
        carts = object_records.read_collection_records("carts", base_dir=base)
    except Exception:
        return {"ok": True, "skipped": "shop not installed (carts absent)"}

    cart = next((c for c in carts if _text(c.get("session_token")) == token
                 and _text(c.get("status")) == "open"), None)
    if cart is None:
        settled = next((c for c in carts
                        if _text(c.get("session_token")) == token
                        and _text(c.get("checked_out_order_id"))), None)
        if settled:
            # One cart, one order, however many times a browser retries.
            return {"ok": True, "duplicate": True,
                    "order_id": _text(settled["checked_out_order_id"]),
                    "note": "this basket has already been checked out"}
        return {"status": 404, "error": "No open basket for this session."}

    try:
        all_items = object_records.read_collection_records("cart_items", base_dir=base)
    except Exception:
        all_items = []
    items = [i for i in all_items if _text(i.get("cart_id")) == cart["id"]]

    products = _products(base, {_text(i.get("product_id")) for i in items})
    levels, tracked = _on_hand(base, products)
    blockers = object_cart.checkout_blockers(items, products, levels, tracked=tracked)

    if blockers["empty"]:
        return {"status": 400, "error": "There is nothing in this basket."}

    if blockers["price_changes"] and not _truthy(request.get("confirm_prices")):
        return {"status": 409,
                "error": "Some prices changed while this basket was open.",
                "price_changes": blockers["price_changes"],
                "note": "Show both numbers and send confirm_prices=true once "
                        "the shopper has agreed to the current ones."}

    if blockers["inactive"] or blockers["unavailable"]:
        return {"status": 409,
                "error": "Some items cannot be ordered right now.",
                "unavailable": blockers["unavailable"],
                "inactive": blockers["inactive"]}

    # Prices confirmed: adopt the live ones so the order records what the
    # shopper actually agreed to, not the number they first saw.
    if blockers["price_changes"]:
        for item in items:
            product = products.get(_text(item.get("product_id")))
            if product:
                item["unit_price_cents"] = str(product.get("price_cents") or 0)

    summary = object_cart.totals(items)
    email = _text(request.get("customer_email"))

    if _truthy(request.get("preview")):
        return {"ok": True, "preview": True, "cart_id": cart["id"],
                "subtotal_cents": summary["subtotal_cents"],
                "lines": summary["lines"],
                "price_changes": blockers["price_changes"]}

    if not email:
        return {"status": 400,
                "error": "An email address is needed to send the receipt to."}

    order_id = object_ids.new_uuid4()
    today = _text(request.get("today")) or date.today().isoformat()
    object_records.create_collection_record(
        "orders",
        {
            "id": order_id,
            "doc_type": "sale",
            "number": f"WEB-{order_id[:8].upper()}",
            "customer_name": _text(request.get("customer_name")) or email,
            "customer_email": email,
            "currency": _text(cart.get("currency")) or "USD",
            "status": "draft",
            "order_date": today,
            "subtotal_cents": str(summary["subtotal_cents"]),
            "total_cents": str(summary["subtotal_cents"]),
            "notes": f"Web checkout [carts/{cart['id']}]",
            "owner_id": _text(cart.get("owner_id")),
        },
        base_dir=base, actor=ACTOR)

    for line in summary["lines"]:
        object_records.create_collection_record(
            "order_lines",
            {
                "id": object_ids.new_uuid4(),
                "order_id": order_id,
                "product_id": line["product_id"],
                "description": line["description"],
                "quantity": line["quantity"],
                "unit_price_cents": str(line["unit_price_cents"]),
                "line_total_cents": str(line["line_total_cents"]),
                "owner_id": _text(cart.get("owner_id")),
            },
            base_dir=base, actor=ACTOR)

    # Stamp the cart BEFORE anything else can retry: the stamp is what
    # makes a second click a no-op rather than a second order.
    object_records.update_collection_record(
        "carts", cart["id"],
        {"status": "checked_out", "checked_out_order_id": order_id,
         "customer_email": email,
         "customer_name": _text(request.get("customer_name"))},
        base_dir=base, actor=ACTOR)

    return {"ok": True, "order_id": order_id, "cart_id": cart["id"],
            "total_cents": summary["subtotal_cents"], "lines": len(summary["lines"]),
            "status_of_order": "draft",
            "note": "order raised; stock moves and the order confirms when "
                    "payment arrives"}
