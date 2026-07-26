"""site_kitchen_ticket -- one order, printed for the person making it.
GET /kitchen/{order_id}/ticket.

NO PRICES. Not "prices in small type", not "prices unless somebody ticks
something": none, ever, for the same reason site_packing_slip carries
none. A ticket is not an invoice. It exists so the cook can make the
right thing, and what it cost is a conversation that already happened
with somebody else -- usually a card reader, sometimes a different person
entirely. A cook who can see the money on every ticket is being handed
information they cannot act on, on the one piece of paper that has to be
readable in three seconds with their hands full.

**The lines and their notes, and almost nothing else.** The packing slip
already prints `customer_note` -- what the shopper needs the PACKER to
know. This prints `line_note`, per line, which is where a cook actually
reads it: "no onions" under the burger it is true of, not in a paragraph
at the bottom of the order that has to be matched back up by hand.
`customer_note` is printed too, once, at the top, because "I am
allergic" is addressed to whoever is making the food and it is worth
having on the paper in the kitchen rather than only on the one in
dispatch.

Printable rather than pretty, and BIG: minimal print CSS, nav hidden at
@media print, and type sized for a metre away. Operator-facing, behind
the ordinary registered permission like the packing slip and for the same
reason -- the order id is an internal record id, so a public page here
would enumerate every order in the shop.

The promised time is on it. A ticket that says what to make and not when
it is due is a ticket that gets done in the order the pile happens to sit
in, which is exactly the failure the queue page exists to prevent.

An unknown order is a friendly 404 in the same shell, never a traceback:
a mistyped link is somebody who is still standing in a kitchen trying to
make food.
"""

import html
import os
from datetime import datetime

import object_records

ACTOR = "site_kitchen_ticket"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _esc(value):
    return html.escape(_text(value))


def _esc_multiline(value):
    return _esc(value).replace("\n", "<br>")


def _parse_when(value):
    """Same reading as site_kitchen's and system_pickup_attention's: ISO
    in, NAIVE LOCAL wall-clock out, None when it will not parse.
    Duplicated rather than shared because objects on this platform are
    executed, not imported, and ten lines is not worth a module boundary
    (docs/logic-decisions.md #4)."""
    raw = _text(value)
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is not None:
        when = when.astimezone().replace(tzinfo=None)
    return when


_STYLE = """
.wrap { max-width: 640px; margin: 0 auto; padding: 2rem 1.25rem; }
.pagehead { margin-bottom: 1rem; }
.pagehead h1 { margin: 0 0 0.25rem; font-size: 1.6rem; }
.promised { font-size: 2.4rem; font-weight: 700; line-height: 1.1; margin: 0.2rem 0; }
.who { font-size: 1.3rem; font-weight: 600; }
.hint { color: var(--muted, #999); font-size: 0.9rem; }
.note { border: 1px solid var(--line, #333); border-radius: 8px; padding: 0.8rem 1rem; margin: 1rem 0; white-space: pre-wrap; font-size: 1.1rem; }
ul.klines { list-style: none; margin: 1.2rem 0; padding: 0; font-size: 1.35rem; }
ul.klines li { padding: 0.5rem 0; border-top: 2px solid var(--line, #38384a); }
ul.klines .qty { font-weight: 700; display: inline-block; min-width: 2.6rem; }
ul.klines .line-note { display: block; margin-left: 2.6rem; font-style: italic; font-weight: 600; font-size: 1.15rem; }
.notfound { text-align: center; padding: 3rem 1rem; }
@media print {
  nav, header.app, .noprint, .btn { display: none !important; }
  .wrap { max-width: none; padding: 0; }
  body { background: #fff; color: #000; }
  .note { border-color: #999; }
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


def _not_found():
    return _page(
        '<div class="notfound"><h1>Not found</h1>'
        '<p class="hint">There is no order with that id. It may have been '
        'mistyped, or the order may have been deleted. '
        '<a href="/kitchen">Back to the kitchen</a>.</p></div>',
        title="Kitchen ticket -- not found", status=404)


def _lines_html(lines):
    if not lines:
        return ('<p class="hint">This order has no lines -- there is nothing '
                'on it to make.</p>')
    items = []
    for line in lines:
        quantity = _text(line.get("quantity")) or "1"
        description = (_text(line.get("description"))
                       or _text(line.get("product_id")) or "Item")
        note = _text(line.get("line_note"))
        note_html = (f'<span class="line-note">{_esc(note)}</span>'
                     if note else "")
        items.append(f'<li><span class="qty">{_esc(quantity)}&times;</span>'
                     f'{_esc(description)}{note_html}</li>')
    return f'<ul class="klines">{"".join(items)}</ul>'


def GET(request):
    base = _base_dir()
    order_id = _text(request.get("order_id"))
    if not order_id:
        return _not_found()

    try:
        order = object_records.get_collection_record("orders", order_id,
                                                     base_dir=base)
    except Exception:
        return _not_found()
    if not order:
        return _not_found()

    try:
        lines = [row for row in object_records.read_collection_records(
            "order_lines", base_dir=base)
            if _text(row.get("order_id")) == order_id]
    except Exception:
        lines = []
    lines.sort(key=lambda row: _text(row.get("description")))

    number = _text(order.get("number")) or order_id
    who = _text(order.get("customer_name")) or "No name on this order"
    when = _parse_when(order.get("promised_at"))
    promised = (f'<div class="promised">{when.strftime("%H:%M")}</div>'
                if when is not None
                else '<p class="hint">No promised time on this order.</p>')

    note = _text(order.get("customer_note"))
    note_html = ('<div class="note"><strong>For the kitchen</strong><br>'
                 f'{_esc_multiline(note)}</div>' if note else "")

    body = f"""
<div class="pagehead">
<h1>Kitchen ticket</h1>
<p class="hint">Order {_esc(number)}</p>
{promised}
<div class="who">{_esc(who)}</div>
</div>
{note_html}
{_lines_html(lines)}
<p class="hint">This is a kitchen ticket, not an invoice: it says what to
make and deliberately carries no prices -- the money conversation happened
somewhere else, with somebody who may not be the person cooking.</p>
<p class="hint noprint"><a href="/kitchen">Kitchen queue</a></p>
"""
    return _page(body, title=f"Kitchen ticket -- order {number}")
