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

**Since the document layer landed, that rule is a PARAMETER, not a
paragraph.** `object_documents.KINDS["packing_slip"]["show_money"]` is
False, there is no per-call override offered, and build_model does not
hide the money -- it never puts it in the model. This file cannot print a
price even by mistake, because it never holds one: it passes `money=None`
to the fold, and the fold never asks for a formatter it was told not to
need. The old version achieved the same outcome by simply not writing the
columns, which worked exactly as long as everybody who edited the file had
read the docstring first.

Everything else this page draws -- header, identity, ship-to, the lines
table, page breaks, the repeated table heading, @page size -- now comes
from the shared renderer instead of from a private `@media print` block
that agreed with the other six printables about nothing. The one rule
those six blocks all missed was `thead { display: table-header-group }`,
which is why a shipment with fifty lines used to print its column
headings once.

Printable rather than pretty, and the nav stays: this is an operator page
(the shipment id is an internal record id, so it sits behind the ordinary
registered permission rather than a capability token like the invoice
portal's), and an operator must not arrive somewhere that looks like a
different product.

customer_note and gift_message are printed WHEN THEY EXIST, read with
.get. Checkout now collects both and stamps them on the order the moment
the orders schema declares the columns (action_checkout's `_has_field`
gate); until it does, the shop carries them on the cart and says so, and
older orders simply have neither. So .get stays: a slip that crashed on
an order predating the fields would be a page breaking itself over a
column somebody added last week. The packer is the one who needs both --
special instructions are useless to anyone else, and a gift message
unprinted is a gift message that never happened.

The gift message needs no gift flag beside it. This page shows no prices
at all, by construction, so every parcel is already gift-safe: the
message is about warmth, not secrecy.

An unknown shipment is a friendly 404 in the same shell, never a
traceback: a mistyped link is somebody who is still at their desk trying
to send a parcel.
"""

import html
import os

import object_documents
import object_records

ACTOR = "site_packing_slip"
KIND = "packing_slip"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _esc(value):
    return html.escape(_text(value))


def _settings(base):
    """Every app_settings row as one mapping -- what object_documents wants,
    and one scan instead of four. Duplicated on purpose like every other
    package that reads app_settings (docs/logic-decisions.md #4)."""
    values = {}
    try:
        for row in object_records.read_collection_records("app_settings",
                                                          base_dir=base):
            key, value = _text(row.get("key")), _text(row.get("value"))
            if key and value:
                values[key] = value
    except Exception:
        pass
    return values


def _not_found():
    """A friendly 404 in the same shell -- an empty document with the message
    where the document would have been, rather than a traceback at somebody
    who is still trying to send a parcel."""
    return object_documents.render_page(
        object_documents.build_model(KIND, {}, {}, require_business=False),
        title="Packing slip -- not found",
        before='<h1>Not found</h1>'
               '<p class="doc-hint">There is no shipment with that id. It may '
               'have been mistyped, or the shipment may have been deleted. '
               '<a href="/pick-list">Back to the pick list</a>.</p>',
        setup_nudge=False,
        status=404)


def _notes_for(order):
    """Special instructions and a gift message, when the order has such
    fields at all -- .get, never [], see the module docstring."""
    notes = []
    note = _text(order.get("customer_note"))
    if note:
        notes.append({"title": "Special instructions", "body": note})
    gift = _text(order.get("gift_message"))
    if gift:
        notes.append({"title": "Gift message", "body": gift})
    return notes


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
    order = order or {}

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

    carrier = _text(shipment.get("carrier"))
    tracking = _text(shipment.get("tracking_number"))
    carriage = " &middot; ".join(
        part for part in (_esc(carrier), _esc(tracking)) if part)

    # money=None is not an omission: on a no-prices kind the fold never calls
    # a formatter, so there is nothing here that could produce a price.
    facts = object_documents.facts_from_records(
        KIND, shipment, lines, money=None,
        extra={
            "number": number,
            "date": when,
            "reference": f"shipment {shipment_id}",
            "to": {"name": (_text(shipment.get("ship_to_name"))
                            or _text(order.get("customer_name"))
                            or "No name on this order"),
                   "address": _text(shipment.get("ship_to_address"))},
            "notes": _notes_for(order),
            "footer": ("This is a packing slip, not an invoice: it says what "
                       "is in the box and deliberately carries no prices, so "
                       "any parcel can be sent as a gift."
                       + ((" &middot; " + carriage) if carriage else "")),
        })

    settings = _settings(base)
    # require_business=False: an operator is standing at a printer and a parcel
    # is waiting. A nameless business is refused where refusing costs nothing
    # -- at send time, in action_send_document -- and nudged (no-print) here.
    model = object_documents.build_model(KIND, facts, settings,
                                         require_business=False)

    empty = ('<p class="doc-hint">This shipment has no lines yet -- nothing '
             'has been picked into it.</p>' if not model["lines"] else "")
    pdf = object_documents.pdf_engine_status(
        settings.get(object_documents.PDF_ENGINE_SETTING))

    return object_documents.render_page(
        model,
        title=f"Packing slip -- order {number}",
        after=empty + '<p class="doc-hint noprint"><a href="/pick-list">Pick list</a></p>',
        pdf=pdf["available"],
        size=settings.get(object_documents.PAGE_SIZE_SETTING))
