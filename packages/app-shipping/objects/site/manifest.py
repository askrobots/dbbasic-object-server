"""site_manifest -- what the driver takes away, and what they sign for.
GET /shipments/manifest?date=&carrier=

A FOLD OVER FACTS, NEVER EDITABLE FROM THIS PAGE. There is no manifests
collection, no form, no button and no link on here that changes anything.
That is the whole character of the document: a manifest is the piece of
paper a driver signs at the dock, and a manifest you can edit after the van
has left is not a manifest -- it is a story about a van. Everything on it is
read live from the shipments that carry the facts (docs/logic-decisions.md
#1: the shipment rows are the facts, this page is a read of them), so the
copy in the driver's hand and the copy on the screen cannot drift, and a
correction is a correction to a SHIPMENT, made where that shipment lives,
with the change log that comes with it.

PER CARRIER, because that is the unit of handover. Two carriers collect from
the same dock on the same morning and each takes their own pile; one list
mixing them is a list neither driver can sign. With no ?carrier= the page
prints every carrier as its own section with its own count and its own
signature line, so the whole morning is one print job; with ?carrier= it is
the single pile, for the shop that prints as each van pulls up.

ONE DAY, keyed on shipped_on -- the date the box was handed over, stamped by
system_order_fulfillment the moment a shipment reaches `shipped`. Not
created_at: a parcel packed on Friday and collected on Monday belongs on
Monday's manifest, because Monday is the day somebody signed for it. A
shipment with no shipped_on therefore appears on no manifest at all, which
is the honest answer -- it has not been handed to anybody.

NO PRICES, the same rule as the packing slip and for a stronger reason: this
document is handed to a third party who has no business knowing what the
contents cost, and a carrier that can read the value of every parcel in the
van is a shop that has published its own theft target.

Columns are the four things a handover argument is ever about -- tracking
number, order, who it is going to, and which service was paid for -- plus
weight WHEN THE ROWS HAVE ONE. shipments declares no weight field: a
deployment that weighs parcels adds one as an extra field, and the column
appears because rows carry it, rather than a column of blanks appearing
because a schema hoped somebody would.

Sign-in gated exactly like site_pick_list: a sign-in prompt, not a 403, and
shipments belonging to the signed-in operator OR to nobody (guest checkout
leaves owner_id blank by design, so filtering strictly by owner would show
an empty dock to the one person with parcels going out).
"""

import html
import os
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import object_records

ACTOR = "site_manifest"

# What a driver can be handed. `open` and `packed` are still on the bench;
# the inbound ladder is somebody else's parcel coming back to us.
HANDED_OVER = {"shipped", "in_transit", "delivered", "returned_to_sender",
               "lost"}

# The heading for parcels whose carrier nobody typed. Not "Unknown": the
# shop knows perfectly well who took them, it just did not write it down,
# and a manifest that says so is a manifest somebody fixes.
NO_CARRIER = "No carrier named"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _esc(value):
    return html.escape(_text(value))


def _weight(value):
    try:
        return Decimal(_text(value) or "0")
    except InvalidOperation:
        return Decimal(0)


def _number(value):
    return format(value.normalize(), "f")


def _day(value):
    """The requested day, or today. A date nobody can parse is answered with
    today plus a note rather than a traceback: somebody is standing at a dock
    holding parcels, and the page's job is to print."""
    raw = _text(value)
    if not raw:
        return date.today().isoformat(), ""
    try:
        return date.fromisoformat(raw).isoformat(), ""
    except ValueError:
        return date.today().isoformat(), (
            f"{raw!r} is not a date this page can read (use YYYY-MM-DD); "
            f"showing today instead.")


_STYLE = """
.manifest-table { width: 100%; border-collapse: collapse; margin: 0.4rem 0 1rem; }
.manifest-table th, .manifest-table td { text-align: left; padding: 0.35rem 0.6rem; border-bottom: 1px solid var(--line, #38384a); }
.manifest-table th { font-weight: 600; }
.manifest-table td.num, .manifest-table th.num { text-align: right; }
.carrier { margin: 1.75rem 0 0; }
.carrier h2 { font-size: 1.05rem; margin: 0 0 0.2rem; }
.sign { margin: 0.6rem 0 1.5rem; border-top: 1px solid var(--line, #38384a); padding-top: 0.6rem; font-size: 0.9rem; color: var(--muted, #999); }
.hint { color: var(--muted, #999); font-size: 0.9rem; }
@media print {
  nav, header.app, .noprint, .btn { display: none !important; }
  body { background: #fff; color: #000; }
  .carrier { page-break-inside: avoid; }
  .sign { color: #000; border-top-color: #999; }
}
"""


