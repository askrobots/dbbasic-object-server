"""/shop -- browse, basket, checkout, on one public page.

Deliberately one page and deliberately server-rendered. Every step a
shopper takes is a form POST that comes back as HTML, so the whole flow
works with no JavaScript at all: no framework, no client-side cart state
to drift out of sync with the server's, and nothing to go wrong on a
phone with one bar of signal in a shop doorway.

The basket is identified by a `cart` cookie holding an opaque session
token. It is not an identity and it is not a cross-site key -- it says
WHICH basket, never who. Nothing here reads or sets anything else: the
server hands routed objects every cookie EXCEPT the identity session, and
accepts exactly one response header back, `set_cookie`.

A product also has a page of its own at /shop/{product_id} -- a grid of
cards is a list of names and prices, and nobody buys a thing they cannot
read about first. Its add-to-basket form posts to /shop, the same index
the cards post to, so there is one basket path and not two.

All the actual decisions live in action_cart and action_checkout; this
object only renders them and passes the token along. That split is what
lets the same flow be driven by an API client, an agent over MCP, or a
different front end, without the rules living in a template.
"""

import html
import os
import secrets
import urllib.parse

import object_execution
import object_records
import python_object_runtime

try:
    import object_stock
except ImportError:     # no stock app installed: the page says nothing at all
    object_stock = None

COOKIE = "cart"

# The product types that sit on a shelf and can run out, same rule
# action_checkout applies. A download or an hour of work never does, and
# saying "Out of stock" about one would refuse a sale that checkout would
# happily take.
STOCKED_TYPES = ("physical", "asset", "")

_STYLE = """
.shop-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 1rem; margin: 1rem 0 2rem; }
.shop-card { border: 1px solid var(--line, #38384a); border-radius: 8px; padding: 0.9rem; display: flex; flex-direction: column; gap: 0.4rem; }
.shop-card .price { font-size: 1.15rem; font-weight: 600; }
.shop-card .sku { font-size: 0.8rem; opacity: 0.65; }
.cart-table { width: 100%; border-collapse: collapse; margin: 0.5rem 0 1rem; }
.cart-table th, .cart-table td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--line, #38384a); }
.cart-table td.num, .cart-table th.num { text-align: right; }
.notice { border-left: 3px solid #c88; padding: 0.5rem 0.8rem; margin: 0.8rem 0; }
.notice.ok { border-left-color: #6a6; }
.shop-form { display: inline; }
.qty { width: 4rem; }
.shop-detail .price { font-size: 1.4rem; font-weight: 600; margin: 0.4rem 0; }
.shop-detail .description { white-space: pre-wrap; margin: 1rem 0; max-width: 42rem; }
.shop-detail .stock { font-size: 0.9rem; opacity: 0.7; }
"""


def _esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", object_records.DEFAULT_DATA_DIR)


def _money(cents):
    try:
        return f"{int(cents) / 100:,.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _call(object_id, payload):
    """Run a sibling object in-process.

    The page owns no rules of its own: it asks the objects that do.
    """
    runtime = python_object_runtime.PythonObjectRuntime()
    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest(
            object_id, method="POST", payload=payload))
    return result.result if result.ok else {"error": "That did not work."}


def _token(request):
    cookies = request.get("_cookies") or {}
    token = str(cookies.get(COOKIE) or "").strip()
    if token:
        return token, False
    form = request.get("_form") or request
    token = str(form.get("session_token") or "").strip()
    if token:
        return token, False
    return secrets.token_urlsafe(18), True


def _products(base):
    try:
        rows = object_records.read_collection_records("products", base_dir=base)
    except Exception:
        return []
    live = [row for row in rows
            if str(row.get("is_active") or "").lower() not in ("false", "0", "no", "off")]
    return sorted(live, key=lambda row: str(row.get("name") or ""))


def _find_product(base, product_id):
    """One product, or None when it is missing OR withdrawn from sale.

    Withdrawn and missing answer the same way on purpose: a public page that
    distinguished them would tell a stranger which ids exist.
    """
    wanted = str(product_id or "").strip()
    if not wanted:
        return None
    for row in _products(base):
        if str(row.get("id") or "") == wanted:
            return row
    return None


