"""site_pick_list -- everything that is owed and not yet in a box. GET
/pick-list.

A READ, never a table. There is no pick_list collection and there must
never be one: the list is ordered quantity minus shipped quantity, folded
live, exactly the way stock levels are folded from stock_moves
(object_stock.py) instead of stored. Storing it would mean two answers to
"what is left to pick" that can disagree, and the stored one is always the
one somebody trusts at the wrong moment.

GROUPED BY PRODUCT, because a picker walks the room once. Two orders each
wanting a mug is one trip to the mug shelf carrying two, not two trips --
and a per-order list is what makes small warehouses walk the same aisle
five times a morning. The orders each row is for are listed alongside, so
the packing bench can split the pile back out again.

Oldest first: the order that has waited longest is picked first, which is
the only fair rule that needs no judgement and the one a customer would
choose if asked.

Sorting by LOCATION walk order -- the bin-typed locations app-catalog
already models -- is the obvious next improvement and is deliberately not
here: nothing in this repo yet records which product lives in which bin,
and a walk order derived from data that does not exist would be a
confident wrong answer. Grouping by product is the honest half of that
idea and is most of its value.

Requires a signed-in identity, the same gate site_stock uses (and the same
shape: a sign-in prompt, not a 403). Orders are included when they belong
to the signed-in user OR carry no owner at all -- guest web checkout
leaves owner_id blank by design (a shopper is not required to have an
account), so filtering strictly by owner would show an empty warehouse to
the one person who has parcels to send.
"""

import html
import os
from decimal import Decimal, InvalidOperation

import object_records

# What a picker is asked to pick against: an order that has been committed
# and is not yet fully out the door. draft is not a commitment; shipped and
# delivered have nothing left; cancelled must never be picked.
PICKABLE_ORDER_STATUSES = {"confirmed", "processing", "partial"}

# Shipments in these never reached anybody, so their lines do not reduce
# what is still owed -- the goods have to be picked again.
NOT_DELIVERED_STATUSES = {"lost", "returned_to_sender"}


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _esc(value):
    return html.escape(_text(value))


def _quantity(value):
    try:
        return Decimal(_text(value) or "0")
    except InvalidOperation:
        return Decimal(0)


def _number(value):
    return format(value.normalize(), "f")


_STYLE = """
.pick-table { width: 100%; border-collapse: collapse; margin: 0.5rem 0 1.5rem; }
.pick-table th, .pick-table td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--line, #38384a); }
.pick-table th { font-weight: 600; }
.pick-table td.num, .pick-table th.num { text-align: right; }
.hint { color: var(--muted, #999); font-size: 0.9rem; }
@media print {
  nav, header.app, .noprint, .btn { display: none !important; }
  body { background: #fff; color: #000; }
}
"""


def _rows_for(base, user_id):
    """Fold orders x order lines x shipment lines into one row per product.

    Full scans of four collections: the right cost at this scale (a shop
    with a warehouse this size has hundreds of open lines, not millions),
    and the same trade site_stock's own summary makes.
    """
    try:
        orders = object_records.read_collection_records("orders", base_dir=base)
    except Exception:
        return []
    open_orders = {
        order["id"]: order for order in orders
        if _text(order.get("status")) in PICKABLE_ORDER_STATUSES
        and _text(order.get("doc_type") or "sale") == "sale"
        and _text(order.get("owner_id")) in ("", user_id)
    }
    if not open_orders:
        return []

    try:
        order_lines = [line for line in object_records.read_collection_records(
            "order_lines", base_dir=base)
            if _text(line.get("order_id")) in open_orders]
    except Exception:
        return []

    try:
        shipments = object_records.read_collection_records("shipments",
                                                           base_dir=base)
    except Exception:
        shipments = []
    counted = {row["id"] for row in shipments
               if _text(row.get("status")) not in NOT_DELIVERED_STATUSES}
    try:
        shipment_lines = object_records.read_collection_records(
            "shipment_lines", base_dir=base)
    except Exception:
        shipment_lines = []

    shipped = {}
    for line in shipment_lines:
        if _text(line.get("shipment_id")) not in counted:
            continue
        key = _text(line.get("order_line_id"))
        shipped[key] = shipped.get(key, Decimal(0)) + _quantity(line.get("quantity"))

    try:
        products = {row["id"]: row for row in
                    object_records.read_collection_records("products",
                                                           base_dir=base)}
    except Exception:
        products = {}

    grouped = {}
    for line in order_lines:
        remaining = _quantity(line.get("quantity")) - shipped.get(line["id"], Decimal(0))
        if remaining <= 0:
            continue
        order = open_orders[_text(line.get("order_id"))]
        product_id = _text(line.get("product_id"))
        description = _text(line.get("description"))
        # Group by product where there is one; a free-text line groups by
        # what it says, which is the only identity it has.
        key = product_id or f"text:{description.lower()}"
        row = grouped.setdefault(key, {
            "product_id": product_id,
            "name": (_text(products.get(product_id, {}).get("name"))
                     or description or product_id),
            "sku": _text(products.get(product_id, {}).get("sku")),
            "quantity": Decimal(0),
            "orders": [],
            "oldest": "",
        })
        row["quantity"] += remaining
        number = _text(order.get("number")) or order["id"]
        if number not in row["orders"]:
            row["orders"].append(number)
        placed = _text(order.get("order_date")) or _text(order.get("created_at"))[:10]
        if placed and (not row["oldest"] or placed < row["oldest"]):
            row["oldest"] = placed

    rows = list(grouped.values())
    # Oldest waiting first; a row with no date sorts last rather than first,
    # because "we do not know when this was ordered" is not a claim to
    # urgency.
    rows.sort(key=lambda row: (row["oldest"] or "9999-12-31", row["name"]))
    return rows


def GET(request):
    identity = request.get("_identity") or {}
    user_id = _text(identity.get("user_id"))

    if not user_id:
        body = ('<div class="pagehead"><h1>Pick list</h1></div>'
                '<p class="hint"><a href="/login?next=/pick-list">Sign in</a> '
                'to see what is waiting to be picked.</p>')
    else:
        rows = _rows_for(_base_dir(), user_id)
        table_rows = "".join(
            "<tr>"
            f"<td>{_esc(row['name'])}</td>"
            f"<td>{_esc(row['sku'])}</td>"
            f"<td class=\"num\">{_esc(_number(row['quantity']))}</td>"
            f"<td>{_esc(', '.join(row['orders']))}</td>"
            f"<td>{_esc(row['oldest'])}</td>"
            "</tr>"
            for row in rows
        ) or ('<tr><td colspan="5" class="hint">Nothing is waiting to be '
              'picked.</td></tr>')

        body = f"""
<div class="breadcrumb noprint"><a href="/">Home</a> / Pick list</div>
<div class="pagehead"><h1>Pick list</h1></div>
<p class="hint">Everything ordered and not yet on a shipment, grouped by
product so each shelf is visited once. Oldest orders first. This is a read
of the order and shipment records, never a stored list.</p>
<table class="pick-table">
<thead><tr><th>Product</th><th>SKU</th><th class="num">To pick</th><th>Orders</th><th>Oldest</th></tr></thead>
<tbody>{table_rows}</tbody>
</table>
<p class="hint noprint">Pack a shipment from an order to take lines off this
list.</p>
"""

    who = (f"signed in as <strong>{_esc(user_id)}</strong>" if user_id
           else '<a href="/login?next=/pick-list">sign in</a>')
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pick list</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header class="app"><h1><a href="/">DBBASIC</a></h1><div class="who">{who}</div></header>
{body}
</div>
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": page}
