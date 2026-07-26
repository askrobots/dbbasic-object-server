"""site_dropship_margin -- what this drop-ship actually earned.
GET /orders/{order_id}/margin.

A READ over two rows, and it is worth saying why that is the entire
feature. The spec's claim about drop-shipping was that "margin is already
a read because both orders carry money" -- so if this page needed a
stored margin field, a snapshot, or a third document, the model would
have been wrong. It needs none: the sale order says what the customer
paid, the linked purchase order says what the vendor charged, and the
subtraction is object_billing.margin, the same function that answers the
same question for metered usage. Not a second implementation of it: a
margin that rounded differently on two pages is a margin nobody trusts,
and there is exactly one right answer to revenue minus cost.

It works from EITHER end. Open it on the sale order or on the purchase
order and it finds the other through linked_order_id, because a link
written both ways is a link you never have to search for.

**Where the two numbers come from, and what happens when they are not
there yet.** Each order's stamped total_cents is preferred -- that is the
document's own number, stamped by system_order_totals, and the one a
human sees on the order itself. When it is zero the lines are folded
instead, because order_totals is a post-commit REACTION: a purchase order
raised ten seconds ago by action_dropship_order may not have been stamped
yet, and quoting a margin of 100% off a total that simply has not landed
would be a confident wrong answer at exactly the wrong moment. The page
says which of the two it used, per order, rather than hiding the
difference.

**A missing vendor price is reported, not smoothed over.** A purchase
order at zero produces 100% margin, which is arithmetically correct and
commercially nonsense; the page says the cost is missing and names what
to fill in. The alternative -- suppressing the number until somebody
types a price -- would hide the one case where the read is most needed.

The object also returns the raw figures alongside its HTML (`margin`,
with revenue_minor / cost_minor / gross_minor / gross_pct). The HTTP
layer reads `body` and ignores the rest, so this costs a browser nothing
and gives anything calling the object in process the numbers without
parsing a page for them.

Requires a signed-in identity, the same gate and the same shape as
site_pick_list: a sign-in prompt rather than a 403, and margin is a
number about somebody's business that no visitor is entitled to.
"""

import html
import os

import object_billing
import object_records


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _esc(value):
    return html.escape(_text(value))


def _cents(value):
    try:
        return int(_text(value) or "0")
    except ValueError:
        return 0


def _money(minor, currency="USD"):
    sign = "-" if minor < 0 else ""
    return f"{sign}{abs(minor) // 100}.{abs(minor) % 100:02d} {currency}"


_STYLE = """
.margin-table { width: 100%; border-collapse: collapse; margin: 0.5rem 0 1.5rem; }
.margin-table th, .margin-table td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--line, #38384a); }
.margin-table td.num, .margin-table th.num { text-align: right; }
.hint { color: var(--muted, #999); font-size: 0.9rem; }
.warn { color: var(--warn, #d08a2a); }
"""


def _totals(base, order):
    """(amount_minor, source) for one order.

    The stamped total first -- it is the document's own number -- and the
    lines folded only when it is zero, because the stamp is a reaction
    that may not have landed yet.
    """
    stamped = _cents(order.get("total_cents"))
    if stamped:
        return stamped, "stamped"
    try:
        lines = object_records.read_collection_records("order_lines",
                                                       base_dir=base)
    except Exception:
        return 0, "unknown"
    folded = 0
    for line in lines:
        if _text(line.get("order_id")) != order["id"]:
            continue
        folded += _cents(line.get("line_total_cents"))
        folded += _cents(line.get("line_tax_cents"))
    return folded, "folded" if folded else "empty"


def _page(title, body, user_id, path):
    who = (f"signed in as <strong>{_esc(user_id)}</strong>" if user_id
           else f'<a href="/login?next={_esc(path)}">sign in</a>')
    return f"""<!doctype html>
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
<header class="app"><h1><a href="/">DBBASIC</a></h1><div class="who">{who}</div></header>
{body}
</div>
<script src="/nav"></script>
</body>
</html>"""


