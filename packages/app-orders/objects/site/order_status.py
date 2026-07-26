"""site_order_status -- "where is my order?", answered without an account.
GET /orders/track/{token}.

The customer is NOT a user of this system. This shop's default sale is a
guest checkout: an email address, a card, and no identity afterwards. So
until now a shopper who paid heard nothing and had nowhere to look --
every fact about their own purchase lived behind a sign-in wall they had
no key to. This page is that key, and it is the same shape app-invoices
already proved for /pay/{token}: a CAPABILITY URL, an unguessable bearer
token on the order (orders.portal_token, minted by action_checkout and by
system_order_portal_link), granting exactly "see this one order."

Looked up BY TOKEN ONLY -- never by id. Order ids are UUIDv4 and appear in
record_changes, in the owner-scoped /collections/orders/records/{id} path,
in shipment rows and in correlation ids threaded through logs: surfaces an
operator reasonably expects to stay internal, not things a stranger's
browser history holds. Keeping the token a separate field means there is
no enumeration path from a guessed id to somebody's name, address and
purchase history -- which is exactly the objection site_return_form raised
when it had to fall back to a sign-in prompt.

hmac.compare_digest for the match: a plain `==` on attacker-controlled
input leaks how many leading characters matched via how long the compare
took, which is the side channel a bearer-token scheme exists to close. A
blank, unknown or mistyped token renders the SAME friendly "not found"
page at 404 -- never a 403, never a traceback, and never a hint that some
other token would have worked. A 403 would confirm the token namespace is
a thing worth attacking; "not found" makes a guess and a typo
indistinguishable.

**It shows prices, unlike the packing slip.** site_packing_slip carries no
money by construction, because it goes in the box and the box may be a
present. This page is the opposite case: it is the buyer's own receipt,
read by the person who paid, and a receipt with the amounts filed off is
not a receipt.

**The status is translated, not echoed.** The orders enum is warehouse
vocabulary -- `partial` is a fact about which shipment lines have left,
`processing` is a bookkeeping hop, `received` means goods arrived HERE on
a purchase order. None of that is a sentence to show a person waiting for
a parcel, and "partial" in particular reads to a customer as "part of my
money went missing". So there is one mapping, in one place, from the
internal word to the customer's word, and an unmapped status falls back to
a plainly honest "Being prepared" rather than leaking the raw enum.

No nav, no global search, no site chrome beyond the base stylesheet: this
page is handed to a stranger's inbox and must never be one click from
somebody else's order or from the sign-in wall of a system they have no
account on. The two links it DOES offer both stay inside this one sale --
the invoice's own pay door, and a return carrying this same token.
"""
from __future__ import annotations

import hmac
import html
import os

import object_money
import object_records

DATA_DIR_ENV = "DBBASIC_DATA_DIR"
ACTOR = "site_order_status"

# The one mapping from warehouse vocabulary to the words a customer
# understands. `processing` and `partial` share a bucket on purpose:
# "some of it has left the building" is an internal distinction about
# shipment lines, and to somebody waiting at home both mean the same thing
# -- it is being dealt with. `received` is the PURCHASE side's terminal
# state and can never appear on an order a customer is tracking, so it is
# deliberately absent rather than mistranslated.
CUSTOMER_STATUS = {
    "confirmed": "Order received",
    "partial": "Being prepared",
    "processing": "Being prepared",
    "shipped": "On its way",
    "delivered": "Delivered",
    "cancelled": "Cancelled",
}

# What each of those means in a sentence, because a two-word badge answers
# "what" and never "so what".
STATUS_BLURB = {
    "Order received": "We have your order and are getting it ready.",
    "Being prepared": "Your order is being picked and packed.",
    "On its way": "Your order has left us and is with the carrier.",
    "Delivered": "This order has been delivered.",
    "Cancelled": "This order was cancelled. If that is a surprise, "
                 "contact the shop.",
}

# The customer's word for anything the mapping does not cover -- a draft
# that somehow acquired a token, or a status added to the enum after this
# page was written. Never the raw enum value: an unmapped internal word on
# a customer's screen is a bug that reads as a system talking to itself.
FALLBACK_STATUS = "Being prepared"

# Goods have reached the customer, so there is something to send back.
RETURNABLE = {"shipped", "delivered", "partial"}

# An invoice in one of these still wants money. `void` does not -- a
# cancelled bill must never be offered a pay button.
UNPAID_INVOICE_STATUSES = {"draft", "sent", "partial", "overdue"}


