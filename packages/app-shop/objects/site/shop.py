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

**A variant IS a product**, and this page is the only place that has to
know it. A size or a colour is its own products row -- own SKU, own
price, own stock level -- carrying parent_product_id (which card it
displays under) and options ({"size": "M", "colour": "navy"}). Stock
moves, pricing, checkout, fulfillment and the books all key on
product_id today, so variants-as-products means every one of those keeps
working with ZERO changes, and "how many medium navy totes are left?" is
the same question as any other stock question. The alternative -- a
variants table hanging under one product -- is the model that gives
every integration a products-vs-variants split personality and two ids
for one sellable thing. The cost lands here and nowhere else: the index
collapses children under one card, and the product page renders a picker
whose radios post the CHILD's product_id into the same add form every
other card uses.

**A note belongs to a LINE, so the box is on the add form.** "No onions"
is true of one burger, not of an order, and a single box at the bottom of
the checkout cannot say which line it meant -- that is what
`customer_note` already is, and it is addressed to the packer. A line
note is addressed to the cook. Two adds of one product with different
notes come back as two basket lines, because they are two things to make,
and the basket prints the note and any price delta under the line so the
shopper can check both before they commit. The page renders no input for
the delta itself: see _add_form.

Categories are one flat text field, grouped into <h2> headings in
alphabetical order, with the uncategorised last under "Everything else".
Never hidden: a product nobody got round to filing must still be
sellable, and a shop that quietly refused to show its own stock over a
blank field would be losing sales it could not even see.

