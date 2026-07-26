"""Drop-shipping: the composition test the whole fulfillment build was
measured against.

plan/fulfillment-logistics-spec.md set it in as many words -- "a sale
order flagged `fulfillment: dropship` produces a linked PURCHASE order to
the vendor carrying the customer's ship-to; receiving is skipped (goods
never touch our shelf); margin is already a read because both orders
carry money. If the shipment model can't express this cleanly, the model
is wrong."

The model expresses it, and it does so with no new collection anywhere,
because the decision made in orders v1 -- sale and purchase orders share
one schema -- turns out to have already modelled a drop-ship as two rows
pointing at each other. That is what the first half of this file asserts:
one PO, confirmed, carrying the customer's ship-to onto the vendor's
document, linked both ways, refusing a second PO and refusing an order
whose goods have already come off our shelf.

**The stock rule is the test that matters**, and it has two halves that
this file deliberately reports differently, because they are in different
states and pretending otherwise would be the dishonest kind of green.

The half that is done: the drop-ship flow itself moves no stock. No
handler fires, no move is composed, and the shelf is provably unchanged
from end to end.

The half that is NOT: app-shipping's system_order_fulfillment composes a
`sale` move for every shipment line the moment a shipment reaches
`shipped`, and it does not look at fulfillment_source -- so the vendor's
dispatch, recorded as a shipment against the sale order the way the spec
describes, would decrement a shelf that never held the goods. The order
carries the fact the handler needs; the handler does not read it. That is
one condition in a package the disputes/drop-ship slice does not own, so
it is a STRICT XFAIL here rather than a hack in a package that is not
ours -- the same posture tests/test_cogs_on_sale.py takes toward the
missing COGS journal. The day somebody adds the condition, the acceptance
test is already written and this file goes red if the fix does not match
what was specified.
"""

import pathlib
import shutil

import pytest
from conftest import stage_collection

import object_execution
import object_ids
import object_records
import object_stock
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
ORDERS_OBJECTS = PACKAGES / "app-orders" / "objects"
SHIPPING_OBJECTS = PACKAGES / "app-shipping" / "objects"
RECEIVING_OBJECTS = PACKAGES / "app-receiving" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()


def setup_env(tmp_path, monkeypatch, *, settings=()):
    """A data dir with every collection this slice touches, and ONE object
    root holding app-orders' objects beside app-shipping's and
    app-receiving's.

    The merged root is what an installed server actually looks like, and
    it is the only way the two gap tests at the bottom can fire the REAL
    handlers rather than a description of them.
    """
    data_dir = tmp_path / "data"
    for pkg, name in (("app-orders", "orders"), ("app-orders", "order_lines"),
                      ("app-catalog", "products"), ("app-catalog", "locations"),
                      ("app-catalog", "stock_moves"),
                      ("app-contacts", "contacts"),
                      ("app-shipping", "shipments"),
                      ("app-shipping", "shipment_lines"),
                      ("app-receiving", "receipts"),
                      ("app-receiving", "receipt_lines")):
        stage_collection(data_dir, pkg, name)
    rows = "".join(f"s{i}\t{k}\t{v}\t\n" for i, (k, v) in enumerate(settings))
    stage_collection(data_dir, "app-settings", "app_settings", rows=rows)

    objects_root = tmp_path / "objects"
    for source in (ORDERS_OBJECTS, SHIPPING_OBJECTS, RECEIVING_OBJECTS):
        shutil.copytree(source, objects_root, dirs_exist_ok=True)

    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_root))
    return data_dir, objects_root


def run(objects_root, object_id, method, payload):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            object_id, method=method, payload=payload),
        roots=[objects_root]).result


# --- fixtures shaped like a real shop -----------------------------------------

def product(data_dir, product_id="p1", name="Enamel Mug", cents=1200,
            cost_cents=None):
    record = {"id": product_id, "name": name, "sku": product_id.upper(),
              "product_type": "physical", "price_cents": str(cents),
              "currency": "USD", "is_active": "true", "owner_id": "shop"}
    if cost_cents is not None:
        record["cost_cents"] = str(cost_cents)
    return object_records.create_collection_record("products", record,
                                                   base_dir=data_dir)


