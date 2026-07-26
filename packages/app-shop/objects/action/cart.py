"""action_cart -- the basket verbs: get, add, set, remove, clear.

POST {session_token, action, product_id?, quantity?, line_note?,
      modifier_cents?, cart_item_id?}

One object rather than five, because these are the same operation with a
different argument and splitting them would mean five copies of "find or
make this session's basket". The verb is data, which is the same reason
the scheduler's tasks are data.

No stock is touched here and no price is refreshed here. Adding to a
basket is not a commitment: reserving stock on add is how a shop shows
"sold out" for goods nobody bought, and quietly repricing a basket is how
a shopper is charged something they never agreed to. Both are decided at
checkout, where a person is looking.

One thing IS refused here, and it is a merchandising rule rather than a
stock one: a product that has variants and no price of its own is a
display heading, not a thing. "Tote bag" is what somebody is looking for;
"Tote bag, medium, navy" is what they can actually be sent, and it is a
products row of its own with its own SKU, price and stock level (see
products.json's own description on why a variant IS a product). Adding
the heading would put a line with no price on an order nobody can pick,
so it is refused HERE, in the object that owns the rule, and the refusal
names the options -- a "no" that does not say what to do instead is a
lost sale. site_shop mirrors the same rule when it decides whether to
draw an Add button, so the page never offers what this would refuse.

**An instruction is not a product, and it makes its own line.** A large
latte is genuinely its own product with its own SKU, price and stock, and
variants-as-products above already handles that correctly. "No onions" is
not a product: it has no SKU, it is true of one line of one order, and a
catalogue row per instruction would be thousands of dead products. So a
line carries `line_note` and `modifier_cents` ("oat milk +60c" is the
same instruction with a price delta, which is why the delta lives on the
line rather than in the price book), and two adds of the same product
with DIFFERENT instructions are two lines rather than a quantity of two.
That is not a nicety: one burger with no onions and one with is two
things to make, and merging them on product_id alone would send the cook
a line they cannot follow. Same product, same instruction, same delta
still merges, so a shop that never types a note sees exactly the basket
it saw before.

`set` and `remove` take an optional `cart_item_id` to say WHICH line,
because product_id stopped being unique the moment two lines could share
one. Without it they fall back to the first line for that product --
today's behaviour, kept so every existing caller still works -- and
site_shop always sends the id, so the page is never the caller guessing.

**A negative modifier is refused.** action_cart is a PUBLIC object (a
basket must not require an account), so anything the request can put into
money is something a stranger can put into money, and "-500" would be a
shopper pricing their own lunch. A genuine discount is a pricing decision
that belongs in the price book or in a variant with its own price, never
in a field the buyer fills in. When a modifier PICKER eventually exists,
its prices must be looked up server-side from whatever lists them -- the
request may say which modifier, never what it costs.
"""

import json
import os

import object_cart
import object_ids
import object_records

ACTOR = "action_cart"

ACTIONS = ("get", "add", "set", "remove", "clear")

INACTIVE = ("false", "0", "no", "off")


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


def _price_cents(product):
    try:
        return int(_text(product.get("price_cents")) or 0)
    except (TypeError, ValueError):
        return 0


def _options(product):
    """The option map a variant declares, or {} for anything that is not
    one.

    Parsed here as well as in site_shop rather than shared through a
    module: it is six lines, and a page and an action reaching for the
    same import across an object boundary is a coupling neither of them
    needs. Bad JSON is {} -- a mistyped brace must not decide whether a
    product can be sold.
    """
    raw = _text(product.get("options"))
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return ({str(key): str(value) for key, value in parsed.items()}
            if isinstance(parsed, dict) else {})


def _variants_of(base, product_id):
    """The variants that display under this product and are still on sale.

    Read fresh every time. This is the gate that decides whether a row is
    a thing or a heading, and a stale answer either sells a heading or
    refuses a real product.
    """
    try:
        rows = object_records.read_collection_records("products", base_dir=base)
    except Exception:
        return []
    return [row for row in rows
            if _text(row.get("parent_product_id")) == product_id
            and _text(row.get("id")) != product_id
            and _text(row.get("is_active")).lower() not in INACTIVE]