def _rows_for(base, user_id, day, carrier_filter):
    """Every parcel handed over on one day, grouped by carrier.

    Full scans of three collections, the same trade site_pick_list makes: a
    dock that hands over a million parcels a day is not running this
    software, and a fold that reads the records is a fold that cannot
    disagree with them.
    """
    try:
        shipments = object_records.read_collection_records("shipments",
                                                           base_dir=base)
    except Exception:
        return {}, False

    wanted = _text(carrier_filter).lower()
    mine = []
    for row in shipments:
        if _text(row.get("direction")) == "inbound":
            continue
        if _text(row.get("status")) not in HANDED_OVER:
            continue
        if _text(row.get("shipped_on")) != day:
            continue
        if _text(row.get("owner_id")) not in ("", user_id):
            continue
        carrier = _text(row.get("carrier"))
        if wanted and carrier.lower() != wanted:
            continue
        mine.append(row)
    if not mine:
        return {}, False

    try:
        orders = {row["id"]: row for row in
                  object_records.read_collection_records("orders", base_dir=base)}
    except Exception:
        orders = {}

    weighed = any(_text(row.get("weight")) for row in mine)
    grouped = {}
    for row in mine:
        carrier = _text(row.get("carrier")) or NO_CARRIER
        order = orders.get(_text(row.get("order_id")), {})
        grouped.setdefault(carrier, []).append({
            "tracking": _text(row.get("tracking_number")),
            "order": _text(order.get("number")) or _text(row.get("order_id")),
            "ship_to": (_text(row.get("ship_to_name"))
                        or _text(order.get("customer_name"))),
            "service": _text(row.get("service")),
            "weight": _text(row.get("weight")),
        })
    for rows in grouped.values():
        # Tracking-number order, so checking the pile against the paper is a
        # walk down the list rather than a search for each parcel.
        rows.sort(key=lambda entry: (entry["tracking"] or "zzz", entry["order"]))
    return grouped, weighed


def _row(row, weighed):
    # A parcel with no tracking number is on the manifest anyway and says so:
    # it is in the van either way, and leaving it off would make the paper
    # disagree with the pile.
    tracking = _esc(row["tracking"]) or '<span class="hint">not recorded</span>'
    weight = f'<td class="num">{_esc(row["weight"])}</td>' if weighed else ""
    return (f"<tr><td>{tracking}</td><td>{_esc(row['order'])}</td>"
            f"<td>{_esc(row['ship_to'])}</td><td>{_esc(row['service'])}</td>"
            f"{weight}</tr>")


def _table(rows, weighed):
    head = ("<tr><th>Tracking number</th><th>Order</th><th>Ship to</th>"
            "<th>Service</th>" + ('<th class="num">Weight</th>' if weighed else "")
            + "</tr>")
    body = "".join(_row(row, weighed) for row in rows)
    return (f'<table class="manifest-table"><thead>{head}</thead>'
            f"<tbody>{body}</tbody></table>")


def _carrier_block(carrier, rows, weighed):
    total = sum((_weight(row["weight"]) for row in rows), Decimal(0))
    weight_line = (f" &middot; {_esc(_number(total))} total weight"
                   if weighed and total > 0 else "")
    parcels = f"{len(rows)} parcel" + ("s" if len(rows) != 1 else "")
    return f"""
<div class="carrier">
<h2>{_esc(carrier)}</h2>
<p class="hint">{parcels}{weight_line}</p>
{_table(rows, weighed)}
<div class="sign">Received by (print name) ______________________________
&nbsp; Signature ______________________________
&nbsp; Time __________</div>
</div>"""


def GET(request):
    base = _base_dir()
    identity = request.get("_identity") or {}
    user_id = _text(identity.get("user_id"))
    day, date_problem = _day(request.get("date"))
    carrier_filter = _text(request.get("carrier"))

    if not user_id:
        body = ('<div class="pagehead"><h1>Shipping manifest</h1></div>'
                '<p class="hint"><a href="/login?next=/shipments/manifest">Sign in</a> '
                'to see what went out.</p>')
        return _page(body, title="Shipping manifest")

    grouped, weighed = _rows_for(base, user_id, day, carrier_filter)
    parcels = sum(len(rows) for rows in grouped.values())

    if grouped:
        blocks = "".join(_carrier_block(carrier, grouped[carrier], weighed)
                         for carrier in sorted(grouped))
    else:
        blocks = ('<p class="hint">Nothing was handed over on this day'
                  + (f" by {_esc(carrier_filter)}" if carrier_filter else "")
                  + ". A shipment joins the manifest for the day it reaches "
                    "<em>shipped</em>, which is when its shipped_on is "
                    "stamped.</p>")

    previous = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    following = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    query = f"&carrier={_esc(carrier_filter)}" if carrier_filter else ""
    scope = (f"Carrier: {_esc(carrier_filter)}" if carrier_filter
             else f"{len(grouped)} carrier" + ("s" if len(grouped) != 1 else ""))
    problem = f'<p class="hint">{_esc(date_problem)}</p>' if date_problem else ""

    body = f"""
<div class="breadcrumb noprint"><a href="/">Home</a> / <a href="/shipments">Shipments</a> / Manifest</div>
<div class="pagehead">
<h1>Shipping manifest</h1>
<p class="hint">{_esc(day)} &middot; {scope} &middot; {parcels} parcel{'' if parcels == 1 else 's'}</p>
</div>
{problem}
{blocks}
<p class="hint noprint">
<a href="/shipments/manifest?date={_esc(previous)}{query}">&larr; {_esc(previous)}</a>
&nbsp; <a href="/shipments/manifest?date={_esc(following)}{query}">{_esc(following)} &rarr;</a>
&nbsp; <a href="/shipments">All shipments</a>
</p>
<p class="hint noprint">This page is a read of the shipment records and
cannot be edited here: a manifest you can change after the van has left is
not a manifest. Correct a parcel on its shipment.</p>
"""
    return _page(body, title=f"Shipping manifest -- {day}")


def _page(body, *, title):
    return {"content_type": "text/html; charset=utf-8",
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
<header class="app"><h1><a href="/">DBBASIC</a></h1></header>
{body}
</div>
<script src="/nav"></script>
</body>
</html>"""}
