"""site_packing_slip -- what is in this box, printed. GET
/shipments/{shipment_id}/slip.

NO PRICES. Not "prices hidden behind a gift flag", not "prices unless the
buyer ticked something": none, ever. A packing slip is not an invoice --
it exists so the person opening the parcel can check the contents against
what they ordered, and the money conversation already happened somewhere
else with somebody who may not be the person holding the box. Making the
slip priceless by construction is what makes EVERY shipment gift-safe
without a flag to remember, and a flag somebody forgets to tick is
precisely how a birthday present arrives with the amount paid stapled to
it. The invoice has its own door (the payment portal) for anyone who
wants the numbers.

Printable rather than pretty: minimal print CSS, the nav hidden at
@media print, everything else the ordinary page shell so an operator does
not arrive somewhere that looks like a different product. Operator-facing,
not customer-facing -- the shipment id is an internal record id, so this
page sits behind the ordinary registered permission rather than a
capability token like the invoice portal's.

customer_note and gift_message are printed WHEN THEY EXIST, read with
.get: the merchandising slice that adds those two fields to orders has
not landed yet (plan/fulfillment-logistics-spec.md), and a slip that
crashed on an older order would be a page that breaks itself waiting for
a feature. The packer is the one who needs both -- special instructions
are useless to anyone else, and a gift message unprinted is a gift
message that never happened.

An unknown shipment is a friendly 404 in the same shell, never a
traceback: a mistyped link is somebody who is still at their desk trying
to send a parcel.
"""

import html
import os

import object_records

ACTOR = "site_packing_slip"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _esc(value):
    return html.escape(_text(value))


def _esc_multiline(value):
    return _esc(value).replace("\n", "<br>")


_STYLE = """
.wrap { max-width: 720px; margin: 0 auto; padding: 2rem 1.25rem; }
.pagehead { margin-bottom: 1.5rem; }
.pagehead h1 { margin: 0 0 0.25rem; font-size: 1.4rem; }
.hint { color: var(--muted, #999); font-size: 0.9rem; }
.shipto { border: 1px solid var(--line, #333); border-radius: 8px; padding: 0.9rem 1.1rem; margin: 1rem 0; }
.note { border: 1px solid var(--line, #333); border-radius: 8px; padding: 0.9rem 1.1rem; margin: 1rem 0; white-space: pre-wrap; }
table.lines { width: 100%; border-collapse: collapse; margin: 1rem 0 1.5rem; }
table.lines th, table.lines td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--line, #38384a); }
table.lines td.num, table.lines th.num { text-align: right; }
.notfound { text-align: center; padding: 3rem 1rem; }
@media print {
  nav, header.app, .noprint, .btn { display: none !important; }
  .wrap { max-width: none; padding: 0; }
  body { background: #fff; color: #000; }
  .shipto, .note { border-color: #999; }
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
        '<p class="hint">There is no shipment with that id. It may have been '
        'mistyped, or the shipment may have been deleted. '
        '<a href="/pick-list">Back to the pick list</a>.</p></div>',
        title="Packing slip -- not found", status=404)


def _lines_html(lines):
    if not lines:
        return ('<p class="hint">This shipment has no lines yet -- nothing '
                'has been picked into it.</p>')
    rows = "".join(
        "<tr>"
        f"<td>{_esc(line.get('description')) or _esc(line.get('product_id'))}</td>"
        f"<td class=\"num\">{_esc(line.get('quantity') or '1')}</td>"
        "</tr>"
        for line in lines)
    return f"""
<table class="lines">
<thead><tr><th>Item</th><th class="num">Qty</th></tr></thead>
<tbody>{rows}</tbody>
</table>"""


def _note_html(order):
    """Special instructions and a gift message, when the order has such
    fields at all -- .get, never [], see the module docstring."""
    blocks = []
    note = _text(order.get("customer_note"))
    if note:
        blocks.append('<div class="note"><strong>Special instructions</strong>'
                      f"<br>{_esc_multiline(note)}</div>")
    gift = _text(order.get("gift_message"))
    if gift:
        blocks.append('<div class="note"><strong>Gift message</strong>'
                      f"<br>{_esc_multiline(gift)}</div>")
    return "".join(blocks)


def GET(request):
    base = _base_dir()
    shipment_id = _text(request.get("shipment_id"))
    if not shipment_id:
        return _not_found()

    try:
        shipment = object_records.get_collection_record("shipments",
                                                        shipment_id,
                                                        base_dir=base)
    except Exception:
        return _not_found()
    if not shipment:
        return _not_found()

    try:
        order = object_records.get_collection_record(
            "orders", _text(shipment.get("order_id")), base_dir=base)
    except Exception:
        order = {}

    try:
        lines = [row for row in object_records.read_collection_records(
            "shipment_lines", base_dir=base)
            if _text(row.get("shipment_id")) == shipment_id]
    except Exception:
        lines = []
    lines.sort(key=lambda row: _text(row.get("description")))

    number = _text(order.get("number")) or _text(shipment.get("order_id"))
    when = (_text(shipment.get("shipped_on"))
            or _text(shipment.get("created_at"))[:10]
            or _text(order.get("order_date")))

    ship_to = _text(shipment.get("ship_to_name")) or _text(order.get("customer_name"))
    address = _text(shipment.get("ship_to_address"))
    address_html = f"<br>{_esc_multiline(address)}" if address else ""

    carrier = _text(shipment.get("carrier"))
    tracking = _text(shipment.get("tracking_number"))
    carriage = " &middot; ".join(
        part for part in (_esc(carrier), _esc(tracking)) if part)

    body = f"""
<div class="pagehead">
<h1>Packing slip</h1>
<p class="hint">Order {_esc(number)}
&middot; {_esc(when) or 'no date'}
&middot; shipment {_esc(shipment_id)}</p>
</div>
<div class="shipto"><strong>Ship to</strong><br>{_esc(ship_to) or 'No name on this order'}{address_html}</div>
{_note_html(order)}
{_lines_html(lines)}
<p class="hint">This is a packing slip, not an invoice: it says what is in
the box and deliberately carries no prices, so any parcel can be sent as a
gift.{(' &middot; ' + carriage) if carriage else ''}</p>
<p class="hint noprint"><a href="/pick-list">Pick list</a></p>
"""
    return _page(body, title=f"Packing slip -- order {number}")