def location(data_dir, location_id, name, kind="warehouse"):
    return object_records.create_collection_record(
        "locations",
        {"id": location_id, "name": name, "location_type": kind,
         "owner_id": "shop"},
        base_dir=data_dir)


def vendor(data_dir, vendor_id="ven-1"):
    """A supplier filed as a contact. full_name is COMPUTED on contacts,
    so it is not written here -- the name the purchase order carries is
    whatever app-contacts derives, which is the point of asking the
    collection rather than stamping a string."""
    return object_records.create_collection_record(
        "contacts",
        {"id": vendor_id, "first_name": "Kiln & Clay", "last_name": "Ltd",
         "email": "sales@kiln.test", "owner_id": "shop"},
        base_dir=data_dir)


def order(data_dir, order_id="ord-1", *, status="confirmed", number="SO-0001",
          owner="shop", **fields):
    record = {"id": order_id, "doc_type": "sale", "number": number,
              "customer_name": "Ada Lovelace",
              "customer_email": "ada@example.test", "currency": "USD",
              "status": "draft", "order_date": "2026-07-01", "owner_id": owner}
    record.update({k: str(v) for k, v in fields.items()})
    object_records.create_collection_record("orders", record,
                                            base_dir=data_dir)
    # Through the ladder, the way a human would: draft is where every order
    # starts.
    for step in ("confirmed", status):
        current = object_records.get_collection_record(
            "orders", order_id, base_dir=data_dir)["status"]
        if step not in ("draft", current):
            object_records.update_collection_record(
                "orders", order_id, {"status": step},
                base_dir=data_dir, actor="test")
    return object_records.get_collection_record("orders", order_id,
                                                base_dir=data_dir)


def order_line(data_dir, line_id, order_id="ord-1", *, product_id="p1",
               description="Enamel Mug", quantity="3", cents=1200):
    total = int(float(quantity) * cents)
    return object_records.create_collection_record(
        "order_lines",
        {"id": line_id, "order_id": order_id, "product_id": product_id,
         "description": description, "quantity": str(quantity),
         "unit_price_cents": str(cents), "line_total_cents": str(total),
         "tax_rate_bps": "0", "line_tax_cents": "0", "owner_id": "shop"},
        base_dir=data_dir)


def stock_in(data_dir, product_id="p1", quantity=20, *, to="loc-shelf"):
    return object_records.create_collection_record(
        "stock_moves",
        {"id": object_ids.new_uuid4(), "product_id": product_id,
         "to_location_id": to, "quantity": str(quantity), "reason": "purchase",
         "occurred_at": "2026-06-01", "owner_id": "shop"},
        base_dir=data_dir)


def moves(data_dir, reason=None):
    rows = object_records.read_collection_records("stock_moves",
                                                  base_dir=data_dir)
    if reason is None:
        return rows
    return [row for row in rows if row["reason"] == reason]


def on_shelf(data_dir, product_id="p1", location_id="loc-shelf"):
    return object_stock.quantity_at_location(product_id, location_id,
                                             base_dir=data_dir)


def orders_of(data_dir, doc_type=None):
    rows = object_records.read_collection_records("orders", base_dir=data_dir)
    if doc_type is None:
        return rows
    return [row for row in rows if row["doc_type"] == doc_type]


def lines_of(data_dir, order_id):
    return [row for row in object_records.read_collection_records(
        "order_lines", base_dir=data_dir) if row["order_id"] == order_id]


def dropship(objects_root, **payload):
    request = {"order_id": "ord-1", "vendor_id": "ven-1"}
    request.update(payload)
    return run(objects_root, "action_dropship_order", "POST", request)


def margin(objects_root, order_id="ord-1", user_id="shop"):
    return run(objects_root, "site_dropship_margin", "GET",
               {"order_id": order_id, "_identity": {"user_id": user_id}})


def shop_that_can_dropship(tmp_path, monkeypatch, *, cost_cents=None,
                           address="12 Analytical Way\nLondon"):
    """A shop with stock on the shelf and one confirmed sale order for
    three mugs, with somewhere for a parcel to go."""
    data_dir, objects_root = setup_env(
        tmp_path, monkeypatch,
        settings=[("shop.stock_location", "loc-shelf"),
                  ("shop.customer_location", "loc-customer")])
    location(data_dir, "loc-shelf", "Shelf")
    location(data_dir, "loc-customer", "Customers", kind="customer")
    product(data_dir, cost_cents=cost_cents)
    stock_in(data_dir)
    vendor(data_dir)
    order(data_dir, ship_to_address=address)
    order_line(data_dir, "line-1")
    return data_dir, objects_root


