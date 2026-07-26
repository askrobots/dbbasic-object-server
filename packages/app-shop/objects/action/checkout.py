"""action_checkout -- a basket becomes an order AND the bill for it.

POST {session_token, customer_email, customer_name?, confirm_prices?,
      preview?, customer_note?, gift_message?}

This is where browsing turns into a commitment, so it is where every
check happens at once:

- the products are still for sale,
- the prices still match what the shopper agreed to,
- the stock is there.

All of them are reported TOGETHER. Telling somebody about one problem,
letting them fix it, then revealing the next is how a checkout gets
abandoned -- and it is the easiest thing in the world to get wrong by
returning on the first failure.

Price disagreement is a hard stop with both numbers shown, not a silent
resolution in either direction. Accepting the new price on the shopper's
behalf is a bait-and-switch; honouring a three-week-old basket forever is
an open-ended liability. `confirm_prices: true` is the shopper saying yes
to the current prices, having seen them.

**Stock is checked here and moved nowhere.** The order is created as a
draft; stock moves and the order confirms when money actually arrives
(system_shop_fulfillment). Decrementing on checkout would make every
abandoned payment a phantom sale.

The oversell race is real and is not pretended away: two shoppers can
both pass this gate for the last unit. That is accepted deliberately at
this scale -- it surfaces at the stock move, which is visible and
refundable -- rather than paid for with a reservation ledger nobody has
needed yet. The same honest posture as hook_wallet_entries' documented
check-then-append race.

**The invoice is raised here too, already issued.** An order with no
invoice gives the buyer nothing to pay: the money is owed and there is no
document saying so and no door to walk through. The invoice is created
"sent" rather than "draft" because checkout IS the issuing act -- the
shopper has committed, and a draft would mean somebody in the back office
must press a button before the person standing at the counter can hand
over money. The response carries the pay link for the same reason.

If the invoice cannot be raised, the ORDER STILL STANDS. Losing a sale to
an invoicing hiccup is the expensive failure; an order without an invoice
is visible, and the period biller or an operator can raise one. So only
the invoice block is wrapped -- never the order.

**Tax and postage are charged here, or the shop is losing money and
breaking the law.** Four settings say how (all absent = a shop that
charges neither, which is exactly what it did before):

  shop.tax_rate_bps            basis points, 1500 = 15%; 0 = no tax
  shop.tax_shipping            is the postage part of the taxable base?
  shop.shipping_flat_cents     one flat charge; 0 = shipping disabled
  shop.shipping_free_over_cents  free over this basket size; 0 = never

The arithmetic itself is object_cart.checkout_totals and nothing here
recomputes any part of it -- this file only writes down what that fold
said. Retrofitting tax later would mean restating invoices that have
already been sent, which is why a flat honest rate now beats a clever
one afterwards.

**Postage is an invoice LINE, tax is an invoice TOTAL.** The line is
because a customer must be able to see what the delivery cost instead of
finding it welded into a number they cannot check, and because it keeps
the invoice's subtotal the sum of its own lines. The tax is a total
because invoices already carry a tax_cents field that means exactly
this, and inventing a "Sales tax" pseudo-line beside a real field would
give the same money two homes.

One seam is left open rather than papered over. invoice_totals, if an
operator ever turns DBBASIC_ENABLE_EVENT_HANDLERS on AND somebody edits
a line of one of these invoices through the API, restates tax as the sum
of the lines' own line_tax_cents -- and only the postage line carries
one, so such a restatement would drop the tax on the goods. Nothing
this file writes triggers that today (checkout writes through
object_records directly, which never reaches the dispatcher), and the
flag is off everywhere. The honest fixes are a decision for whoever owns
invoice_totals -- either it learns to leave an invoice-level tax alone,
or every taxable line carries its own rate -- and the second one costs a
cent of per-line rounding drift, which is exactly the error the single
rounding above exists to avoid. Written down here so the next person
finds it before a customer does.

The ORDER records tax_cents and a total_cents that includes both, but
its subtotal stays goods-only -- so postage on an order is currently
implied by total - subtotal - tax rather than stated. That is a known
gap: orders has no shipping_cents column yet. The moment one exists this
file stamps it (it checks the schema rather than assuming), and until
then the itemisation lives where a customer actually reads it, on the
invoice.

**Special instructions and a gift message: two fields, not a subsystem.**
customer_note is what the shopper needs the PACKER to know ("leave it
with the neighbour"), gift_message is what should be printed on the slip.
Both are optional, both are stamped on the cart, and both are stamped on
the ORDER through the same `_has_field` check shipping_cents uses -- the
picker and the packer read the order, and a note that lives only on a
basket is a note nobody in the warehouse ever sees. Until orders declares
those two columns the values stay on the cart rather than being written
into a column that does not exist, and the response says so
(`notes_on_cart_only`) instead of pretending the packer will get them:
the failure that prevents is a birthday message the shopper typed, the
shop charged for, and nobody ever printed.

There is deliberately no gift FLAG. The packing slip carries no prices by
construction (site_packing_slip), so every parcel is already gift-safe,
and a flag somebody forgets to tick is exactly how a present arrives with
the amount paid stapled to it. The message is about warmth, not secrecy.
"""

