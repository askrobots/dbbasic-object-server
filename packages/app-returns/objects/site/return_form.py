"""site_return_form -- the page where a return starts. GET/POST
/returns/{order_id}, with `?token=` for the customer who has no account.

**Two ways in, one of them new.** This page used to demand a signed-in
session, and said so as an honest half-measure: the customer-facing door
it wanted was the invoice portal's shape -- a capability URL, an
unguessable bearer token on the record, no sign-up wall between a willing
customer and the thing they are trying to do -- and that needed a token
FIELD on the order, which belongs to app-orders and not to this package.
The wait is over: orders.portal_token exists (schema v5, minted by
app-shop's action_checkout and by system_order_portal_link) and this page
now accepts it as `?token=`, matched against THIS order with
hmac.compare_digest.

That is not a nicety, it is the difference between having a returns flow
and not having one. A guest checkout is the default sale in this shop, so
every buyer who ever wanted to send something back was being shown a
sign-in prompt for an account they were never offered. site_order_status
puts the link in front of them, already carrying the token.

The signed-in path is untouched and still works: staff raise returns on
the phone the way they always have, and an order's owner never needs a
token to see their own order. What is refused is what was always refused
-- a bare order id with neither. An order id is not a secret good enough
to protect a customer's name and address, and the token check is
constant-time for the same reason the invoice portal's is: a plain `==`
on attacker-controlled input leaks how many leading characters matched.
A wrong token gets the same sign-in prompt as no token at all, never a
"that token is wrong" that tells somebody probing they are close.

A READ, not a table. What can be returned is folded live -- shipped
quantities minus what is already claimed by an inbound shipment -- so the
page cannot show a mug that went back last week. The same fold
action_authorize_return does its arithmetic with, deliberately a second
copy of a few lines rather than a shared helper: the action must be
correct with no page in front of it, and a page that trusted the action to
recheck would still have to display SOMETHING before the action ran.

The form POSTs to itself and hands everything to action_authorize_return,
which owns every refusal. This page decides nothing about what is
returnable; it just asks. A refusal comes back as the action's own words,
because the numbers in "shipped 3, already authorized 1" are the whole
value of the message and rewording them here would guarantee two
vocabularies for one rule.

**The return label is named, not faked.** Carrier labels are a connector
boundary (plan/fulfillment-logistics-spec.md's label section: BYO
credentials, manual-first), and that connector is a later slice. So the
page says what will appear here and what has to be configured for it to,
rather than showing a button that does nothing. A dead button costs
somebody a click, then their trust in every other button on the page.
"""

import hmac
import html
import os
from decimal import Decimal, InvalidOperation

import object_execution
import object_records
import python_object_runtime

ACTOR = "site_return_form"

# The goods reached the customer, so there is something to send back.
SHIPPED_ONWARD = {"shipped", "in_transit", "delivered"}

# An inbound shipment in one of these has released its claim on the units.
RELEASED_INBOUND_STATUSES = {"expired"}

RETURNABLE_ORDER_STATUSES = {"shipped", "delivered", "partial"}