# --- one sale order spawns exactly one purchase order --------------------------

def test_a_dropship_order_spawns_one_po_carrying_the_customers_address(
        tmp_path, monkeypatch):
    """The whole shape in one assertion block: the vendor is the
    counterparty, the customer is the destination, and this is the only
    place in the repo where those are different people."""
    data_dir, objects_root = shop_that_can_dropship(tmp_path, monkeypatch)
    result = dropship(objects_root, vendor_price_cents="700",
                      today="2026-07-20")
    assert result["ok"] is True
    assert result["lines"] == 1

    purchases = orders_of(data_dir, "purchase")
    assert len(purchases) == 1
    po = purchases[0]
    assert po["id"] == result["purchase_order_id"]
    assert po["number"] == "PO-SO-0001"
    # Confirmed, not draft: the operator choosing a vendor IS the
    # commitment to buy, and a drop-ship PO in draft is a customer waiting
    # on a parcel nobody ordered.
    assert po["status"] == "confirmed"
    assert po["fulfillment_source"] == "dropship"
    assert po["vendor_id"] == "ven-1"
    # The counterparty is the vendor, named the way every other PO in this
    # repo names its supplier.
    assert po["customer_name"] == "Kiln & Clay Ltd"
    # ...and the goods go somewhere else entirely.
    assert po["ship_to_name"] == "Ada Lovelace"
    assert po["ship_to_address"] == "12 Analytical Way\nLondon"
    assert "orders/ord-1" in po["notes"]

    po_lines = lines_of(data_dir, po["id"])
    assert len(po_lines) == 1
    assert po_lines[0]["description"] == "Enamel Mug"
    assert po_lines[0]["quantity"] == "3"
    assert po_lines[0]["unit_price_cents"] == "700"
    assert po_lines[0]["line_total_cents"] == "2100"

    # Linked both ways, so either end answers on its own and no handler
    # ever has to join to find out what it is looking at.
    sale = object_records.get_collection_record("orders", "ord-1",
                                                base_dir=data_dir)
    assert sale["fulfillment_source"] == "dropship"
    assert sale["linked_order_id"] == po["id"]
    assert sale["vendor_id"] == "ven-1"
    assert po["linked_order_id"] == "ord-1"


def test_drop_shipping_the_same_order_twice_is_refused_with_the_po_that_exists(
        tmp_path, monkeypatch):
    """A duplicate PO is a duplicate commitment to buy, with a supplier on
    the other end who will happily send the goods twice."""
    data_dir, objects_root = shop_that_can_dropship(tmp_path, monkeypatch)
    first = dropship(objects_root, vendor_price_cents="700")
    assert first["ok"]

    refused = dropship(objects_root, vendor_price_cents="700")
    assert refused["status"] == 409
    assert "already drop-shipped" in refused["already_linked"]
    assert "PO-SO-0001" in refused["already_linked"]
    assert len(orders_of(data_dir, "purchase")) == 1


def test_a_purchase_order_cannot_itself_be_drop_shipped(tmp_path, monkeypatch):
    data_dir, objects_root = shop_that_can_dropship(tmp_path, monkeypatch)
    order(data_dir, "po-9", doc_type="purchase", number="PO-9",
          customer_name="Kiln & Clay Ltd")
    order_line(data_dir, "po-9-line", order_id="po-9")

    refused = dropship(objects_root, order_id="po-9")
    assert refused["status"] == 409
    assert "That is a purchase order" in refused["wrong_document"]


def test_every_blocker_is_reported_at_once(tmp_path, monkeypatch):
    """Whoever is doing this is usually choosing between two vendors with
    a customer waiting; revealing one problem at a time is how a screen
    gets abandoned."""
    data_dir, objects_root = shop_that_can_dropship(tmp_path, monkeypatch)
    object_records.update_collection_record(
        "orders", "ord-1", {"status": "cancelled"},
        base_dir=data_dir, actor="test")

    refused = dropship(objects_root, order_id="ord-1", vendor_id="")
    assert refused["status"] == 409
    assert "cancelled" in refused["order_status"]
    assert "has to name who is shipping it" in refused["missing_vendor"]
    assert orders_of(data_dir, "purchase") == []