def _detail_path(product_id):
    return "/shop/" + urllib.parse.quote(str(product_id or ""), safe="")


def _add_form(product, token):
    """The one way anything enters a basket: a POST to /shop.

    The detail page uses this form too, so a product page is a different
    VIEW of the shop and not a second checkout path to keep in step.
    """
    return f"""
<form method="post" action="/shop" class="shop-form">
  <input type="hidden" name="do" value="add">
  <input type="hidden" name="session_token" value="{_esc(token)}">
  <input type="hidden" name="product_id" value="{_esc(product.get('id'))}">
  <input class="qty" type="number" name="quantity" value="1" min="1" step="1">
  <button type="submit">Add</button>
</form>"""


def _product_cards(products, token):
    if not products:
        return ('<p class="hint">Nothing is for sale yet. Add products in '
                '<a href="/products">the catalogue</a>.</p>')
    cards = []
    for product in products:
        cards.append(f"""
<div class="shop-card">
  <div><strong><a href="{_esc(_detail_path(product.get('id')))}">{_esc(product.get('name'))}</a></strong></div>
  <div class="sku">{_esc(product.get('sku') or '')}</div>
  <div class="price">{_esc(product.get('currency') or 'USD')} {_money(product.get('price_cents'))}</div>
  {_add_form(product, token)}
</div>""")
    return f'<div class="shop-grid">{"".join(cards)}</div>'


def _availability(base, product):
    """One low-key line: in stock, out of stock, or nothing at all.

    Silence is the right answer more often than it looks -- for a service
    or a download, and for a server with no stock app installed. Guessing
    there would turn a shop that simply does not count things into a shop
    that appears to have sold out of everything. Where stock IS counted,
    zero means zero: checkout refuses that sale, and a page that promised
    otherwise would be sending shoppers into a wall.
    """
    if object_stock is None:
        return ""
    if str(product.get("product_type") or "") not in STOCKED_TYPES:
        return ""
    try:
        quantity = object_stock.total_quantity(product.get("id"), base_dir=base)
    except Exception:
        return ""
    state = "In stock" if quantity > 0 else "Out of stock"
    return f'<p class="stock">{state}</p>'


def _preview(token):
    """What this basket would actually cost, asked of action_checkout.

    The page owns no arithmetic: postage and tax are decided by the same
    object that will charge them, so the footer a shopper reads and the
    invoice they get cannot disagree. A preview writes nothing.

    None when the preview cannot be had -- an empty basket, a price that
    moved, something out of stock. Those all have their own notice
    already, and a basket that still shows its plain total is a better
    answer than one whose totals vanish.
    """
    result = _call("action_checkout", {"session_token": token, "preview": "true"})
    if isinstance(result, dict) and result.get("preview"):
        return result
    return None


def _totals_rows(cart, preview):
    """The footer under the basket: today's one Total, or the breakdown.

    A shop charging neither tax nor postage sees EXACTLY what it saw
    before -- one Total row. Zero-rows are not neutral: "Shipping 0.00"
    and "Tax 0.00" read as a broken shop, not a shop that does not do
    those things.
    """
    if preview is None:
        return (f'<tr><th colspan="3">Total</th>'
                f'<th class="num">{_money(cart.get("subtotal_cents"))}</th></tr>')

    shipping = int(preview.get("shipping_cents") or 0)
    free = bool(preview.get("shipping_free"))
    tax = int(preview.get("tax_cents") or 0)
    if not shipping and not free and not tax:
        return (f'<tr><th colspan="3">Total</th>'
                f'<th class="num">{_money(preview.get("subtotal_cents"))}</th></tr>')

    rows = [f'<tr><td colspan="3">Subtotal</td>'
            f'<td class="num">{_money(preview.get("subtotal_cents"))}</td></tr>']
    if shipping or free:
        # Say it. Earning free delivery is the one moment in a basket
        # that a shopper is pleased about, and a silent 0.00 throws it
        # away -- it reads as a shop that forgot to charge rather than
        # one that gave them something.
        amount = ('<strong>Free shipping</strong>' if free
                  else _money(shipping))
        rows.append(f'<tr><td colspan="3">Shipping</td>'
                    f'<td class="num">{amount}</td></tr>')
    if tax:
        rows.append(f'<tr><td colspan="3">Tax</td>'
                    f'<td class="num">{_money(tax)}</td></tr>')
    rows.append(f'<tr><th colspan="3">Total</th>'
                f'<th class="num">{_money(preview.get("total_cents"))}</th></tr>')
    return "".join(rows)