def _modifier_cents(value):
    """The line's price delta, or None when it is not a whole number of
    minor units. None is a REFUSAL upstream, not a zero: silently reading
    "sixty pence" as no charge is the shop giving the oat milk away."""
    raw = _text(value)
    if not raw:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _same_line(item, product_id, note, modifier):
    """Is this basket line the same THING as what is being added?

    Product, instruction and delta all three. Two burgers where one has no
    onions are two lines, because they are two things to make.
    """
    return (_text(item.get("product_id")) == product_id
            and _text(item.get("line_note")) == note
            and _modifier_cents(item.get("modifier_cents")) == modifier)


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
    cart_item_id = _text(request.get("cart_item_id"))
    if not product_id and not cart_item_id:
        return {"status": 400,
                "error": "product_id (or cart_item_id) is required for this action"}

    note = _text(request.get("line_note"))
    modifier = _modifier_cents(request.get("modifier_cents"))
    if modifier is None:
        return {"status": 400,
                "error": "modifier_cents must be a whole number of minor "
                         "units -- 60 for sixty cents, not \"0.60\"."}
    if modifier < 0:
        # See the module docstring: this object is public, so a negative
        # delta is a stranger pricing their own lunch. A real discount is a
        # price-book decision, not something the buyer types.
        return {"status": 400,
                "error": "A modifier can add to a line's price, never take "
                         "money off it. A discount is a price the shop sets, "
                         "not one the basket asks for."}

    if action == "add":
        # Product, instruction and delta: same three, same line.
        existing = next((i for i in items
                         if _same_line(i, product_id, note, modifier)), None)
    elif cart_item_id:
        existing = next((i for i in items
                         if _text(i.get("id")) == cart_item_id), None)
    else:
        existing = next((i for i in items
                         if _text(i.get("product_id")) == product_id), None)
    if not product_id and existing is not None:
        # Addressed purely by line id: the line already knows what it is.
        product_id = _text(existing.get("product_id"))
    if not product_id:
        return {"status": 400,
                "error": "No line with that cart_item_id is in this basket."}

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
    if _text(product.get("is_active")).lower() in INACTIVE:
        return {"status": 409,
                "error": f"{_text(product.get('name')) or product_id} is not for sale."}

    variants = _variants_of(base, product_id)
    if variants and _price_cents(product) <= 0:
        name = _text(product.get("name")) or product_id
        # Name the options in the sentence AND hand back the ids: the
        # sentence is for the shopper reading a page, the list is for an
        # API client or an agent that has to pick one without scraping
        # prose. Both, because this refusal is the whole of the guidance
        # either of them gets.
        choices = [{"product_id": _text(variant.get("id")),
                    "label": " / ".join(
                        value for value in _options(variant).values() if value)
                             or _text(variant.get("name")),
                    "price_cents": str(_price_cents(variant))}
                   for variant in variants]
        # "Choose a size" beats "choose an option" when the seller has said
        # what the axis is called -- it is their word for it, and reading
        # it back is the difference between a form and a conversation.
        axes = []
        for variant in variants:
            for key in _options(variant):
                if key not in axes:
                    axes.append(key)
        what = " and ".join(axes) if axes else "option"
        listed = ", ".join(choice["label"] for choice in choices)
        return {"status": 409,
                "error": f"{name} comes in more than one {what}. "
                         f"Choose one: {listed}.",
                "product_id": product_id,
                "options": choices}

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
        changes = {"quantity": str(wanted)}
        # On `set`, a stated instruction or delta EDITS the line -- somebody
        # correcting "no onions" to "extra onions" is changing this line,
        # not buying a different thing. Only what was actually stated: an
        # ordinary quantity update must not silently wipe a note the
        # shopper typed a minute ago.
        if "line_note" in request:
            changes["line_note"] = note
        if "modifier_cents" in request:
            changes["modifier_cents"] = str(modifier)
        object_records.update_collection_record(
            "cart_items", existing["id"], changes,
            base_dir=base, actor=ACTOR)
        for item in items:
            if item["id"] == existing["id"]:
                item.update(changes)
    else:
        item = object_records.create_collection_record(
            "cart_items",
            {"id": object_ids.new_uuid4(), "cart_id": cart["id"],
             "product_id": product_id,
             "description": _text(product.get("name")) or product_id,
             "quantity": str(wanted),
             # Stamped now; compared, never silently replaced, at checkout.
             "unit_price_cents": str(product.get("price_cents") or 0),
             # What was asked for on this line, and what asking cost.
             "line_note": note,
             "modifier_cents": str(modifier),
             "owner_id": _text(cart.get("owner_id"))},
            base_dir=base, actor=ACTOR)
        items.append(item)

    return _state(base, cart, items)