# --- you cannot drop-ship what you already picked ------------------------------

def picked_from_stock(data_dir, order_id="ord-1", shipment_id="ship-1"):
    """A parcel packed and sent from our own shelf, with the `sale` move
    system_order_fulfillment composes for it. Written directly, the way a
    trusted server-side write is: this file is about what happens NEXT."""
    object_records.create_collection_record(
        "shipments",
        {"id": shipment_id, "order_id": order_id, "direction": "outbound",
         "status": "open", "service": "ground", "ship_to_name": "Ada Lovelace",
         "shipped_on": "2026-07-05", "owner_id": "shop"},
        base_dir=data_dir)
    object_records.create_collection_record(
        "shipment_lines",
        {"id": f"{shipment_id}-line-0", "shipment_id": shipment_id,
         "order_line_id": "line-1", "product_id": "p1",
         "description": "Enamel Mug", "quantity": "3", "owner_id": "shop"},
        base_dir=data_dir)
    object_records.create_collection_record(
        "stock_moves",
        {"id": object_ids.new_uuid4(), "product_id": "p1",
         "from_location_id": "loc-shelf", "to_location_id": "loc-customer",
         "quantity": "3", "reason": "sale",
         "reference": f"shipments/{shipment_id}:line/{shipment_id}-line-0 SO-0001",
         "occurred_at": "2026-07-05", "owner_id": "shop"},
        base_dir=data_dir)


def test_drop_shipping_an_order_with_stock_movement_is_refused(
        tmp_path, monkeypatch):
    """The goods left our shelf, so the sale is ours to fulfill. Raising a
    PO now would buy a second set of the same units, and the refusal names
    the moves so somebody can see whether they picked the wrong order or
    the wrong day."""
    data_dir, objects_root = shop_that_can_dropship(tmp_path, monkeypatch)
    picked_from_stock(data_dir)

    refused = dropship(objects_root, vendor_price_cents="700")
    assert refused["status"] == 409
    assert len(refused["stock_moved"]) == 1
    moved = refused["stock_moved"][0]
    assert moved["product_id"] == "p1"
    assert moved["quantity"] == "3"
    assert moved["reason"] == "sale"
    assert "shipments/ship-1:" in moved["reference"]
    assert refused["shipments"] == ["ship-1"]

    # Nothing half-written, and the sale order still says what it is.
    assert orders_of(data_dir, "purchase") == []
    assert object_records.get_collection_record(
        "orders", "ord-1", base_dir=data_dir)["fulfillment_source"] == "stock"


# --- THE composition test: a drop-ship order moves no stock --------------------

def test_a_dropship_order_moves_no_stock_end_to_end(tmp_path, monkeypatch):
    """The property the whole slice is judged on, asserted over the shelf
    itself rather than over a handler's return value.

    Twenty mugs on the shelf before, twenty after: not one move composed
    anywhere, no receipt raised, and the purchase order still sitting at
    confirmed because nothing on this server creates a receipt on its own.
    That last clause is why receiving is skipped BY CONSTRUCTION rather
    than by a gate -- system_receipt_posting only ever reacts to a
    `receipts` row a human raised.
    """
    data_dir, objects_root = shop_that_can_dropship(tmp_path, monkeypatch)
    before = on_shelf(data_dir)
    assert before == 20
    moves_before = len(moves(data_dir))

    result = dropship(objects_root, vendor_price_cents="700")
    assert result["ok"] is True
    assert "never touch our shelf" in result["note"]

    assert on_shelf(data_dir) == before
    assert len(moves(data_dir)) == moves_before
    assert moves(data_dir, "sale") == []
    # The one purchase move is the shelf being stocked in the fixture; the
    # drop-ship added none of its own.
    assert len(moves(data_dir, "purchase")) == 1

    # No receipt exists, so system_receipt_posting has nothing to react to
    # and the PO stays where the action put it.
    assert object_records.read_collection_records(
        "receipts", base_dir=data_dir) == []
    po = orders_of(data_dir, "purchase")[0]
    assert po["status"] == "confirmed"

    # And firing the receiving handler anyway -- the replay case -- finds
    # no receipt and moves nothing.
    replayed = run(objects_root, "system_receipt_posting", "EVENT",
                   {"event": "receipts.record.updated",
                    "collection": "receipts", "record_id": "no-such-receipt",
                    "action": "update"})
    assert replayed["ok"] is True
    assert on_shelf(data_dir) == before