def _cart_table(cart, token, preview=None):
    lines = cart.get("lines") or []
    if not lines:
        return '<p class="hint">Your basket is empty.</p>'
    rows = []
    for line in lines:
        rows.append(f"""
<tr>
  <td>{_esc(line['description'])}</td>
  <td class="num">
    <form method="post" action="/shop" class="shop-form">
      <input type="hidden" name="do" value="set">
      <input type="hidden" name="session_token" value="{_esc(token)}">
      <input type="hidden" name="product_id" value="{_esc(line['product_id'])}">
      <input class="qty" type="number" name="quantity" value="{_esc(line['quantity'])}" min="0" step="any">
      <button type="submit">Update</button>
    </form>
  </td>
  <td class="num">{_money(line['unit_price_cents'])}</td>
  <td class="num">{_money(line['line_total_cents'])}</td>
</tr>""")
    return f"""
<table class="cart-table">
<thead><tr><th>Item</th><th class="num">Qty</th><th class="num">Each</th><th class="num">Total</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
<tfoot>{_totals_rows(cart, preview)}</tfoot>
</table>"""


def _checkout_form(token):
    return f"""
<form method="post" action="/shop">
  <input type="hidden" name="do" value="checkout">
  <input type="hidden" name="session_token" value="{_esc(token)}">
  <p><label>Name<br><input type="text" name="customer_name"></label></p>
  <p><label>Email<br><input type="email" name="customer_email" required></label></p>
  <p><button type="submit">Place order</button></p>
</form>"""


def _notice(result):
    """Turn an object's refusal into something a shopper can act on."""
    if not isinstance(result, dict):
        return ""
    if result.get("order_id") and not result.get("error"):
        # The pay link is the most useful thing on the page at this moment,
        # so it gets its own line rather than being buried in a sentence: a
        # shopper who has just committed should not have to wait for an
        # email to find out how to pay.
        pay = ""
        if result.get("pay_path"):
            pay = (f'<p><strong><a href="{_esc(result["pay_path"])}">Pay now'
                   f'</a></strong></p>')
        return (f'<div class="notice ok"><strong>Order placed.</strong> '
                f'Reference {_esc(str(result["order_id"])[:8].upper())}. '
                f'A receipt is on its way to you.{pay}</div>')
    error = result.get("error")
    if not error:
        return ""
    detail = []
    for change in result.get("price_changes") or []:
        detail.append(f"{_esc(change['description'])}: was {_money(change['was_cents'])}, "
                      f"now {_money(change['now_cents'])}")
    for short in result.get("unavailable") or []:
        detail.append(f"{_esc(short['description'])}: {_esc(short['available'])} left, "
                      f"you asked for {_esc(short['wanted'])}")
    for gone in result.get("inactive") or []:
        detail.append(f"{_esc(gone.get('description') or gone.get('product_id'))}: "
                      f"{_esc(gone['reason'])}")
    extra = ("<ul><li>" + "</li><li>".join(detail) + "</li></ul>") if detail else ""
    confirm = ""
    if result.get("price_changes"):
        confirm = ('<p>These are the current prices. Place the order again to '
                   'accept them.</p>')
    return f'<div class="notice"><strong>{_esc(error)}</strong>{extra}{confirm}</div>'


