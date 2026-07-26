"""site_receiving_sheet -- the clipboard. GET
/purchase-orders/{order_id}/receive.

The sheet exists because the dock is where the paperwork actually happens,
and a clipboard beats a phone in a cold warehouse. Somebody is standing by
a roller door with gloves on, a driver waiting, and a pallet that has to be
counted before it can be signed for. A screen requiring a scroll, a login
that timed out, or a touch target the size of a pea are not neutral in that
room: they are the reason the count gets done from memory, at a desk,
twenty minutes later, from what the delivery note claimed. Printed columns
for received and rejected -- left BLANK on purpose -- are what make the
count happen where the goods are, which is the only place it is worth
anything.

A READ, never a table. It folds the purchase order's lines against every
prior receipt, so what it shows is what is genuinely still outstanding and
not what somebody remembered to update. Prior receipts are summarised
underneath rather than hidden: a second delivery against a partly-received
PO is exactly when a receiver needs to see that eight of the ten already
arrived last Tuesday, and its absence is how the same carton gets booked in
twice.

Rejected has its own column, next to received and never folded into it,
because they are different arguments with the supplier: a short delivery
means goods that were never sent and must never be invoiced; a rejection
means goods that arrived, that we hold, and that we want credit for. One
column for both is how a supplier gets paid for what they never delivered.

Requires a signed-in identity, the same gate site_pick_list uses (and the
same shape: a sign-in prompt, not a 403). An unknown or non-purchase order
is a friendly page in the same shell, never a traceback: a mistyped link is
somebody who is still trying to receive a delivery.
"""

import html
import os
from decimal import Decimal, InvalidOperation

import object_records