# --- margin is a read over the two orders --------------------------------------

def test_margin_reads_from_the_two_linked_orders(tmp_path, monkeypatch):
    """The spec's claim was that margin needs no stored field because both
    orders carry money. This is that claim, checked: revenue off the sale
    order, cost off the purchase order, and object_billing.margin doing
    the arithmetic rather than a second implementation of it."""
    data_dir, objects_root = shop_that_can_dropship(tmp_path, monkeypatch)
    dropship(objects_root, vendor_price_cents="700")

    read = margin(objects_root)
    assert read["margin"] == {"revenue_minor": 3600, "cost_minor": 2100,
                              "gross_minor": 1500, "gross_pct": 41.67}
    assert read["currency"] == "USD"
    # Folded from the lines, because system_order_totals is a post-commit
    # reaction that has not run in this test -- and a margin quoted off a
    # total that simply has not landed yet would be a confident wrong
    # answer at exactly the wrong moment.
    assert read["revenue_source"] == "folded"
    assert read["cost_source"] == "folded"
    assert "36.00 USD" in read["body"]
    assert "21.00 USD" in read["body"]
    assert "15.00 USD" in read["body"]


def test_margin_opens_from_either_end_of_the_pair(tmp_path, monkeypatch):
    """A link written both ways is a link you never have to search for."""
    data_dir, objects_root = shop_that_can_dropship(tmp_path, monkeypatch)
    created = dropship(objects_root, vendor_price_cents="700")

    from_sale = margin(objects_root, "ord-1")
    from_po = margin(objects_root, created["purchase_order_id"])
    assert from_sale["margin"] == from_po["margin"]
    assert from_po["sale_order_id"] == "ord-1"


def test_margin_prefers_the_stamped_total_when_the_handler_has_run(
        tmp_path, monkeypatch):
    """The document's own number wins when it exists: that is what a human
    sees on the order itself, and a page that quietly disagreed with the
    order it is about would be worse than no page."""
    data_dir, objects_root = shop_that_can_dropship(tmp_path, monkeypatch)
    created = dropship(objects_root, vendor_price_cents="700")
    for order_id, total in (("ord-1", "4000"),
                            (created["purchase_order_id"], "2500")):
        object_records.update_collection_record(
            "orders", order_id,
            {"subtotal_cents": total, "total_cents": total},
            base_dir=data_dir, actor="test")

    read = margin(objects_root)
    assert read["revenue_source"] == "stamped"
    assert read["margin"]["revenue_minor"] == 4000
    assert read["margin"]["cost_minor"] == 2500
    assert read["margin"]["gross_minor"] == 1500


def test_a_missing_vendor_price_is_warned_about_rather_than_smoothed_over(
        tmp_path, monkeypatch):
    """A cost of zero reads as 100% margin and a cost copied from the sale
    price reads as none. Both are lies; saying the number is missing is
    not."""
    data_dir, objects_root = shop_that_can_dropship(tmp_path, monkeypatch)
    result = dropship(objects_root)
    assert result["ok"] is True
    assert result["unpriced_lines"] == ["line-1"]
    assert "no vendor price" in result["warning"]

    read = margin(objects_root)
    assert read["margin"]["cost_minor"] == 0
    assert read["margin"]["gross_pct"] == 100.0
    assert "reads as pure profit and is not" in read["body"]


def test_the_products_recorded_cost_is_used_before_giving_up(tmp_path,
                                                             monkeypatch):
    """A price we already wrote down is a fact; zero is not."""
    data_dir, objects_root = shop_that_can_dropship(tmp_path, monkeypatch,
                                                    cost_cents=650)
    result = dropship(objects_root)
    assert result["ok"] is True
    assert "warning" not in result
    po_lines = lines_of(data_dir, result["purchase_order_id"])
    assert po_lines[0]["unit_price_cents"] == "650"


