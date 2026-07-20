"""Stock levels page: current on-hand quantities per product x location,
plus per-product on-hand totals (customer/supplier/virtual locations
excluded -- see object_stock.py).

Stock levels are DERIVED, never stored (object_stock.py folds the
immutable stock_moves log on every call). Every other page in this
package (products.py, product_view.py, locations.py) reads its collection
client-side via a browser fetch to /collections/{collection}/records and
lets the platform's row-filter permission enforce owner-scoping -- that
generic endpoint only exists for stored collections, though, and stock
levels are not one: there is no "stock_levels" collection to fetch. Two
paths were open to close that gap: add a new authenticated GET endpoint
(e.g. /api/stock/levels), or compute the summary here, server-side, in
this page's own GET(). This file takes the second path -- it is the
simpler correct one: no new route, no new permission surface, and no
duplicate owner-scoping logic to keep in sync with permissions/rules.json
-- this page's GET() applies the exact same owner_id == signed-in-user
scoping object_stock.stock_levels()'s `owner` parameter is built for,
matching the row_filter every other owner-scoped collection in this
package already enforces. The tradeoff, honestly: this page does not
live-refresh over the websocket the instant a stock_moves row lands
(there is no fetched JSON payload to re-render in place) -- it re-fetches
the whole page instead when stock_moves changes, which is simple and
correct at this collection's expected scale.
"""

_SCRIPT = """
(function () {
  let _lt = null;
  const reload = () => { clearTimeout(_lt); _lt = setTimeout(() => location.reload(), 400); };
  (function wait() {
    if (window.dbbasicSubscribe) window.dbbasicSubscribe("stock_moves", reload);
    else setTimeout(wait, 300);
  })();
})();
"""

_STYLE = """
.stock-table { width: 100%; border-collapse: collapse; margin: 0.5rem 0 1.5rem; }
.stock-table th, .stock-table td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--line, #38384a); }
.stock-table th { font-weight: 600; }
.stock-table td.num, .stock-table th.num { text-align: right; }
"""

import os

import object_records
import object_stock

DATA_DIR_ENV = "DBBASIC_DATA_DIR"


def _data_dir() -> str:
    # Mirrors app-orders' order_totals.py's own _data_dir(): standalone,
    # reads os.environ directly rather than depending on object_server.
    return os.environ.get(DATA_DIR_ENV, object_records.DEFAULT_DATA_DIR)


def _name_lookup(collection: str, owner: str, base_dir: str) -> dict[str, str]:
    """Return {id: name} for one owner's records in a collection, or {}
    when the read fails for any reason (e.g. no rows yet) -- a display
    fallback (the raw id) always covers a lookup miss, so this never needs
    to raise.
    """
    try:
        records = object_records.read_collection_records(collection, base_dir=base_dir)
    except (LookupError, OSError):
        return {}
    return {
        record["id"]: (record.get("name") or record["id"])
        for record in records
        if record.get("id") and record.get("owner_id") == owner
    }


def _esc(value) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#39;")
    )


def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_stock served", user_id=user_id or "anonymous")

    if not user_id:
        body = '<p class="hint"><a href="/login?next=/stock">Sign in</a> to see your stock levels.</p>'
        script = ""
    else:
        base_dir = _data_dir()
        summary = object_stock.stock_levels(base_dir=base_dir, owner=user_id)
        product_names = _name_lookup("products", user_id, base_dir)
        location_names = _name_lookup("locations", user_id, base_dir)

        level_rows = "".join(
            "<tr><td>{product}</td><td>{location}</td><td class=\"num\">{quantity}</td></tr>".format(
                product=_esc(product_names.get(row["product_id"], row["product_id"])),
                location=_esc(location_names.get(row["location_id"], row["location_id"])),
                quantity=_esc(row["quantity"]),
            )
            for row in summary["levels"]
        ) or '<tr><td colspan="3" class="hint">No stock moves yet.</td></tr>'

        total_rows = "".join(
            "<tr><td>{product}</td><td class=\"num\">{quantity}</td></tr>".format(
                product=_esc(product_names.get(row["product_id"], row["product_id"])),
                quantity=_esc(row["quantity"]),
            )
            for row in summary["totals"]
        ) or '<tr><td colspan="2" class="hint">No on-hand stock yet.</td></tr>'

        body = f"""
<div class="breadcrumb"><a href="/">Home</a> / Stock</div>
<div class="pagehead"><h1>Stock Levels</h1><a class="btn" href="/stock">Refresh</a></div>
<h2 style="font-size:1rem">On hand by location</h2>
<table class="stock-table">
<thead><tr><th>Product</th><th>Location</th><th class="num">Quantity</th></tr></thead>
<tbody>{level_rows}</tbody>
</table>
<h2 style="font-size:1rem; margin-top:1.5rem">On-hand totals</h2>
<p class="hint" style="margin-top:0">Excludes customer, supplier, and virtual locations -- see /locations for each location's type.</p>
<table class="stock-table">
<thead><tr><th>Product</th><th class="num">Quantity</th></tr></thead>
<tbody>{total_rows}</tbody>
</table>
<p class="hint"><a href="/products">Products</a> &middot; <a href="/locations">Locations</a></p>
"""
        script = f"<script>{_SCRIPT}</script>"

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else '<a href="/login?next=/stock">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stock Levels</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header class="app"><h1><a href="/">DBBASIC</a></h1><div class="who">{who}</div></header>
{body}
</div>
{script}
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