import os
import secrets
from datetime import date, timedelta

import object_cart
import object_ids
import object_records
import object_schemas
import object_stock

ACTOR = "action_checkout"

DEFAULT_DUE_DAYS = 14


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _truthy(value):
    return _text(value).lower() in ("true", "1", "yes", "on")


def _setting(base, key, default):
    try:
        for row in object_records.read_collection_records("app_settings", base_dir=base):
            if row.get("key") == key and _text(row.get("value")):
                return _text(row["value"])
    except Exception:
        pass
    return default


def _int(value, default=0):
    try:
        return int(_text(value) or default)
    except (TypeError, ValueError):
        return default


def _has_field(base, collection, field):
    """Does this collection's schema declare `field` yet?

    Asked rather than assumed because orders is due to gain a
    shipping_cents column and this file wants to fill it the day it
    appears, without a migration and without writing a field that does
    not exist into a shop that has not upgraded. Any failure to read the
    schema answers "no": a missing column costs an itemisation, a
    rejected write would cost the order.
    """
    try:
        schema = object_schemas.get_schema(collection, base_dir=base)
    except Exception:
        return False
    return any(f.get("name") == field for f in schema.get("fields") or [])


def _tax_and_shipping_settings(base):
    """The four numbers that decide what this sale costs beyond the goods.

    All default to 0/off, so a shop that has configured nothing charges
    nothing extra and its invoices look exactly as they did yesterday --
    absent configuration must read as "this shop does not do that", never
    as a broken tax line.
    """
    return {
        "tax_rate_bps": _int(_setting(base, "shop.tax_rate_bps", 0)),
        "tax_shipping": _truthy(_setting(base, "shop.tax_shipping", "")),
        "shipping_flat_cents": _int(_setting(base, "shop.shipping_flat_cents", 0)),
        "free_over_cents": _int(_setting(base, "shop.shipping_free_over_cents", 0)),
    }


def _pay_path_for_order(base, order_id):
    """The pay link an already-checked-out order was given, if it has one.

    Everything here is best-effort on purpose: a retry must hand back the
    SAME door rather than mint a second invoice, but an order whose invoice
    or token cannot be found is still a perfectly good order. The caller
    gets no link instead of an error.
    """
    try:
        order = object_records.get_collection_record("orders", order_id, base_dir=base)
        invoice_id = _text(order.get("invoice_id"))
        if not invoice_id:
            return ""
        invoice = object_records.get_collection_record("invoices", invoice_id,
                                                       base_dir=base)
        token = _text(invoice.get("portal_token"))
    except Exception:
        return ""
    return f"/pay/{token}" if token else ""


def _products(base, product_ids):
    out = {}
    for product_id in product_ids:
        try:
            row = object_records.get_collection_record(
                "products", product_id, base_dir=base)
        except Exception:
            row = None
        if row:
            out[product_id] = row
    return out