def test_margin_on_an_ordinary_order_says_what_is_missing_instead_of_zero(
        tmp_path, monkeypatch):
    """An order fulfilled from our own shelf has no vendor cost to set
    against it -- that needs inventory valuation, which this server does
    not do yet. Saying so beats printing a margin of 100%."""
    data_dir, objects_root = shop_that_can_dropship(tmp_path, monkeypatch)
    read = margin(objects_root)
    assert read["status"] == 409
    assert "not part of a drop-ship pair" in read["body"]


def test_margin_is_not_a_number_for_visitors(tmp_path, monkeypatch):
    """The page is addressed by an internal order id; public execute would
    make it an enumeration oracle for the whole margin book."""
    data_dir, objects_root = shop_that_can_dropship(tmp_path, monkeypatch)
    dropship(objects_root, vendor_price_cents="700")
    body = margin(objects_root, user_id="")["body"]
    assert "Please sign in" in body
    assert "36.00" not in body


# --- the halves that are NOT done, held as specifications ----------------------
#
# Both were written first as strict xfails, because each needed one
# condition in a package the drop-ship slice did not own, and hacking
# around a sibling package's handler would have been exactly the kind of
# "make it work by the data" a stock rule cannot afford. Writing the
# acceptance test first and leaving it red is the honest move: a green
# test over a shelf that silently decrements is worse than a red one.
#
# Both conditions have since been added -- app-shipping's fulfilment
# handler and app-receiving's check-in action each now read the order's
# fulfillment_source -- so these are ordinary passing tests. The history
# is left here because it is the argument for why they exist at all.

def shipped_by_the_vendor(data_dir, shipment_id="ship-ds"):
    """The vendor's dispatch, recorded the way plan/fulfillment-logistics-
    spec.md describes it: a shipment against the sale order.

    NOTE: the spec asks for `source: vendor` on that shipment, and
    app-shipping's shipments schema has no `source` field -- a second,
    smaller follow-up in the same package, named here so it is not
    rediscovered.
    """
    object_records.create_collection_record(
        "shipments",
        {"id": shipment_id, "order_id": "ord-1", "direction": "outbound",
         "status": "open", "service": "ground", "carrier": "Vendor",
         "ship_to_name": "Ada Lovelace", "owner_id": "shop"},
        base_dir=data_dir)
    object_records.create_collection_record(
        "shipment_lines",
        {"id": f"{shipment_id}-line-0", "shipment_id": shipment_id,
         "order_line_id": "line-1", "product_id": "p1",
         "description": "Enamel Mug", "quantity": "3", "owner_id": "shop"},
        base_dir=data_dir)
    for step in ("packed", "shipped"):
        object_records.update_collection_record(
            "shipments", shipment_id, {"status": step},
            base_dir=data_dir, actor="test")
    return shipment_id


def test_the_vendors_dispatch_must_not_move_our_stock(tmp_path, monkeypatch):
    """The specification for the missing condition, written as the
    acceptance test whoever adds it already has."""
    data_dir, objects_root = shop_that_can_dropship(tmp_path, monkeypatch)
    dropship(objects_root, vendor_price_cents="700")
    before = on_shelf(data_dir)

    shipment_id = shipped_by_the_vendor(data_dir)
    result = run(objects_root, "system_order_fulfillment", "EVENT",
                 {"event": "shipments.record.updated",
                  "collection": "shipments", "record_id": shipment_id,
                  "action": "update"})

    assert result["moved"] == 0
    assert moves(data_dir, "sale") == []
    assert on_shelf(data_dir) == before


def test_receiving_against_a_dropship_po_is_refused(tmp_path, monkeypatch):
    """The second specification. Weaker than the first -- it takes a
    deliberate human mistake to trigger, where the shipping gap fires on
    its own -- but the same one-line fix in the same shape."""
    data_dir, objects_root = shop_that_can_dropship(tmp_path, monkeypatch)
    created = dropship(objects_root, vendor_price_cents="700")
    po_line = lines_of(data_dir, created["purchase_order_id"])[0]
    before = on_shelf(data_dir)

    result = run(objects_root, "action_receive_goods", "POST",
                 {"order_id": created["purchase_order_id"],
                  "lines": [{"order_line_id": po_line["id"],
                             "quantity_received": "3"}]})

    assert result.get("status") == 409
    assert "drop-ship" in result.get("error", "")
    assert on_shelf(data_dir) == before