def _base_dir():
    return os.environ.get(DATA_DIR_ENV, "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _esc(value):
    return html.escape(_text(value))


def _find_order_by_token(base, token):
    """The only lookup this page performs: a full scan matched with a
    constant-time comparison, never a keyed lookup by id.

    A blank token matches nothing -- without this guard an order that has
    never had a token minted (stored as "") would "match" a blank incoming
    token, which turns "no link generated yet" into an accidental open
    door onto whichever unlinked order happens to be first in the file.
    """
    token = _text(token)
    if not token:
        return None
    try:
        rows = object_records.read_collection_records("orders", base_dir=base)
    except Exception:
        return None
    for row in rows:
        candidate = _text(row.get("portal_token"))
        if not candidate:
            continue
        if hmac.compare_digest(candidate, token):
            return row
    return None


def _lines_for(base, order_id):
    """The order's lines, or none at all if order_lines is somehow absent.
    A receipt with no itemisation is thin; a 500 on the page a customer is
    trying to read is worse.
    """
    try:
        rows = object_records.read_collection_records("order_lines",
                                                      base_dir=base)
    except Exception:
        return []
    lines = [row for row in rows if _text(row.get("order_id")) == order_id]
    lines.sort(key=lambda row: _text(row.get("description")))
    return lines


def _shipments_for(base, order_id):
    """Outbound parcels for this order, newest-looking last.

    Wrapped because app-shipping is a separate package and app-orders does
    not depend on it: a shop that never installed shipping still has
    orders, and its customers still deserve this page.
    """
    try:
        rows = object_records.read_collection_records("shipments",
                                                      base_dir=base)
    except Exception:
        return []
    return [row for row in rows
            if _text(row.get("order_id")) == order_id
            and _text(row.get("direction")) != "inbound"]


def _invoice_for(base, order):
    """The bill for this order, when there is one and it can be found.

    Same posture as the shipments read: app-invoices is another package,
    and an order with no findable invoice is an order with no pay link,
    not an error page.
    """
    invoice_id = _text(order.get("invoice_id"))
    if not invoice_id:
        return None
    try:
        return object_records.get_collection_record("invoices", invoice_id,
                                                    base_dir=base)
    except Exception:
        return None


def _setting(base, key, default=""):
    """Duplicated, on purpose, from every other package that reads
    app_settings: there is no shared settings module in this codebase yet
    and inventing one for an nth copy is the layer docs/logic-decisions.md
    #4 says to wait on.
    """
    try:
        for row in object_records.read_collection_records("app_settings",
                                                          base_dir=base):
            if row.get("key") == key and _text(row.get("value")):
                return _text(row["value"])
    except Exception:
        pass
    return default


_STYLE = """
.wrap { max-width: 720px; margin: 0 auto; padding: 2rem 1.25rem; }
.pagehead { margin-bottom: 1.25rem; }
.pagehead h1 { margin: 0 0 0.25rem; font-size: 1.4rem; }
.hint { color: var(--muted, #999); font-size: 0.9rem; }
.status { border: 1px solid var(--line, #333); border-radius: 8px; padding: 0.9rem 1.1rem; margin: 1.25rem 0; }
.status .n { font-size: 1.25rem; font-weight: 700; }
table.lines { width: 100%; border-collapse: collapse; margin: 1rem 0 1.5rem; }
table.lines th, table.lines td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--line, #38384a); }
table.lines td.num, table.lines th.num { text-align: right; }
table.lines tr.total td { font-weight: 700; border-bottom: none; }
.parcel { border: 1px solid var(--line, #333); border-radius: 8px; padding: 0.8rem 1rem; margin: 0.75rem 0; }
.doors { margin: 1.5rem 0; }
.doors a { display: inline-block; margin-right: 1rem; }
.notfound { text-align: center; padding: 3rem 1rem; }
"""


def _page(body, *, title, status=200):
    """Deliberately bare: no /nav script, no global search mount, no
    breadcrumb back into the app. This page is handed to a stranger's
    inbox; it must not be one click from anything but this one order.
    """
    page = {
        "content_type": "text/html; charset=utf-8",
        "body": f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{_esc(title)}</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
{body}
</div>
</body>
</html>""",
    }
    if status != 200:
        page["status"] = status
    return page


def _not_found():
    """One page for every failure: blank token, mistyped token, token for
    an order that was purged. Saying which would tell somebody probing
    that they were close.
    """
    return _page(
        '<div class="notfound"><h1>Not found</h1>'
        '<p class="hint">This tracking link is not valid. It may have been '
        'mistyped, or copied without its last few characters. Check the link '
        'in your confirmation email, or contact the shop and they can send '
        'you a fresh one.</p></div>',
        title="Not found", status=404)


def _business_html(base):
    """Whatever the operator configured under business.name, and nothing
    invented when they configured nothing: an unbranded receipt is honest,
    a fabricated shop name is not.
    """
    name = _setting(base, "business.name")
    return f'<div class="hint">{_esc(name)}</div>' if name else ""


def _status_html(order):
    raw = _text(order.get("status"))
    word = CUSTOMER_STATUS.get(raw, FALLBACK_STATUS)
    blurb = STATUS_BLURB.get(word, "")
    return (f'<div class="status"><div class="n">{_esc(word)}</div>'
            + (f'<div class="hint">{_esc(blurb)}</div>' if blurb else "")
            + "</div>")


def _lines_html(order, lines, base):
    """The receipt itself. Prices included -- see the module docstring."""
    currency = _text(order.get("currency")) or "USD"
    total = object_money.format_amount(order.get("total_cents") or 0, currency,
                                       base_dir=base)
    if not lines:
        # An order whose lines cannot be read still owes the customer the
        # one number they care about. A "What you ordered" heading over an
        # empty space reads as "we have lost your order".
        return (f'<p>Total paid: <strong>{_esc(total)}</strong></p>'
                '<p class="hint">The itemised list is not available here. '
                'Your confirmation email has it.</p>')
    rows = []
    for line in lines:
        rows.append(
            "<tr>"
            f"<td>{_esc(line.get('description')) or _esc(line.get('product_id'))}</td>"
            f"<td class=\"num\">{_esc(line.get('quantity')) or '1'}</td>"
            f"<td class=\"num\">{_esc(object_money.format_amount(line.get('unit_price_cents') or 0, currency, base_dir=base))}</td>"
            f"<td class=\"num\">{_esc(object_money.format_amount(line.get('line_total_cents') or 0, currency, base_dir=base))}</td>"
            "</tr>")
    # The GRAND total, which is what the buyer paid: postage and tax are in
    # it. Showing the goods subtotal under the word "Total" is how a
    # receipt ends up disagreeing with a card statement.
    rows.append('<tr class="total"><td colspan="3">Total</td>'
                f'<td class="num">{_esc(total)}</td></tr>')
    return f"""
<table class="lines">
<thead><tr><th>Item</th><th class="num">Qty</th>
<th class="num">Price</th><th class="num">Amount</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>"""


def _parcels_html(shipments):
    """Carrier and tracking number when the shipment carries them, and
    silence when it does not. A "Tracking: (none)" row tells a worried
    customer nothing except that the shop has a field for it.
    """
    blocks = []
    for shipment in shipments:
        carrier = _text(shipment.get("carrier"))
        tracking = _text(shipment.get("tracking_number"))
        if not carrier and not tracking:
            continue
        parts = []
        if carrier:
            parts.append(f"<strong>{_esc(carrier)}</strong>")
        if tracking:
            parts.append(f"tracking number {_esc(tracking)}")
        shipped_on = _text(shipment.get("shipped_on"))
        if shipped_on:
            parts.append(f"sent {_esc(shipped_on)}")
        blocks.append(f'<div class="parcel">{" &middot; ".join(parts)}</div>')
    if not blocks:
        return ""
    return "<h2>Your parcel</h2>" + "".join(blocks)


def _doors_html(order, invoice, token):
    """The two links that stay inside this one sale.

    The pay link only appears while money is actually owed: offering "pay
    now" on an order already paid for is how a shop gets paid twice and
    spends a fortnight refunding it. The return link only appears once
    something has physically left, because an order still in the building
    is changed or cancelled, not returned -- and it carries THIS token, so
    the guest who never made an account can actually use it.
    """
    doors = []
    if invoice is not None:
        pay_token = _text(invoice.get("portal_token"))
        status = _text(invoice.get("status"))
        if pay_token and status in UNPAID_INVOICE_STATUSES:
            doors.append(f'<a href="/pay/{_esc(pay_token)}">Pay for this order</a>')
    if _text(order.get("status")) in RETURNABLE:
        doors.append(f'<a href="/returns/{_esc(order["id"])}?token={_esc(token)}">'
                     "Start a return</a>")
    if not doors:
        return ""
    return f'<div class="doors">{"".join(doors)}</div>'


def GET(request):
    base = _base_dir()
    token = _text(request.get("token"))
    order = _find_order_by_token(base, token)
    if order is None:
        return _not_found()

    number = _text(order.get("number")) or order["id"]
    lines = _lines_for(base, order["id"])
    shipments = _shipments_for(base, order["id"])
    invoice = _invoice_for(base, order)

    ordered_on = _text(order.get("order_date"))
    body = f"""
<div class="pagehead">
{_business_html(base)}
<h1>Order {_esc(number)}</h1>
<p class="hint">{('Placed ' + _esc(ordered_on)) if ordered_on else 'Thank you for your order'}</p>
</div>
{_status_html(order)}
{_parcels_html(shipments)}
<h2>What you ordered</h2>
{_lines_html(order, lines, base)}
{_doors_html(order, invoice, token)}
<p class="hint">Keep this link -- it is the only way back to this page, and
it works for nobody but whoever holds it.</p>
"""
    return _page(body, title=f"Order {number}")