def _response(title, body, token, fresh, status=200):
    """One shell for every page this object serves -- index, product, 404.

    A shopper who mistypes a product id is still in the shop, so the not-
    found page is the same page furniture with a different sentence in it,
    never a traceback and never a bare JSON error.
    """
    html_page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header class="app"><h1><a href="/">DBBASIC</a></h1></header>
{body}
</div>
</body>
</html>"""

    response = {"status": status, "content_type": "text/html; charset=utf-8",
                "body": html_page}
    if fresh:
        # Path-scoped, http-only, SameSite=Lax: it identifies a basket and is
        # useless anywhere else. `set_cookie` is the single response header
        # the server accepts from an object -- see _object_set_cookie_headers.
        response["set_cookie"] = (
            f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=1209600")
    return response


def _render(token, cart, products, notice, fresh, preview=None):
    body = f"""
<div class="breadcrumb"><a href="/">Home</a> / Shop</div>
<div class="pagehead"><h1>Shop</h1></div>
{notice}
{_product_cards(products, token)}
<h2 style="font-size:1rem">Your basket</h2>
{_cart_table(cart, token, preview)}
{_checkout_form(token) if (cart.get('lines') or []) else ''}
<p class="hint">Prices are confirmed when you order. Nothing is reserved until
payment arrives.</p>"""
    return _response("Shop", body, token, fresh)


def _render_detail(base, product, token, fresh):
    description = product.get("description") or ""
    unit = f" / {_esc(product.get('unit'))}" if product.get("unit") else ""
    body = f"""
<div class="breadcrumb"><a href="/">Home</a> / <a href="/shop">Shop</a> /
{_esc(product.get('name'))}</div>
<div class="pagehead"><h1>{_esc(product.get('name'))}</h1></div>
<div class="shop-detail">
  <div class="sku">{_esc(product.get('sku') or '')}</div>
  <div class="price">{_esc(product.get('currency') or 'USD')} {_money(product.get('price_cents'))}{unit}</div>
  {_availability(base, product)}
  <div class="description">{_esc(description)}</div>
  {_add_form(product, token)}
</div>
<p><a href="/shop">Back to shop</a></p>"""
    return _response(str(product.get("name") or "Product"), body, token, fresh)


def _render_missing(token, fresh):
    body = """
<div class="breadcrumb"><a href="/">Home</a> / <a href="/shop">Shop</a></div>
<div class="pagehead"><h1>Not for sale</h1></div>
<p>We could not find that. It may have sold out for good, or the link may
have a typo in it.</p>
<p><a href="/shop">Back to shop</a></p>"""
    return _response("Not for sale", body, token, fresh, status=404)


def _detail(request, product_id):
    base = _base_dir()
    token, fresh = _token(request)
    product = _find_product(base, product_id)
    if product is None:
        return _render_missing(token, fresh)
    return _render_detail(base, product, token, fresh)


def _handle(request):
    base = _base_dir()
    token, fresh = _token(request)
    form = request.get("_form") or request
    do = str(form.get("do") or "").strip()
    notice = ""

    if do in ("add", "set", "remove", "clear"):
        result = _call("action_cart", {
            "session_token": token, "action": do,
            "product_id": form.get("product_id"),
            "quantity": form.get("quantity")})
        notice = _notice(result)
    elif do == "checkout":
        result = _call("action_checkout", {
            "session_token": token,
            "customer_email": form.get("customer_email"),
            "customer_name": form.get("customer_name"),
            # A second attempt after seeing the new prices IS the shopper
            # agreeing to them, which is why this is not a hidden default.
            "confirm_prices": form.get("confirm_prices")})
        notice = _notice(result)
        if result.get("order_id"):
            token, fresh = secrets.token_urlsafe(18), True   # a fresh basket

    cart = _call("action_cart", {"session_token": token, "action": "get"})
    cart = cart if isinstance(cart, dict) else {}
    # Only ask what it costs when there is something to cost. A preview
    # of an empty basket is a 404 the page would only throw away.
    preview = _preview(token) if (cart.get("lines") or []) else None
    return _render(token, cart, _products(base), notice, fresh, preview)


def GET(request):
    # /shop/{product_id} arrives as a top-level route capture. POST is
    # deliberately NOT routed here: every basket change posts to /shop, so
    # there is one add path and the detail page is only ever a view.
    product_id = str(request.get("product_id") or "").strip()
    if product_id:
        return _detail(request, product_id)
    return _handle(request)


def POST(request):
    return _handle(request)