def GET(request):
    base = _base_dir()
    identity = request.get("_identity") or {}
    user_id = _text(identity.get("user_id"))
    order_id = _text(request.get("order_id"))
    path = f"/orders/{order_id}/margin"

    if not user_id:
        return {"content_type": "text/html; charset=utf-8",
                "body": _page("Margin",
                              '<p class="hint">Margin is a number about '
                              'somebody\'s business. Please sign in.</p>',
                              user_id, path)}

    try:
        order = object_records.get_collection_record("orders", order_id,
                                                     base_dir=base)
    except Exception:
        return {"status": 404, "content_type": "text/html; charset=utf-8",
                "body": _page("Margin",
                              '<p class="hint">No such order.</p>',
                              user_id, path)}

    linked_id = _text(order.get("linked_order_id"))
    linked = {}
    if linked_id:
        try:
            linked = object_records.get_collection_record("orders", linked_id,
                                                          base_dir=base)
        except Exception:
            linked = {}

    if not linked:
        body = (f'<div class="pagehead"><h1>Margin</h1></div>'
                f'<p class="hint">Order {_esc(order.get("number"))} is not '
                f'part of a drop-ship pair, so there is no vendor cost to '
                f'set against it. Margin here is the difference between what '
                f'the customer paid on one order and what the vendor charged '
                f'on the other; an order fulfilled from our own shelf needs '
                f'inventory valuation instead, which this server does not do '
                f'yet (see tests/test_cogs_on_sale.py).</p>')
        return {"status": 409, "content_type": "text/html; charset=utf-8",
                "body": _page("Margin", body, user_id, path)}

    # Which of the two is the sale is a question of doc_type, never of
    # which one the URL happened to name.
    if _text(order.get("doc_type")) == "purchase":
        sale, purchase = linked, order
    else:
        sale, purchase = order, linked

    currency = _text(sale.get("currency")) or "USD"
    revenue, revenue_source = _totals(base, sale)
    cost, cost_source = _totals(base, purchase)
    figures = object_billing.margin(revenue, cost)

    pct = ("n/a" if figures["gross_pct"] is None
           else f"{figures['gross_pct']}%")
    missing = ""
    if cost <= 0:
        missing = ('<p class="hint warn">The purchase order carries no cost, '
                   'so this reads as pure profit and is not. Put the vendor\'s '
                   'price on the purchase order lines (or record cost_cents '
                   'on the products) and this page will be right.</p>')

    body = f"""
<div class="breadcrumb noprint"><a href="/">Home</a> / <a href="/orders">Orders</a> / Margin</div>
<div class="pagehead"><h1>Margin on {_esc(sale.get('number'))}</h1></div>
<p class="hint">Drop-shipped by {_esc(purchase.get('customer_name'))} on
purchase order {_esc(purchase.get('number'))}. This is a read of the two
orders, never a stored figure.</p>
<table class="margin-table">
<thead><tr><th>Line</th><th>Document</th><th class="num">Amount</th><th>Source</th></tr></thead>
<tbody>
<tr><td>Revenue</td><td>{_esc(sale.get('number'))}</td><td class="num">{_esc(_money(revenue, currency))}</td><td>{_esc(revenue_source)}</td></tr>
<tr><td>Cost</td><td>{_esc(purchase.get('number'))}</td><td class="num">{_esc(_money(cost, currency))}</td><td>{_esc(cost_source)}</td></tr>
<tr><td><strong>Gross</strong></td><td></td><td class="num"><strong>{_esc(_money(figures['gross_minor'], currency))}</strong></td><td>{_esc(pct)}</td></tr>
</tbody>
</table>
{missing}
"""
    return {"content_type": "text/html; charset=utf-8",
            "body": _page(f"Margin on {_text(sale.get('number'))}", body,
                          user_id, path),
            "margin": figures,
            "currency": currency,
            "sale_order_id": sale["id"],
            "purchase_order_id": purchase["id"],
            "revenue_source": revenue_source,
            "cost_source": cost_source}