All the actual decisions live in action_cart and action_checkout; this
object only renders them and passes the token along. That split is what
lets the same flow be driven by an API client, an agent over MCP, or a
different front end, without the rules living in a template.
"""

import html
import json
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

# Where product photographs already live: http_api_contract.USER_FILES_PATH,
# the endpoint app-files has served since it existed. Not a shop-specific
# image route -- inventing one would mean a second way to read the same
# bytes, with its own permission story to get wrong.
FILES_PATH = "/api/files"

# The heading for products nobody has categorised. Last on the page and
# never hidden: a blank field is a filing omission, not a decision to stop
# selling something.
UNCATEGORISED = "Everything else"

_STYLE = """
.shop-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 1rem; margin: 1rem 0 2rem; }
.shop-card { border: 1px solid var(--line, #38384a); border-radius: 8px; padding: 0.9rem; display: flex; flex-direction: column; gap: 0.4rem; }
.shop-card .price { font-size: 1.15rem; font-weight: 600; }
.shop-card .sku { font-size: 0.8rem; opacity: 0.65; }
.shop-category { font-size: 1rem; margin: 1.8rem 0 0.2rem; opacity: 0.85; }
.shop-image { display: block; width: 100%; aspect-ratio: 4 / 3; object-fit: cover; border-radius: 6px; }
.shop-image.placeholder { background: var(--line, #38384a); opacity: 0.3; }
.shop-detail .shop-image { max-width: 22rem; margin-bottom: 0.8rem; border-radius: 8px; }
.options { border: 0; margin: 0.8rem 0; padding: 0; display: flex; flex-direction: column; gap: 0.35rem; }
.options legend { padding: 0; font-weight: 600; }
.option { display: block; }
.option.out { opacity: 0.6; }
.checkout textarea { width: 100%; max-width: 32rem; }
.cart-table { width: 100%; border-collapse: collapse; margin: 0.5rem 0 1rem; }
.cart-table th, .cart-table td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--line, #38384a); }
.cart-table td.num, .cart-table th.num { text-align: right; }
.notice { border-left: 3px solid #c88; padding: 0.5rem 0.8rem; margin: 0.8rem 0; }
.notice.ok { border-left-color: #6a6; }
.shop-form { display: inline; }
.qty { width: 4rem; }
.note { width: 11rem; max-width: 100%; }
.line-note { display: block; font-size: 0.85rem; opacity: 0.75; font-style: italic; }
.line-modifier { display: block; font-size: 0.85rem; opacity: 0.75; }
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


def _find_product(products, product_id):
    """One product, or None when it is missing OR withdrawn from sale.

    Withdrawn and missing answer the same way on purpose: a public page that
    distinguished them would tell a stranger which ids exist.

    Takes the already-read list rather than re-reading the collection: the
    detail page needs the whole catalogue anyway, to find this product's
    variants and its parent.
    """
    wanted = str(product_id or "").strip()
    if not wanted:
        return None
    for row in products:
        if str(row.get("id") or "") == wanted:
            return row
    return None


def _detail_path(product_id):
    return "/shop/" + urllib.parse.quote(str(product_id or ""), safe="")


def _price_cents(product):
    try:
        return int(str(product.get("price_cents") or "0").strip() or 0)
    except (TypeError, ValueError):
        return 0


def _options(product):
    """The option map a variant declares, or {} for anything that is not one.

    Bad JSON is {} rather than an error. A mistyped brace in one product's
    options must not take that product off the shelf, and there is nobody
    on this page who could fix it anyway.
    """
    raw = str(product.get("options") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    # Insertion order is the SELLER's order -- "size" then "colour" reads
    # the way a label is written -- so it is kept rather than sorted.
    return {str(key): str(value) for key, value in parsed.items()}


def _option_label(product):
    """"M / navy" -- one line naming which variant this is.

    Falls back to the product's own name so a row with no options (a
    parent that is also sellable on its own) still has something to be
    called in a picker.
    """
    values = [value for value in _options(product).values() if value]
    return " / ".join(values) or str(product.get("name") or "")


def _children_by_parent(products):
    """Variants grouped under the product they display beneath.

    Only parents that are themselves on sale count. A child whose parent
    is withdrawn or missing would otherwise be collapsed under a card that
    is not on the page at all -- a product nobody can see and nobody can
    buy -- so it falls back to being its own card, which is exactly what
    it was before anybody set the field.
    """
    live = {str(row.get("id") or "") for row in products}
    groups = {}
    for row in products:
        parent = str(row.get("parent_product_id") or "").strip()
        if parent and parent in live and parent != str(row.get("id") or ""):
            groups.setdefault(parent, []).append(row)
    return groups


def _sellable(product, children):
    """Can this row go in a basket on its own?

    A parent WITH children and no price of its own is a display heading,
    not a thing: "Tote bag" at 0.00 is not what anybody is buying, and
    selling it would be charging nothing for a parcel nobody can pick. A
    parent that does carry a price is a real base product that happens to
    have variants too, and stays sellable.

    action_cart is the authority and refuses the sale there; this mirrors
    the rule so the page never offers a button that would be refused --
    the same posture as _availability, for the same reason.
    """
    return not (children and _price_cents(product) <= 0)


def _image(product):
    """The product photograph, or an honest blank.

    The catalogue stores a file id, never bytes, and the bytes come back
    from the endpoint app-files already serves. A missing photograph is a
    plain block rather than an <img> pointed at nothing: a broken-image
    icon reads as a shop whose pages do not work, which is worse than a
    product nobody has photographed yet.
    """
    file_id = str(product.get("image_file_id") or "").strip()
    if not file_id:
        return '<div class="shop-image placeholder"></div>'
    src = FILES_PATH + "/" + urllib.parse.quote(file_id, safe="")
    return (f'<img class="shop-image" src="{_esc(src)}" '
            f'alt="{_esc(product.get("name"))}" loading="lazy">')


def _add_form(product, token):
    """The one way anything enters a basket: a POST to /shop.

    The detail page uses this form too, so a product page is a different
    VIEW of the shop and not a second checkout path to keep in step.

    The note box is here rather than at checkout because it belongs to a
    LINE: "no onions" is true of one burger, and a single box at the
    bottom of the order cannot say which. action_cart makes a differing
    note its own line, so two burgers ordered differently stay two things
    to make.

    There is deliberately NO price-delta input. This page is public and
    action_cart is a public object, so an input that sets money is a
    shopper pricing their own lunch; the delta arrives from an API caller
    today, and the day a modifier picker exists it must price itself from
    the server's own list rather than from the form.
    """
    return f"""
<form method="post" action="/shop" class="shop-form">
  <input type="hidden" name="do" value="add">
  <input type="hidden" name="session_token" value="{_esc(token)}">
  <input type="hidden" name="product_id" value="{_esc(product.get('id'))}">
  <input class="qty" type="number" name="quantity" value="1" min="1" step="1">
  <input class="note" type="text" name="line_note" maxlength="300"
    placeholder="No onions, extra hot...">
  <button type="submit">Add</button>
</form>"""


def _card(product, children, token):
    """One card. A parent with variants gets ONE, not one per size.

    A grid with Small, Medium and Large as three separate tiles is a grid
    nobody can read: the shopper is looking for a tote bag, not for the
    medium. When the parent is only a heading (variants priced, parent
    not) the card shows the cheapest child's price as a "from" and offers
    the only thing that is actually possible -- go and choose -- because a
    disabled Add button is a dead control that teaches a shopper the shop
    is broken.
    """
    currency = _esc(product.get("currency") or "USD")
    if _sellable(product, children):
        price = f"{currency} {_money(product.get('price_cents'))}"
        action = _add_form(product, token)
    else:
        price = f"from {currency} {_money(min(_price_cents(c) for c in children))}"
        action = (f'<a href="{_esc(_detail_path(product.get("id")))}">'
                  f'Choose options</a>')
    return f"""
<div class="shop-card">
  <a href="{_esc(_detail_path(product.get('id')))}">{_image(product)}</a>
  <div><strong><a href="{_esc(_detail_path(product.get('id')))}">{_esc(product.get('name'))}</a></strong></div>
  <div class="sku">{_esc(product.get('sku') or '')}</div>
  <div class="price">{price}</div>
  {action}
</div>"""


def _grid(products, groups, token):
    cards = [_card(product, groups.get(str(product.get("id") or ""), []), token)
             for product in products]
    return f'<div class="shop-grid">{"".join(cards)}</div>'


def _product_cards(products, token):
    """The catalogue, collapsed by variant and grouped by category.

    A shop that has categorised nothing gets EXACTLY the grid it had
    before any of this: it is not a shop with one category called
    "Everything else", it is a shop that does not use categories, and a
    heading over the whole page would be furniture pretending to be
    information. Headings appear the moment somebody actually fills the
    field in.
    """
    if not products:
        return ('<p class="hint">Nothing is for sale yet. Add products in '
                '<a href="/products">the catalogue</a>.</p>')

    groups = _children_by_parent(products)
    collapsed = {str(child.get("id") or "")
                 for children in groups.values() for child in children}
    top = [row for row in products if str(row.get("id") or "") not in collapsed]

    buckets = {}
    for product in top:
        buckets.setdefault(str(product.get("category") or "").strip(),
                           []).append(product)
    named = sorted((key for key in buckets if key), key=str.lower)
    if not named:
        return _grid(top, groups, token)

    sections = [f'<h2 class="shop-category">{_esc(name)}</h2>'
                f'{_grid(buckets[name], groups, token)}' for name in named]
    if buckets.get(""):
        # Last, and never hidden. A product nobody got round to filing is
        # still stock somebody paid for, and a shop that dropped it off the
        # page would be refusing sales it could not even see it was losing.
        sections.append(f'<h2 class="shop-category">{UNCATEGORISED}</h2>'
                        f'{_grid(buckets[""], groups, token)}')
    return "".join(sections)


def _stock_state(base, product):
    """True, False, or None when nothing in this shop counts this thing.

    The three-way answer is the point. None is not "out": a service, a
    download, or a server with no stock app installed has no shelf, and
    collapsing that into False would refuse to sell the things that never
    run out. It is the same tracked-type rule action_checkout applies, so
    the page cannot promise what checkout would refuse -- nor refuse what
    checkout would happily take.
    """
    if object_stock is None:
        return None
    if str(product.get("product_type") or "") not in STOCKED_TYPES:
        return None
    try:
        return object_stock.total_quantity(product.get("id"), base_dir=base) > 0
    except Exception:
        return None


def _availability(base, product):
    """One low-key line: in stock, out of stock, or nothing at all.

    Silence is the right answer more often than it looks -- see
    _stock_state. Where stock IS counted, zero means zero: checkout
    refuses that sale, and a page that promised otherwise would be sending
    shoppers into a wall.
    """
    state = _stock_state(base, product)
    if state is None:
        return ""
    return f'<p class="stock">{"In stock" if state else "Out of stock"}</p>'


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


def _line_extras(line):
    """The instruction under a basket line, and what it cost.

    Both are shown, and shown apart, because they answer different
    questions: the note is what the shopper asked for and is the thing
    they will check before ordering, while the delta is why the price is
    not the one on the card. A delta with no explanation beside it is the
    small unexplained difference that becomes a phone call.
    """
    note = str(line.get("line_note") or "").strip()
    modifier = int(line.get("modifier_cents") or 0)
    blocks = ""
    if note:
        blocks += f'<span class="line-note">{_esc(note)}</span>'
    if modifier:
        blocks += (f'<span class="line-modifier">+{_money(modifier)} each'
                   f'</span>')
    return blocks


def _cart_table(cart, token, preview=None):
    lines = cart.get("lines") or []
    if not lines:
        return '<p class="hint">Your basket is empty.</p>'
    rows = []
    for line in lines:
        # cart_item_id, not just product_id: two lines can now share a
        # product (one burger with no onions, one without), and an Update
        # button that could only say WHICH PRODUCT would edit whichever
        # line happened to be first.
        rows.append(f"""
<tr>
  <td>{_esc(line['description'])}{_line_extras(line)}</td>
  <td class="num">
    <form method="post" action="/shop" class="shop-form">
      <input type="hidden" name="do" value="set">
      <input type="hidden" name="session_token" value="{_esc(token)}">
      <input type="hidden" name="product_id" value="{_esc(line['product_id'])}">
      <input type="hidden" name="cart_item_id" value="{_esc(line.get('cart_item_id'))}">
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
    """Two optional textareas, and no gift FLAG anywhere near them.

    The packing slip carries no prices by construction, so every parcel
    this shop sends is already gift-safe -- a tickbox would only be one
    more thing to forget, and forgetting it is how a present arrives with
    the amount paid stapled to it. The placeholder says so out loud,
    because a shopper who does not know that will not risk the message.
    """
    return f"""
<form method="post" action="/shop" class="checkout">
  <input type="hidden" name="do" value="checkout">
  <input type="hidden" name="session_token" value="{_esc(token)}">
  <p><label>Name<br><input type="text" name="customer_name"></label></p>
  <p><label>Email<br><input type="email" name="customer_email" required></label></p>
  <p><label>Special instructions (optional)<br>
  <textarea name="customer_note" rows="2"
    placeholder="Anything the packer needs to know"></textarea></label></p>
  <p><label>Gift message (optional)<br>
  <textarea name="gift_message" rows="2"
    placeholder="Printed on the packing slip, which never shows prices"></textarea></label></p>
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


def _picker(base, product, children, token):
    """Choose a variant: plain radios in the same add form as everywhere.

    This is where variants-as-products pays for itself. The radio's value
    is a product_id, the form posts to /shop with do=add like every card
    on the index, and action_cart, checkout, the stock ledger and the
    books never learn that a "variant" is a word anybody uses.

    Out-of-stock children are SHOWN and disabled, never dropped. Hiding
    the medium is how a shopper concludes this shop does not sell their
    size and goes to one that does; saying "Out of stock" tells them to
    come back, and it is also simply true.

    Nothing is pre-selected. A default that quietly ships the small
    because it sorted first is a wrong parcel, a return and a refund; a
    form that insists on being answered is a second of the shopper's time.
    The parent itself appears as a choice only when it is sellable in its
    own right -- otherwise it is a heading, not a thing to buy.
    """
    choices = ([product] if _sellable(product, children) else []) + list(children)
    rows = []
    for choice in choices:
        label = _esc(_option_label(choice))
        value = _esc(choice.get("id"))
        if _stock_state(base, choice) is False:
            rows.append(f'<label class="option out"><input type="radio" '
                        f'name="product_id" value="{value}" disabled> {label} '
                        f'&mdash; Out of stock</label>')
        else:
            money = (f'{_esc(choice.get("currency") or "USD")} '
                     f'{_money(choice.get("price_cents"))}')
            rows.append(f'<label class="option"><input type="radio" '
                        f'name="product_id" value="{value}" required> {label} '
                        f'&mdash; {money}</label>')
    return f"""
<form method="post" action="/shop" class="shop-form">
  <input type="hidden" name="do" value="add">
  <input type="hidden" name="session_token" value="{_esc(token)}">
  <fieldset class="options"><legend>Choose an option</legend>
  {"".join(rows)}
  </fieldset>
  <input class="qty" type="number" name="quantity" value="1" min="1" step="1">
  <input class="note" type="text" name="line_note" maxlength="300"
    placeholder="No onions, extra hot...">
  <button type="submit">Add</button>
</form>"""


def _variant_of(parent, product):
    """The line on a child's page that says what it is and where it came
    from.

    A shopper who landed on the medium from a search result needs the way
    back to the other sizes, or the only size this shop appears to sell is
    the one Google happened to index.
    """
    options = _options(product)
    named = ", ".join(f"{key}: {value}" for key, value in options.items())
    said = f'<p class="options-said">{_esc(named)}</p>' if named else ""
    return (f'{said}<p><a href="{_esc(_detail_path(parent.get("id")))}">'
            f'All options of {_esc(parent.get("name"))}</a></p>')


def _render_detail(base, product, children, parent, token, fresh):
    description = product.get("description") or ""
    unit = f" / {_esc(product.get('unit'))}" if product.get("unit") else ""
    currency = _esc(product.get("currency") or "USD")
    if _sellable(product, children):
        price = f"{currency} {_money(product.get('price_cents'))}{unit}"
    else:
        # A heading has no price of its own, and printing 0.00 would be a
        # number the shopper cannot buy anything at.
        price = f"from {currency} {_money(min(_price_cents(c) for c in children))}"
    # Availability belongs to a thing that can be added; for a parent the
    # answer is per-variant and lives on each radio, where the choice is.
    stock = "" if children else _availability(base, product)
    buy = (_picker(base, product, children, token) if children
           else _add_form(product, token))
    body = f"""
<div class="breadcrumb"><a href="/">Home</a> / <a href="/shop">Shop</a> /
{_esc(product.get('name'))}</div>
<div class="pagehead"><h1>{_esc(product.get('name'))}</h1></div>
<div class="shop-detail">
  {_image(product)}
  <div class="sku">{_esc(product.get('sku') or '')}</div>
  <div class="price">{price}</div>
  {stock}
  <div class="description">{_esc(description)}</div>
  {buy}
  {_variant_of(parent, product) if parent is not None else ''}
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
    products = _products(base)
    product = _find_product(products, product_id)
    if product is None:
        return _render_missing(token, fresh)
    children = _children_by_parent(products).get(str(product.get("id") or ""), [])
    # A parent that is withdrawn or missing is the same as no parent at
    # all: the child is a product in its own right and its page must still
    # sell it, rather than pointing at a card that is not on the shop.
    parent = _find_product(products, product.get("parent_product_id"))
    if parent is not None and str(parent.get("id") or "") == str(product.get("id") or ""):
        parent = None
    return _render_detail(base, product, children, parent, token, fresh)


def _handle(request):
    base = _base_dir()
    token, fresh = _token(request)
    form = request.get("_form") or request
    do = str(form.get("do") or "").strip()
    notice = ""

    if do in ("add", "set", "remove", "clear"):
        payload = {"session_token": token, "action": do,
                   "product_id": form.get("product_id"),
                   "quantity": form.get("quantity")}
        if do == "add":
            # Only on add. Passing an absent note on `set` would tell
            # action_cart to blank a note the shopper typed, which is
            # what "stated" versus "omitted" means over there.
            payload["line_note"] = form.get("line_note")
        if form.get("cart_item_id"):
            payload["cart_item_id"] = form.get("cart_item_id")
        result = _call("action_cart", payload)
        notice = _notice(result)
    elif do == "checkout":
        result = _call("action_checkout", {
            "session_token": token,
            "customer_email": form.get("customer_email"),
            "customer_name": form.get("customer_name"),
            # Two optional fields, straight through: the page decides
            # nothing about them, action_checkout decides where they land.
            "customer_note": form.get("customer_note"),
            "gift_message": form.get("gift_message"),
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