# A receipt abandoned before anything was counted describes no goods.
NOT_RECEIVED_STATUSES = {"cancelled"}


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
.wrap { max-width: 820px; margin: 0 auto; padding: 2rem 1.25rem; }
.pagehead { margin-bottom: 1rem; }
.pagehead h1 { margin: 0 0 0.25rem; font-size: 1.4rem; }
.hint { color: var(--muted, #999); font-size: 0.9rem; }
.supplier { border: 1px solid var(--line, #333); border-radius: 8px; padding: 0.9rem 1.1rem; margin: 1rem 0; }
table.sheet { width: 100%; border-collapse: collapse; margin: 1rem 0 1.5rem; }
table.sheet th, table.sheet td { text-align: left; padding: 0.45rem 0.6rem; border-bottom: 1px solid var(--line, #38384a); }
table.sheet td.num, table.sheet th.num { text-align: right; }
table.sheet td.blank { border-bottom: 1px solid var(--line, #38384a); min-width: 5rem; }
.sign { margin-top: 2rem; }
.sign span { display: inline-block; border-bottom: 1px solid var(--line, #999); min-width: 14rem; }
.notfound { text-align: center; padding: 3rem 1rem; }
@media print {
  nav, header.app, .noprint, .btn { display: none !important; }
  .wrap { max-width: none; padding: 0; }
  body { background: #fff; color: #000; }
  .supplier, table.sheet th, table.sheet td { border-color: #999; }
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
        title="Receiving sheet -- not found", status=404)


def _received_by_line(base, order_id):
    """Cumulative received quantity per order line, plus the receipts and
    lines it was folded from -- one read of the collections, used by both
    tables on the page."""
    try:
        receipts = [row for row in object_records.read_collection_records(
            "receipts", base_dir=base)
            if _text(row.get("order_id")) == order_id
            and _text(row.get("status")) not in NOT_RECEIVED_STATUSES]
    except Exception:
        receipts = []
    counted = {row["id"]: row for row in receipts}
    try:
        lines = [row for row in object_records.read_collection_records(
            "receipt_lines", base_dir=base)
            if _text(row.get("receipt_id")) in counted]
    except Exception:
        lines = []

    received = {}
    for line in lines:
        key = _text(line.get("order_line_id"))
        received[key] = received.get(key, Decimal(0)) + _quantity(
            line.get("quantity_received"))
    return received, list(counted.values()), lines


def _lines_html(order_lines, received):
    if not order_lines:
        return ('<p class="hint">This purchase order has no lines, so there '
                'is nothing anybody could be delivering against it.</p>')
    rows = []
    for line in order_lines:
        ordered = _quantity(line.get("quantity"))
        already = received.get(line["id"], Decimal(0))
        outstanding = ordered - already
        rows.append(
            "<tr>"
            f"<td>{_esc(line.get('description')) or _esc(line.get('product_id'))}</td>"
            f"<td class=\"num\">{_esc(_number(ordered))}</td>"
            f"<td class=\"num\">{_esc(_number(already))}</td>"
            f"<td class=\"num\">{_esc(_number(outstanding if outstanding > 0 else Decimal(0)))}</td>"
            "<td class=\"blank\">&nbsp;</td>"
            "<td class=\"blank\">&nbsp;</td>"
            "</tr>")
    return f"""
<table class="sheet">
<thead><tr>
<th>Item</th><th class="num">Ordered</th><th class="num">Already in</th>
<th class="num">Expected today</th><th>Received</th><th>Rejected</th>
</tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>"""


def _prior_html(receipts, lines):
    if not receipts:
        return ('<p class="hint">No deliveries have been booked in against '
                'this purchase order yet.</p>')
    by_receipt = {}
    for line in lines:
        key = _text(line.get("receipt_id"))
        entry = by_receipt.setdefault(key, [Decimal(0), Decimal(0)])
        entry[0] += _quantity(line.get("quantity_received"))
        entry[1] += _quantity(line.get("quantity_rejected"))
    receipts = sorted(receipts, key=lambda row: _text(row.get("received_on")))
    rows = "".join(
        "<tr>"
        f"<td>{_esc(row.get('received_on')) or 'no date'}</td>"
        f"<td>{_esc(row.get('supplier_reference')) or '&mdash;'}</td>"
        f"<td>{_esc(row.get('status'))}</td>"
        f"<td class=\"num\">{_esc(_number(by_receipt.get(row['id'], [Decimal(0), Decimal(0)])[0]))}</td>"
        f"<td class=\"num\">{_esc(_number(by_receipt.get(row['id'], [Decimal(0), Decimal(0)])[1]))}</td>"
        "</tr>"
        for row in receipts)
    return f"""
<h2>Already received</h2>
<table class="sheet">
<thead><tr><th>Date</th><th>Their reference</th><th>Status</th>
<th class="num">Received</th><th class="num">Rejected</th></tr></thead>
<tbody>{rows}</tbody>
</table>"""


def GET(request):
    identity = request.get("_identity") or {}
    user_id = _text(identity.get("user_id"))
    base = _base_dir()
    order_id = _text(request.get("order_id"))

    if not user_id:
        return _page(
            '<div class="pagehead"><h1>Receiving sheet</h1></div>'
            f'<p class="hint"><a href="/login?next=/purchase-orders/{_esc(order_id)}/receive">'
            'Sign in</a> to check a delivery in.</p>',
            title="Receiving sheet")

    if not order_id:
        return _not_found("No purchase order was named in that link.")
    try:
        order = object_records.get_collection_record("orders", order_id,
                                                     base_dir=base)
    except Exception:
        order = None
    if not order:
        return _not_found("There is no purchase order with that id. It may "
                          "have been mistyped.")
    if _text(order.get("doc_type")) != "purchase":
        return _not_found(
            "That is a sales order. Goods leave on a shipment and arrive on a "
            "receipt, so there is no delivery to check in here -- "
            '<a href="/pick-list">the pick list</a> is the sheet for orders '
            "going out.")

    try:
        order_lines = [row for row in object_records.read_collection_records(
            "order_lines", base_dir=base)
            if _text(row.get("order_id")) == order_id]
    except Exception:
        order_lines = []
    order_lines.sort(key=lambda row: _text(row.get("description")))

    received, receipts, receipt_lines = _received_by_line(base, order_id)

    number = _text(order.get("number")) or order_id
    supplier = _text(order.get("customer_name")) or "No supplier on this order"
    expected = _text(order.get("expected_date"))

    body = f"""
<div class="breadcrumb noprint"><a href="/">Home</a> / Receiving</div>
<div class="pagehead">
<h1>Receiving sheet</h1>
<p class="hint">Purchase order {_esc(number)}
&middot; {_esc(order.get('status'))}
{('&middot; expected ' + _esc(expected)) if expected else ''}</p>
</div>
<div class="supplier"><strong>Supplier</strong><br>{_esc(supplier)}</div>
{_lines_html(order_lines, received)}
<p class="hint">Count the pallet against "expected today", write what
actually turned up in the blank columns, and put anything damaged or wrong
in Rejected -- rejected goods arrived and are going back, which is not the
same argument as a short delivery and must never be folded into it.</p>
{_prior_html(receipts, receipt_lines)}
<div class="sign"><p>Received by <span>&nbsp;</span> &nbsp; Date
<span>&nbsp;</span></p>
<p>Their delivery note number <span>&nbsp;</span></p></div>
<p class="hint noprint">Type the counted sheet in with Receive goods against
this purchase order.</p>
"""
    return _page(body, title=f"Receiving sheet -- {number}")