REASONS = (
    ("damaged", "Arrived damaged"),
    ("wrong_item", "Wrong item sent"),
    ("not_as_described", "Not as described"),
    ("no_longer_wanted", "No longer wanted"),
    ("arrived_late", "Arrived too late"),
    ("other", "Something else"),
)


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
.wrap { max-width: 760px; margin: 0 auto; padding: 2rem 1.25rem; }
.pagehead { margin-bottom: 1rem; }
.pagehead h1 { margin: 0 0 0.25rem; font-size: 1.4rem; }
.hint { color: var(--muted, #999); font-size: 0.9rem; }
.notice { border: 1px solid var(--line, #333); border-radius: 8px; padding: 0.8rem 1rem; margin: 1rem 0; }
.rma { border: 1px solid var(--line, #333); border-radius: 8px; padding: 0.9rem 1.1rem; margin: 1rem 0; }
table.lines { width: 100%; border-collapse: collapse; margin: 1rem 0 1.25rem; }
table.lines th, table.lines td { text-align: left; padding: 0.45rem 0.6rem; border-bottom: 1px solid var(--line, #38384a); }
table.lines td.num, table.lines th.num { text-align: right; }
input[type=number] { width: 5rem; }
label.block { display: block; margin: 0.75rem 0 0.25rem; }
textarea { width: 100%; min-height: 4rem; }
.notfound { text-align: center; padding: 3rem 1rem; }
@media print {
  nav, header.app, .noprint, .btn, form { display: none !important; }
  .wrap { max-width: none; padding: 0; }
  body { background: #fff; color: #000; }
}
"""


def _page(body, *, title, status=200):
    page = {
        "content_type": "text/html; charset=utf-8",
        "body": f"""<!doctype html>
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
{body}
</div>
<script src="/nav"></script>
</body>
</html>""",
    }
    if status != 200:
        page["status"] = status
    return page


def _not_found(message):
    return _page(
        f'<div class="notfound"><h1>Not found</h1><p class="hint">{message}</p>'
        '</div>',
        title="Return -- not found", status=404)


def _call(object_id, payload, *, method="POST"):
    try:
        runtime = python_object_runtime.PythonObjectRuntime()
        outcome = object_execution.execute_object(
            runtime,
            object_execution.ObjectExecutionRequest(
                object_id, method=method, payload=payload))
    except Exception as exc:
        return {"error": str(exc)[:200]}
    if not outcome.ok:
        message = getattr(outcome.error, "message", "") if outcome.error else ""
        return {"error": _text(message)[:200] or f"{object_id} failed"}
    return outcome.result if isinstance(outcome.result, dict) else {}


def _returnable(base, order_id, order_lines):
    """Shipped minus already claimed, per order line -- the same fold
    action_authorize_return gates on."""
    try:
        shipments = object_records.read_collection_records("shipments",
                                                           base_dir=base)
    except Exception:
        shipments = []
    outbound = {row["id"] for row in shipments
                if _text(row.get("order_id")) == order_id
                and _text(row.get("direction")) != "inbound"
                and _text(row.get("status")) in SHIPPED_ONWARD}
    inbound = {row["id"] for row in shipments
               if _text(row.get("order_id")) == order_id
               and _text(row.get("direction")) == "inbound"
               and _text(row.get("status")) not in RELEASED_INBOUND_STATUSES}
    try:
        lines = object_records.read_collection_records("shipment_lines",
                                                       base_dir=base)
    except Exception:
        lines = []

    shipped, claimed = {}, {}
    for line in lines:
        parent = _text(line.get("shipment_id"))
        key = _text(line.get("order_line_id"))
        amount = _quantity(line.get("quantity"))
        if parent in outbound:
            shipped[key] = shipped.get(key, Decimal(0)) + amount
        elif parent in inbound:
            claimed[key] = claimed.get(key, Decimal(0)) + amount

    return {line["id"]: (shipped.get(line["id"], Decimal(0))
                         - claimed.get(line["id"], Decimal(0)))
            for line in order_lines}


def _returns_for(base, order_id):
    try:
        rows = object_records.read_collection_records("return_authorizations",
                                                      base_dir=base)
    except Exception:
        return []
    return [row for row in rows if _text(row.get("order_id")) == order_id]


def _lines_html(order_lines, returnable):
    rows = []
    for line in order_lines:
        left = returnable.get(line["id"], Decimal(0))
        if left <= 0:
            continue
        rows.append(
            "<tr>"
            f"<td>{_esc(line.get('description')) or _esc(line.get('product_id'))}</td>"
            f"<td class=\"num\">{_esc(_number(left))}</td>"
            f"<td class=\"num\"><input type=\"number\" min=\"0\" step=\"1\" "
            f"max=\"{_esc(_number(left))}\" name=\"qty_{_esc(line['id'])}\" "
            f"value=\"0\"></td>"
            "</tr>")
    if not rows:
        return ""
    return f"""
<table class="lines">
<thead><tr><th>Item</th><th class="num">You can return</th>
<th class="num">Send back</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>"""


def _reasons_html():
    options = "".join(f'<option value="{_esc(value)}">{_esc(label)}</option>'
                      for value, label in REASONS)
    return f"""
<label class="block" for="reason">Why are you sending it back?</label>
<select id="reason" name="reason">{options}</select>
<label class="block" for="reason_note">Anything else we should know?</label>
<textarea id="reason_note" name="reason_note"></textarea>"""


def _returns_html(returns):
    if not returns:
        return ""
    blocks = []
    for rma in sorted(returns, key=lambda row: _text(row.get("authorized_at"))):
        label = (
            '<p class="hint">A printable return label will appear here once a '
            'carrier connector is configured (carrier credentials are a later '
            'slice -- until then, post it to the address on your invoice and '
            'write this return number on the outside).</p>')
        refund = _text(rma.get("refund_ref"))
        blocks.append(f"""
<div class="rma">
<strong>Return {_esc(rma.get('id'))}</strong> &middot; {_esc(rma.get('status'))}
&middot; {_esc(rma.get('reason'))}
<p class="hint">Authorized {_esc(rma.get('authorized_at')) or 'recently'}
{('&middot; send it back by ' + _esc(rma.get('expires_on'))) if _text(rma.get('expires_on')) else ''}
{('&middot; refunded (' + _esc(refund) + ')') if refund else ''}</p>
{label}
</div>""")
    return "<h2>Returns on this order</h2>" + "".join(blocks)


def _token_matches(order, token):
    """Does this token open THIS order?

    Constant-time, and scoped to the one order the URL already named --
    there is deliberately no scan across orders here. site_order_status
    resolves a bare token to an order because that is its whole job; this
    page was handed an order id by its route, so the only question worth
    asking is whether the bearer holds that order's token. A blank stored
    token matches nothing, or an order that never had a link minted would
    be an open door to anybody submitting `?token=`.
    """
    token = _text(token)
    stored = _text(order.get("portal_token"))
    if not token or not stored:
        return False
    return hmac.compare_digest(stored, token)


def _render(base, order, notice="", token=""):
    order_id = order["id"]
    try:
        order_lines = [row for row in object_records.read_collection_records(
            "order_lines", base_dir=base)
            if _text(row.get("order_id")) == order_id]
    except Exception:
        order_lines = []
    order_lines.sort(key=lambda row: _text(row.get("description")))
    returnable = _returnable(base, order_id, order_lines)
    table = _lines_html(order_lines, returnable)
    returns = _returns_for(base, order_id)

    number = _text(order.get("number")) or order_id
    notice_html = f'<div class="notice">{notice}</div>' if notice else ""

    if _text(order.get("status")) not in RETURNABLE_ORDER_STATUSES:
        form = ('<p class="hint">Nothing on this order has shipped yet, so '
                'there is nothing to send back. An order that has not left the '
                'building is changed or cancelled, not returned.</p>')
    elif not table:
        form = ('<p class="hint">Everything that shipped on this order is '
                'already covered by a return. Nothing left to send back.</p>')
    else:
        # The token rides along in a hidden field so the POST is
        # authenticated the same way the GET was. Without it a guest could
        # read the form and then be bounced by their own submit button --
        # the most infuriating shape a web form can take.
        carry = (f'<input type="hidden" name="token" value="{_esc(token)}">'
                 if token else "")
        form = f"""
<form method="post" action="/returns/{_esc(order_id)}">
{carry}
{table}
{_reasons_html()}
<p><button type="submit" name="do" value="request">Request a return</button></p>
</form>"""

    body = f"""
<div class="breadcrumb noprint"><a href="/">Home</a> / Returns</div>
<div class="pagehead">
<h1>Return something from order {_esc(number)}</h1>
<p class="hint">{_esc(order.get('customer_name'))}
&middot; {_esc(order.get('status'))}</p>
</div>
{notice_html}
{form}
{_returns_html(returns)}
"""
    return _page(body, title=f"Return -- {number}")


def _notice(result):
    """The action's own words, never a reworded copy: the numbers in
    'shipped 3, already authorized 1, would make 4' are the whole value of
    the message."""
    if not isinstance(result, dict):
        return "Something went wrong raising that return."
    if result.get("ok"):
        return (f"Return {_esc(result.get('return_id'))} authorized. Send the "
                f"goods back by {_esc(result.get('expires_on'))}.")
    parts = [_esc(result.get("error")) or "That return was refused."]
    for entry in result.get("over_return") or []:
        parts.append(
            f"{_esc(entry.get('description'))}: shipped "
            f"{_esc(entry.get('shipped'))}, already authorized "
            f"{_esc(entry.get('already_authorized'))}, asked for "
            f"{_esc(entry.get('asked_for'))}, would make "
            f"{_esc(entry.get('would_make'))}.")
    if _text(result.get("missing_reason")):
        parts.append(_esc(result.get("missing_reason")))
    for entry in result.get("bad_quantities") or []:
        parts.append(_esc(entry.get("reason")))
    return "<br>".join(parts)


def _order_for(base, order_id):
    if not order_id:
        return None
    try:
        return object_records.get_collection_record("orders", order_id,
                                                    base_dir=base)
    except Exception:
        return None


def _no_way_in(order_id):
    """One page for every way of not being allowed in: no session and no
    token, a wrong token, a token for a different order, or an order that
    does not exist at all. Distinguishing them would tell somebody probing
    with a guessed order id that they had guessed a real one -- which is
    precisely the leak the token exists to close.
    """
    return _page(
        '<div class="pagehead"><h1>Returns</h1></div>'
        '<p class="hint">To start a return, open the tracking link from your '
        'order confirmation email and follow "Start a return" -- it carries '
        'the key to this page. If you have an account with the shop, you can '
        f'<a href="/login?next=/returns/{_esc(order_id)}">sign in</a> '
        'instead.</p>',
        title="Returns")


def _way_in(base, request):
    """Resolve (order, token, refusal) for whoever is asking.

    Two accepted credentials, checked in this order because the cheap one
    is free: a signed-in session (staff raising a return at the counter,
    an owner looking at their own order), or the order's own portal_token
    (the guest who was never offered an account and holds the link the
    confirmation email gave them).

    The two refusals differ ON PURPOSE. A signed-in operator who mistypes
    an order id is told plainly that no such order exists, because they
    are already inside and a vague page would just waste their afternoon.
    An anonymous visitor is told nothing that distinguishes a wrong token
    from a wrong order id -- otherwise the 404 itself becomes an oracle
    confirming which guessed ids are real orders, and the token would be
    guarding a door with a window next to it.
    """
    order_id = _text(request.get("order_id"))
    identity = request.get("_identity") or {}
    signed_in = bool(_text(identity.get("user_id")))
    order = _order_for(base, order_id) if order_id else None

    if signed_in:
        if order is None:
            return None, "", _not_found(
                "There is no order with that id. It may have been mistyped.")
        return order, "", None

    token = _text(request.get("token"))
    if order is not None and _token_matches(order, token):
        return order, token, None
    return None, "", _no_way_in(order_id)


def GET(request):
    base = _base_dir()
    order, token, refusal = _way_in(base, request)
    if refusal is not None:
        return refusal
    return _render(base, order, token=token)


def POST(request):
    base = _base_dir()
    order, token, refusal = _way_in(base, request)
    if refusal is not None:
        return refusal
    order_id = order["id"]
    identity = request.get("_identity") or {}

    form = request.get("_form") or request
    lines = []
    for key, value in form.items():
        if not str(key).startswith("qty_"):
            continue
        quantity = _quantity(value)
        if quantity > 0:
            lines.append({"order_line_id": str(key)[4:],
                          "quantity": _number(quantity)})

    result = _call("action_authorize_return", {
        "order_id": order_id,
        "lines": lines,
        "reason": form.get("reason"),
        "reason_note": form.get("reason_note"),
        "_identity": identity})
    return _render(base, order, notice=_notice(result), token=token)
