"""action_cart -- the basket verbs: get, add, set, remove, clear.

POST {session_token, action, product_id?, quantity?}

One object rather than five, because these are the same operation with a
different argument and splitting them would mean five copies of "find or
make this session's basket". The verb is data, which is the same reason
the scheduler's tasks are data.

No stock is touched here and no price is refreshed here. Adding to a
basket is not a commitment: reserving stock on add is how a shop shows
"sold out" for goods nobody bought, and quietly repricing a basket is how
a shopper is charged something they never agreed to. Both are decided at
checkout, where a person is looking.
"""

import os

import object_cart
import object_ids
import object_records

ACTOR = "action_cart"

ACTIONS = ("get", "add", "set", "remove", "clear")


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _shop_owner(base):
    """Whose shop this is -- the owner stamped on anonymous baskets.

    A cart always belongs to somebody so that permissions have something
    to filter on; before anyone signs in, that somebody is the shop.
    """
    try:
        for row in object_records.read_collection_records("app_settings", base_dir=base):
            if row.get("key") == "shop.owner_id" and _text(row.get("value")):
                return _text(row["value"])
    except Exception:
        pass
    return "shop"


def _find_cart(base, token):
    try:
        rows = object_records.read_collection_records("carts", base_dir=base)
    except Exception:
        return None, "shop not installed (carts absent)"
    for row in rows:
        if _text(row.get("session_token")) == token and _text(row.get("status")) == "open":
            return row, ""
    return None, ""


def _items_of(base, cart_id):
    try:
        rows = object_records.read_collection_records("cart_items", base_dir=base)
    except Exception:
        return []
    return [row for row in rows if _text(row.get("cart_id")) == cart_id]


def _state(base, cart, items):
    summary = object_cart.totals(items)
    return {"ok": True, "cart_id": cart["id"],
            "session_token": _text(cart.get("session_token")),
            "status": _text(cart.get("status")), **summary}


def POST(request):
    base = _base_dir()
    token = _text(request.get("session_token"))
    action = _text(request.get("action")) or "get"
    if not token:
        return {"status": 400, "error": "session_token is required"}
    if action not in ACTIONS:
        return {"status": 400,
                "error": f"action must be one of {', '.join(ACTIONS)}"}

    cart, problem = _find_cart(base, token)
    if problem:
        return {"ok": True, "skipped": problem}

    if cart is None:
        if action in ("get", "remove", "clear"):
            # Nothing to look at and nothing to remove: an empty basket is
            # a perfectly good answer, not a 404.
            return {"ok": True, "cart_id": "", "session_token": token,
                    "status": "open", "lines": [], "subtotal_cents": 0, "count": "0"}
        cart = object_records.create_collection_record(
            "carts",
            {"id": object_ids.new_uuid4(), "session_token": token, "status": "open",
             "currency": "USD", "owner_id": _shop_owner(base)},
            base_dir=base, actor=ACTOR)

    items = _items_of(base, cart["id"])

    if action == "get":
        return _state(base, cart, items)

    if action == "clear":
        for item in items:
            object_records.delete_collection_record(
                "cart_items", item["id"], base_dir=base, actor=ACTOR)
        return _state(base, cart, [])

    product_id = _text(request.get("product_id"))
    if not product_id:
        return {"status": 400, "error": "product_id is required for this action"}

    existing = next((i for i in items if _text(i.get("product_id")) == product_id), None)

    if action == "remove":
        if existing:
            object_records.delete_collection_record(
                "cart_items", existing["id"], base_dir=base, actor=ACTOR)
            items = [i for i in items if i["id"] != existing["id"]]
        return _state(base, cart, items)

    try:
        product = object_records.get_collection_record(
            "products", product_id, base_dir=base)
    except Exception:
        product = None
    if not product:
        return {"status": 404, "error": f"No such product: {product_id}"}
    if _text(product.get("is_active")).lower() in ("false", "0", "no", "off"):
        return {"status": 409,
                "error": f"{_text(product.get('name')) or product_id} is not for sale."}

    wanted = object_cart._num(request.get("quantity"), 1)
    if action == "add" and existing:
        wanted = object_cart._num(existing.get("quantity")) + wanted
    if wanted <= 0:
        if existing:
            object_records.delete_collection_record(
                "cart_items", existing["id"], base_dir=base, actor=ACTOR)
            items = [i for i in items if i["id"] != existing["id"]]
        return _state(base, cart, items)

    if existing:
        object_records.update_collection_record(
            "cart_items", existing["id"], {"quantity": str(wanted)},
            base_dir=base, actor=ACTOR)
        for item in items:
            if item["id"] == existing["id"]:
                item["quantity"] = str(wanted)
    else:
        item = object_records.create_collection_record(
            "cart_items",
            {"id": object_ids.new_uuid4(), "cart_id": cart["id"],
             "product_id": product_id,
             "description": _text(product.get("name")) or product_id,
             "quantity": str(wanted),
             # Stamped now; compared, never silently replaced, at checkout.
             "unit_price_cents": str(product.get("price_cents") or 0),
             "owner_id": _text(cart.get("owner_id"))},
            base_dir=base, actor=ACTOR)
        items.append(item)

    return _state(base, cart, items)