def _on_hand(base, products):
    """Levels for the products this basket touches, and which of them the
    shop actually tracks.

    A service or a digital download has no stock level, and treating "no
    level recorded" as "none available" would refuse to sell the things
    that are always available.
    """
    # products.product_type: physical | digital | service | subscription |
    # asset. Only the first and last are things that sit on a shelf and
    # can run out; a download or an hour of work never does.
    tracked = {pid for pid, row in products.items()
               if _text(row.get("product_type")) in ("physical", "asset", "")}
    levels = {}
    for product_id in tracked:
        try:
            levels[product_id] = object_stock.total_quantity(
                product_id, base_dir=base)
        except Exception:
            continue
    return levels, tracked


def POST(request):
    base = _base_dir()
    token = _text(request.get("session_token"))
    if not token:
        return {"status": 400, "error": "session_token is required"}

    try:
        carts = object_records.read_collection_records("carts", base_dir=base)
    except Exception:
        return {"ok": True, "skipped": "shop not installed (carts absent)"}

    cart = next((c for c in carts if _text(c.get("session_token")) == token
                 and _text(c.get("status")) == "open"), None)
    if cart is None:
        settled = next((c for c in carts
                        if _text(c.get("session_token")) == token
                        and _text(c.get("checked_out_order_id"))), None)
        if settled:
            # One cart, one order, however many times a browser retries --
            # and the same pay link back each time, because a shopper who
            # double-clicked still needs the door, not a second bill.
            order_id = _text(settled["checked_out_order_id"])
            duplicate = {"ok": True, "duplicate": True, "order_id": order_id,
                         "note": "this basket has already been checked out"}
            pay_path = _pay_path_for_order(base, order_id)
            if pay_path:
                duplicate["pay_path"] = pay_path
            return duplicate
        return {"status": 404, "error": "No open basket for this session."}

    try:
        all_items = object_records.read_collection_records("cart_items", base_dir=base)
    except Exception:
        all_items = []
    items = [i for i in all_items if _text(i.get("cart_id")) == cart["id"]]

    products = _products(base, {_text(i.get("product_id")) for i in items})
    levels, tracked = _on_hand(base, products)
    blockers = object_cart.checkout_blockers(items, products, levels, tracked=tracked)

    if blockers["empty"]:
        return {"status": 400, "error": "There is nothing in this basket."}

    if blockers["price_changes"] and not _truthy(request.get("confirm_prices")):
        return {"status": 409,
                "error": "Some prices changed while this basket was open.",
                "price_changes": blockers["price_changes"],
                "note": "Show both numbers and send confirm_prices=true once "
                        "the shopper has agreed to the current ones."}

    if blockers["inactive"] or blockers["unavailable"]:
        return {"status": 409,
                "error": "Some items cannot be ordered right now.",
                "unavailable": blockers["unavailable"],
                "inactive": blockers["inactive"]}

    # Prices confirmed: adopt the live ones so the order records what the
    # shopper actually agreed to, not the number they first saw.
    if blockers["price_changes"]:
        for item in items:
            product = products.get(_text(item.get("product_id")))
            if product:
                item["unit_price_cents"] = str(product.get("price_cents") or 0)

    charges = _tax_and_shipping_settings(base)
    summary = object_cart.checkout_totals(items, **charges)
    email = _text(request.get("customer_email"))

    if _truthy(request.get("preview")):
        # The whole breakdown, because a preview exists so somebody can
        # see what they are about to owe -- a subtotal alone would hide
        # exactly the two numbers this preview was extended to show.
        return {"ok": True, "preview": True, "cart_id": cart["id"],
                "subtotal_cents": summary["subtotal_cents"],
                "shipping_cents": summary["shipping_cents"],
                "shipping_free": summary["shipping_free"],
                "tax_cents": summary["tax_cents"],
                "total_cents": summary["total_cents"],
                "lines": summary["lines"],
                "price_changes": blockers["price_changes"]}

    if not email:
        return {"status": 400,
                "error": "An email address is needed to send the receipt to."}

    order_id = object_ids.new_uuid4()
    today = _text(request.get("today")) or date.today().isoformat()
    order_row = {
        "id": order_id,
        "doc_type": "sale",
        "number": f"WEB-{order_id[:8].upper()}",
        "customer_name": _text(request.get("customer_name")) or email,
        "customer_email": email,
        "currency": _text(cart.get("currency")) or "USD",
        "status": "draft",
        "order_date": today,
        # Subtotal is goods, per the schema's own definition (the sum of
        # the order's lines). Postage is not a line of this order, so it
        # cannot be in its subtotal -- it is in the total, which is what
        # the buyer owes.
        "subtotal_cents": str(summary["subtotal_cents"]),
        "tax_cents": str(summary["tax_cents"]),
        "total_cents": str(summary["total_cents"]),
        "notes": f"Web checkout [carts/{cart['id']}]",
        "owner_id": _text(cart.get("owner_id")),
    }
    if summary["shipping_cents"] and _has_field(base, "orders", "shipping_cents"):
        order_row["shipping_cents"] = str(summary["shipping_cents"])

    # The two merchandising fields, on the order the moment orders declares
    # them -- see the module docstring. Same schema-aware posture as
    # shipping_cents above: a missing column costs the packer a note (which
    # the cart still holds and this response still reports), a rejected
    # write would cost the shopper their order.
    notes = {"customer_note": _text(request.get("customer_note")),
             "gift_message": _text(request.get("gift_message"))}
    carried_on_cart = []
    for field, value in notes.items():
        if not value:
            continue
        if _has_field(base, "orders", field):
            order_row[field] = value
        else:
            carried_on_cart.append(field)

    object_records.create_collection_record(
        "orders", order_row, base_dir=base, actor=ACTOR)

    for line in summary["lines"]:
        object_records.create_collection_record(
            "order_lines",
            {
                "id": object_ids.new_uuid4(),
                "order_id": order_id,
                "product_id": line["product_id"],
                "description": line["description"],
                "quantity": line["quantity"],
                "unit_price_cents": str(line["unit_price_cents"]),
                "line_total_cents": str(line["line_total_cents"]),
                "owner_id": _text(cart.get("owner_id")),
            },
            base_dir=base, actor=ACTOR)

    # Stamp the cart BEFORE anything else can retry: the stamp is what
    # makes a second click a no-op rather than a second order.
    stamp = {"status": "checked_out", "checked_out_order_id": order_id,
             "customer_email": email,
             "customer_name": _text(request.get("customer_name"))}
    # Always on the cart, whether or not orders took them: this is where
    # the shopper typed them, and it is the only copy that survives a shop
    # whose orders schema has not caught up yet.
    stamp.update({field: value for field, value in notes.items() if value})
    object_records.update_collection_record(
        "carts", cart["id"], stamp, base_dir=base, actor=ACTOR)

    # From here on the sale is already made. Everything below is wrapped so
    # that an invoicing problem costs the shopper a pay link, not the order
    # they just placed -- see the module docstring.
    invoice_id = ""
    pay_path = ""
    invoice_error = ""
    try:
        invoice_id = object_ids.new_uuid4()
        due_days = _int(_setting(base, "billing.invoice_due_days", DEFAULT_DUE_DAYS),
                        DEFAULT_DUE_DAYS)
        # The token is minted HERE rather than left to
        # system_invoice_portal_link. That handler is a reaction --
        # post-commit and best-effort -- which is right for an invoice that
        # will be emailed, and useless to a shopper standing at the counter
        # now: THIS response is what has to carry the door. The handler only
        # mints when a token is absent, so a token that already exists is
        # something it tolerates rather than fights over.
        token = secrets.token_urlsafe(24)
        object_records.create_collection_record(
            "invoices",
            {
                "id": invoice_id,
                # Same number as the order: one human reference for the
                # whole sale, so "WEB-1A2B3C4D" means one thing to the
                # shopper, the packer and the bookkeeper alike.
                "number": f"WEB-{order_id[:8].upper()}",
                "customer_name": _text(request.get("customer_name")) or email,
                "customer_email": email,
                "currency": _text(cart.get("currency")) or "USD",
                "status": "sent",
                "issue_date": today,
                "due_date": (date.fromisoformat(today)
                             + timedelta(days=due_days)).isoformat(),
                # The invoice's subtotal IS the sum of its own lines --
                # goods plus the postage line below -- and its total is
                # that plus tax. Exactly the arithmetic the schema
                # describes, so invoice_totals restating it one day finds
                # the same numbers rather than an argument.
                "subtotal_cents": str(summary["subtotal_cents"]
                                      + summary["shipping_cents"]),
                "tax_cents": str(summary["tax_cents"]),
                "total_cents": str(summary["total_cents"]),
                # Provenance in notes, the house pattern: invoices carry no
                # generated_from column, so the marker goes where it can be
                # found again by anyone asking where this bill came from.
                "notes": f"Generated by {ACTOR} [orders/{order_id}]",
                "source_order_id": order_id,
                "portal_token": token,
                "owner_id": _text(cart.get("owner_id")),
            },
            # portal_token is schema read_only so no client can choose its
            # own; a server-side writer passes preserve_read_only, which is
            # exactly the narrow escape hatch that flag exists for.
            base_dir=base, actor=ACTOR, preserve_read_only=True)

        for line in summary["lines"]:
            object_records.create_collection_record(
                "invoice_lines",
                {
                    "id": object_ids.new_uuid4(),
                    "invoice_id": invoice_id,
                    "description": line["description"],
                    "quantity": line["quantity"],
                    "unit_price_cents": str(line["unit_price_cents"]),
                    "line_total_cents": str(line["line_total_cents"]),
                    "owner_id": _text(cart.get("owner_id")),
                },
                base_dir=base, actor=ACTOR)

        if summary["shipping_cents"]:
            # Postage as its own line, only when there is postage to
            # charge: a shop that does not post things must not grow a
            # "Shipping 0.00" row on every bill.
            shipping_line = {
                "id": object_ids.new_uuid4(),
                "invoice_id": invoice_id,
                "description": "Shipping",
                "quantity": "1",
                "unit_price_cents": str(summary["shipping_cents"]),
                "line_total_cents": str(summary["shipping_cents"]),
                "owner_id": _text(cart.get("owner_id")),
            }
            if charges["tax_shipping"] and charges["tax_rate_bps"]:
                # Where postage is taxable, the line says so and carries
                # its own share -- somebody auditing "was delivery taxed?"
                # should find the answer on the line, not have to
                # reverse-engineer it out of the invoice total.
                shipping_line["tax_rate_bps"] = str(charges["tax_rate_bps"])
                shipping_line["line_tax_cents"] = str(object_cart.tax_cents(
                    summary["shipping_cents"], charges["tax_rate_bps"]))
            object_records.create_collection_record(
                "invoice_lines", shipping_line, base_dir=base, actor=ACTOR)

        # The stamp is the join system_shop_fulfillment walks backwards:
        # a payment knows its invoice, and this is the only thing that says
        # which order that invoice was for.
        object_records.update_collection_record(
            "orders", order_id, {"invoice_id": invoice_id},
            base_dir=base, actor=ACTOR)
        pay_path = f"/pay/{token}"
    except Exception as exc:
        invoice_error = str(exc)[:200]

    # total_cents is the GRAND total: what the payer owes, including
    # postage and tax. A response whose "total" was the goods subtotal
    # would be the number a payment gets built from, and the shop would
    # quietly collect the wrong amount.
    result = {"ok": True, "order_id": order_id, "cart_id": cart["id"],
              "subtotal_cents": summary["subtotal_cents"],
              "shipping_cents": summary["shipping_cents"],
              "shipping_free": summary["shipping_free"],
              "tax_cents": summary["tax_cents"],
              "total_cents": summary["total_cents"],
              "lines": len(summary["lines"]),
              "status_of_order": "draft",
              "note": "order raised; stock moves and the order confirms when "
                      "payment arrives"}
    if carried_on_cart:
        # Reported, not swallowed. Somebody has to know that what the
        # shopper typed did not reach the order the packer will read.
        result["notes_on_cart_only"] = carried_on_cart
    if invoice_error:
        # Say what went wrong rather than returning a cheerful ok: somebody
        # has to raise this bill, and they can only do that if the failure
        # was reported instead of swallowed.
        result["invoice_error"] = invoice_error
        result["note"] = ("order raised, but the invoice could not be: "
                          f"{invoice_error}. The order stands and the invoice "
                          "can be raised again.")
    else:
        result["invoice_id"] = invoice_id
        result["pay_path"] = pay_path
    return result
