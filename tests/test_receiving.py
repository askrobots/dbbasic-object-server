"""Receipts: the document that says what turned up on the dock.

The properties worth holding here are the ones a shop discovers the hard
way, and most of them are the mirror of the shipping slice's. You cannot
receive more than was ordered, and being told so is only useful if the
refusal carries the numbers. Two deliveries against one purchase order
derive partial then received, each exactly once, without anybody typing a
status. A replayed event shelves nothing twice, because events ARE
replayed (object_change_dispatch promises at-least-once).

The ones that are NOT a mirror are the interesting ones. A rejection is
not a short delivery: rejected goods physically arrived, so the fact is
recorded, and no stock move is composed because they never entered stock.
A line that received nothing has to SAY why, or it is indistinguishable
from a half-typed row. And a `purchase` move carries unit_cost_cents from
the price we agreed to pay -- the cost on the way in that valuation
(and therefore COGS, see test_cogs_on_sale.py) will need and that cannot
be reconstructed later.
"""

import pathlib
import shutil

from conftest import stage_collection

import object_execution
import object_ids
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
RECEIVING_OBJECTS = PACKAGES / "app-receiving" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()


def setup_env(tmp_path, monkeypatch, *, settings=()):
    """A data dir with every collection this slice touches, and one object
    root holding the package's objects -- the shape an installed server
    actually has (every package installs into the same objects directory).
    """
    data_dir = tmp_path / "data"
    for pkg, name in (("app-receiving", "receipts"),
                      ("app-receiving", "receipt_lines"),
                      ("app-catalog", "products"), ("app-catalog", "locations"),
                      ("app-catalog", "stock_moves"),
                      ("app-orders", "orders"), ("app-orders", "order_lines")):
        stage_collection(data_dir, pkg, name)
    rows = "".join(f"s{i}\t{k}\t{v}\t\n" for i, (k, v) in enumerate(settings))
    stage_collection(data_dir, "app-settings", "app_settings", rows=rows)

    objects_root = tmp_path / "objects"
    shutil.copytree(RECEIVING_OBJECTS, objects_root, dirs_exist_ok=True)

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

def product(data_dir, product_id, name, cents=1200):
    return object_records.create_collection_record(
        "products",
        {"id": product_id, "name": name, "sku": product_id.upper(),
         "product_type": "physical", "price_cents": str(cents),
         "currency": "USD", "is_active": "true", "owner_id": "shop"},
        base_dir=data_dir)


def location(data_dir, location_id, name, kind="warehouse"):
    return object_records.create_collection_record(
        "locations",
        {"id": location_id, "name": name, "location_type": kind,
         "owner_id": "shop"},
        base_dir=data_dir)


def purchase_order(data_dir, order_id="po-1", *, status="confirmed",
                   number="PO-0001", doc_type="purchase", owner="shop",
                   **fields):
    record = {"id": order_id, "doc_type": doc_type, "number": number,
              "customer_name": "Kiln & Clay Ltd",
              "customer_email": "sales@kiln.test", "currency": "USD",
              "status": "draft", "order_date": "2026-07-01", "owner_id": owner}
    record.update({k: str(v) for k, v in fields.items()})
    created = object_records.create_collection_record("orders", record,
                                                      base_dir=data_dir)
    if status != "draft":
        # Through the ladder, the way a human would: draft is where every
        # order starts, purchase or sale.
        object_records.update_collection_record(
            "orders", order_id, {"status": "confirmed"},
            base_dir=data_dir, actor="test")
        if status != "confirmed":
            object_records.update_collection_record(
                "orders", order_id, {"status": status},
                base_dir=data_dir, actor="test")
        created = object_records.get_collection_record("orders", order_id,
                                                       base_dir=data_dir)
    return created


def order_line(data_dir, line_id, order_id="po-1", *, product_id="p1",
               description="Enamel Mug", quantity="10", cents=450):
    return object_records.create_collection_record(
        "order_lines",
        {"id": line_id, "order_id": order_id, "product_id": product_id,
         "description": description, "quantity": str(quantity),
         "unit_price_cents": str(cents),
         "line_total_cents": str(int(float(quantity) * cents)),
         "owner_id": "shop"},
        base_dir=data_dir)


def receipts(data_dir):
    return object_records.read_collection_records("receipts", base_dir=data_dir)


def receipt_lines(data_dir):
    return object_records.read_collection_records("receipt_lines",
                                                  base_dir=data_dir)


def purchases(data_dir):
    return [move for move in object_records.read_collection_records(
        "stock_moves", base_dir=data_dir) if move["reason"] == "purchase"]


def order_status(data_dir, order_id="po-1"):
    return object_records.get_collection_record("orders", order_id,
                                                base_dir=data_dir)["status"]


def receive(objects_root, **payload):
    return run(objects_root, "action_receive_goods", "POST", payload)


def open_receipt(data_dir, receipt_id="rec-open", order_id="po-1", *,
                 lines=()):
    """A receipt still being counted -- the pallet is on the floor and
    nobody has signed for it yet.

    Written directly, the way any trusted server-side write is:
    action_receive_goods signs its own receipts off in the same pass, so
    `open` is reachable in a test only through the raw path -- which is
    precisely the path hook_receipt_lines exists to guard.
    """
    receipt = object_records.create_collection_record(
        "receipts",
        {"id": receipt_id, "order_id": order_id, "status": "open",
         "received_on": "2026-07-20", "owner_id": "shop"},
        base_dir=data_dir)
    for index, (order_line_id, quantity) in enumerate(lines):
        object_records.create_collection_record(
            "receipt_lines",
            {"id": f"{receipt_id}-line-{index}", "receipt_id": receipt_id,
             "order_line_id": order_line_id, "product_id": "p1",
             "description": "Enamel Mug", "quantity_expected": str(quantity),
             "quantity_received": str(quantity), "quantity_rejected": "0",
             "owner_id": "shop"},
            base_dir=data_dir)
    return receipt


def hook(objects_root, record, action="create"):
    return run(objects_root, "hook_receipt_lines", "BEFORE_WRITE",
               {"action": action, "collection": "receipt_lines",
                "record": record})


def post_receipt(objects_root, receipt_id):
    """The dispatcher's own payload shape: EVENT verb, record_id, raw action."""
    return run(objects_root, "system_receipt_posting", "EVENT",
               {"event": "receipts.record.updated", "collection": "receipts",
                "record_id": receipt_id, "action": "update"})


def stocked_shop(tmp_path, monkeypatch, *, quantity="10", status="confirmed",
                 settings=None, doc_type="purchase"):
    if settings is None:
        settings = [("receiving.supplier_location", "loc-supplier"),
                    ("receiving.stock_location", "loc-shelf")]
    data_dir, objects_root = setup_env(tmp_path, monkeypatch, settings=settings)
    location(data_dir, "loc-shelf", "Shelf")
    location(data_dir, "loc-supplier", "Suppliers", kind="supplier")
    product(data_dir, "p1", "Enamel Mug")
    purchase_order(data_dir, status=status, doc_type=doc_type)
    order_line(data_dir, "po-line-1", quantity=quantity)
    return data_dir, objects_root


# --- the gate ------------------------------------------------------------------

def test_over_receiving_is_refused_with_all_three_numbers(tmp_path, monkeypatch):
    """"No" is not an answer a receiver can act on. Which of the two
    deliveries is wrong is only knowable from the numbers."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    first = receive(objects_root, order_id="po-1",
                    lines=[{"order_line_id": "po-line-1",
                            "quantity_received": "8"}])
    assert first["ok"]

    # A second delivery, still being counted, claiming three more.
    open_receipt(data_dir)
    refused = hook(objects_root, {"id": object_ids.new_uuid4(),
                                  "receipt_id": "rec-open",
                                  "order_line_id": "po-line-1",
                                  "quantity_received": "3",
                                  "quantity_rejected": "0"})
    assert refused["status"] == 409
    assert "ordered 10" in refused["error"]
    assert "already received 8" in refused["error"]
    assert "would make 11" in refused["error"]


def test_the_action_reports_over_receiving_before_writing_anything(
        tmp_path, monkeypatch):
    """The hook guards the generic write path; the action has to guard its
    own, because trusted server-side writes bypass hooks by design."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    receive(objects_root, order_id="po-1",
            lines=[{"order_line_id": "po-line-1", "quantity_received": "8"}])

    refused = receive(objects_root, order_id="po-1",
                      lines=[{"order_line_id": "po-line-1",
                              "quantity_received": "3"}])
    assert refused["status"] == 409
    assert refused["over_receive"][0] == {
        "order_line_id": "po-line-1", "description": "Enamel Mug",
        "ordered": "10", "already_received": "8", "asked_for": "3",
        "would_make": "11"}
    assert len(receipts(data_dir)) == 1         # nothing half-written


def test_a_settled_receipt_refuses_new_lines(tmp_path, monkeypatch):
    """A delivery is settled once it has been signed for: a carton found
    afterwards is a NEW receipt, not a rewrite of what the driver handed
    over."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    created = receive(objects_root, order_id="po-1",
                      lines=[{"order_line_id": "po-line-1",
                              "quantity_received": "4"}])

    refused = hook(objects_root, {"id": object_ids.new_uuid4(),
                                  "receipt_id": created["receipt_id"],
                                  "order_line_id": "po-line-1",
                                  "quantity_received": "1",
                                  "quantity_rejected": "0"})
    assert refused["status"] == 409
    assert "already received" in refused["error"]
    assert "NEW receipt" in refused["error"]


def test_a_zero_received_line_with_no_note_is_refused(tmp_path, monkeypatch):
    """A blank zero is indistinguishable from a row somebody has not
    finished typing, and the two need opposite answers when the supplier's
    invoice arrives."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    open_receipt(data_dir)

    refused = hook(objects_root, {"id": object_ids.new_uuid4(),
                                  "receipt_id": "rec-open",
                                  "order_line_id": "po-line-1",
                                  "quantity_received": "0",
                                  "quantity_rejected": "0"})
    assert refused["status"] == 400
    assert "says nothing about" in refused["error"]
    assert "short delivery" in refused["error"]

    # The same line WITH a note is exactly the short delivery this system
    # exists to record, and is allowed through.
    allowed = hook(objects_root, {"id": object_ids.new_uuid4(),
                                  "receipt_id": "rec-open",
                                  "order_line_id": "po-line-1",
                                  "quantity_received": "0",
                                  "quantity_rejected": "0",
                                  "discrepancy_note": "nothing on the pallet"})
    assert allowed is None or allowed == {}


def test_a_receipt_line_cannot_be_negative(tmp_path, monkeypatch):
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    open_receipt(data_dir)

    refused = hook(objects_root, {"id": object_ids.new_uuid4(),
                                  "receipt_id": "rec-open",
                                  "order_line_id": "po-line-1",
                                  "quantity_received": "-1",
                                  "quantity_rejected": "0"})
    assert refused["status"] == 400
    assert "negative" in refused["error"]


def test_lines_of_a_cancelled_receipt_do_not_consume_the_order(
        tmp_path, monkeypatch):
    """A receipt raised and then abandoned describes no goods -- being
    refused for re-checking-in a delivery after voiding a mistyped receipt
    would be the paperwork punishing the shop for doing the right thing."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    open_receipt(data_dir, lines=[("po-line-1", "10")])
    object_records.update_collection_record(
        "receipts", "rec-open", {"status": "cancelled"},
        base_dir=data_dir, actor="test")

    again = receive(objects_root, order_id="po-1")
    assert again["ok"] and again["lines"] == 1
    assert again["receipt_lines"][0]["quantity_received"] == "10"


# --- partial receipt is a count, not a flag -------------------------------------

def test_a_short_delivery_records_the_discrepancy_and_leaves_the_po_partial(
        tmp_path, monkeypatch):
    """Eight of the ten arrived. The PO is partial, the shortfall is still
    outstanding, and the note saying why survives on the line."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    created = receive(objects_root, order_id="po-1",
                      supplier_reference="DN-77812",
                      lines=[{"order_line_id": "po-line-1",
                              "quantity_received": "8",
                              "discrepancy_note": "two short, supplier "
                                                  "back-ordering"}])
    assert created["still_outstanding"] == "2"

    result = post_receipt(objects_root, created["receipt_id"])
    assert result["order_status"] == "partial"
    assert order_status(data_dir) == "partial"
    assert result["moved"] == 1
    assert purchases(data_dir)[0]["quantity"] == "8"

    line = receipt_lines(data_dir)[0]
    assert line["quantity_expected"] == "10"
    assert line["quantity_received"] == "8"
    assert "back-ordering" in line["discrepancy_note"]
    receipt = object_records.get_collection_record(
        "receipts", created["receipt_id"], base_dir=data_dir)
    assert receipt["supplier_reference"] == "DN-77812"


def test_a_second_receipt_completes_the_purchase_order(tmp_path, monkeypatch):
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    first = receive(objects_root, order_id="po-1",
                    lines=[{"order_line_id": "po-line-1",
                            "quantity_received": "8"}])
    result = post_receipt(objects_root, first["receipt_id"])
    assert result["order_status"] == "partial"

    second = receive(objects_root, order_id="po-1")
    # The default is everything still outstanding: the remaining two.
    assert second["receipt_lines"][0]["quantity_received"] == "2"
    result = post_receipt(objects_root, second["receipt_id"])
    assert result["order_status"] == "received"
    assert result["order_status_changed"] is True
    assert order_status(data_dir) == "received"

    # Derived exactly once each: a second pass over the same facts changes
    # nothing and says so by NOT reporting a change.
    again = post_receipt(objects_root, second["receipt_id"])
    assert again["order_status"] == "received"
    assert "order_status_changed" not in again

    assert len(purchases(data_dir)) == 2
    assert sorted(move["quantity"] for move in purchases(data_dir)) == ["2", "8"]


def test_a_replayed_received_event_shelves_nothing_twice(tmp_path, monkeypatch):
    """Events are replayed by design -- the change dispatcher promises
    at-least-once. The per-line provenance marker is what makes that safe."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    created = receive(objects_root, order_id="po-1")
    first = post_receipt(objects_root, created["receipt_id"])
    assert first["moved"] == 1

    again = post_receipt(objects_root, created["receipt_id"])
    assert again["moved"] == 0
    assert len(purchases(data_dir)) == 1
    marker = f"receipts/{created['receipt_id']}:line/"
    assert marker in purchases(data_dir)[0]["reference"]


def test_a_rejected_quantity_is_recorded_and_moves_no_stock(
        tmp_path, monkeypatch):
    """Rejected goods arrived and are going back on the van. They never
    entered stock, so no move is composed -- but the fact is kept, because
    that is what the argument with the supplier is made of."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    created = receive(objects_root, order_id="po-1",
                      lines=[{"order_line_id": "po-line-1",
                              "quantity_received": "7",
                              "quantity_rejected": "3",
                              "discrepancy_note": "three cartons crushed"}])
    assert created["rejected"] == "3"
    post_receipt(objects_root, created["receipt_id"])

    # Seven on the shelf, not ten: the rejection moved nothing.
    assert len(purchases(data_dir)) == 1
    assert purchases(data_dir)[0]["quantity"] == "7"

    line = receipt_lines(data_dir)[0]
    assert line["quantity_rejected"] == "3"
    assert "crushed" in line["discrepancy_note"]
    # And the shortfall is still owed: a rejection is not a receipt.
    assert order_status(data_dir) == "partial"


def test_a_wholly_rejected_line_moves_nothing_at_all(tmp_path, monkeypatch):
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    created = receive(objects_root, order_id="po-1",
                      lines=[{"order_line_id": "po-line-1",
                              "quantity_received": "0",
                              "quantity_rejected": "10",
                              "discrepancy_note": "whole pallet water "
                                                  "damaged"}])
    result = post_receipt(objects_root, created["receipt_id"])
    assert result["moved"] == 0
    assert purchases(data_dir) == []
    # Nothing arrived that we kept, so the PO's status is untouched: the
    # facts say nothing yet, and a machine that knows less than a human
    # must not drag the row backwards.
    assert order_status(data_dir) == "confirmed"


def test_the_purchase_move_carries_the_price_we_agreed_to_pay(
        tmp_path, monkeypatch):
    """unit_cost_cents on the way IN is what FIFO or weighted-average
    valuation will consume, and it cannot be reconstructed once the
    supplier's price list moves on (see tests/test_cogs_on_sale.py)."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    created = receive(objects_root, order_id="po-1")
    post_receipt(objects_root, created["receipt_id"])

    move = purchases(data_dir)[0]
    assert move["unit_cost_cents"] == "450"      # order_line.unit_price_cents
    assert move["from_location_id"] == "loc-supplier"
    assert move["to_location_id"] == "loc-shelf"
    assert "PO-0001" in move["reference"]


def test_a_missing_stock_location_still_lets_the_receipt_stand(
        tmp_path, monkeypatch):
    """A missing location must not cost somebody the record that goods
    arrived: the gap is reported, the receipt and the derived status hold."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch, settings=[])
    created = receive(objects_root, order_id="po-1")
    result = post_receipt(objects_root, created["receipt_id"])

    assert result["moved"] == 0
    assert "stock_location" in result["warning"]
    assert result["order_status"] == "received"
    assert len(receipts(data_dir)) == 1


def test_the_stock_location_falls_back_to_the_shops_own_setting(
        tmp_path, monkeypatch):
    """A one-warehouse shop has already told app-shop where its shelf is and
    should not have to say it twice."""
    data_dir, objects_root = stocked_shop(
        tmp_path, monkeypatch,
        settings=[("shop.stock_location", "loc-shelf")])
    created = receive(objects_root, order_id="po-1")
    result = post_receipt(objects_root, created["receipt_id"])

    assert result["moved"] == 1
    assert purchases(data_dir)[0]["to_location_id"] == "loc-shelf"


def test_a_cancelled_purchase_order_is_never_touched_by_posting(
        tmp_path, monkeypatch):
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    created = receive(objects_root, order_id="po-1")
    object_records.update_collection_record(
        "orders", "po-1", {"status": "cancelled"},
        base_dir=data_dir, actor="test")

    result = post_receipt(objects_root, created["receipt_id"])
    assert result["skipped"] == "order is cancelled"
    assert purchases(data_dir) == []
    assert order_status(data_dir) == "cancelled"


# --- creating the receipt --------------------------------------------------------

def test_receiving_against_a_sales_order_is_refused_in_words(
        tmp_path, monkeypatch):
    """Goods leave on a shipment and arrive on a receipt. Somebody who got
    here with an SO has a model of the system worth correcting on the
    spot."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch,
                                          doc_type="sale")
    refused = receive(objects_root, order_id="po-1")
    assert refused["status"] == 409
    assert "sales order" in refused["error"]
    assert "shipment" in refused["error"]
    assert receipts(data_dir) == []


def test_a_draft_purchase_order_cannot_be_received_against(tmp_path, monkeypatch):
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch, status="draft")
    refused = receive(objects_root, order_id="po-1")
    assert refused["status"] == 409
    assert "Commit before you receive" in refused["error"]


def test_an_unknown_order_is_a_404(tmp_path, monkeypatch):
    _, objects_root = stocked_shop(tmp_path, monkeypatch)
    assert receive(objects_root, order_id="nope")["status"] == 404
    assert receive(objects_root)["status"] == 400


def test_every_blocker_is_reported_at_once(tmp_path, monkeypatch):
    """Checkout-style: the person doing this is standing up, in the cold,
    with a driver waiting."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    refused = receive(
        objects_root, order_id="po-1",
        lines=[{"order_line_id": "po-line-1", "quantity_received": "99"},
               {"order_line_id": "no-such-line", "quantity_received": "1"},
               {"order_line_id": "po-line-1", "quantity_received": "0"},
               {"order_line_id": "po-line-1", "quantity_received": "-2"}])
    assert refused["status"] == 409
    assert refused["over_receive"] and refused["unknown_lines"] == ["no-such-line"]
    reasons = " ".join(entry["reason"] for entry in refused["bad_quantities"])
    assert "must say why" in reasons and "negative" in reasons
    assert receipts(data_dir) == []


def test_the_default_receipt_is_everything_still_outstanding(
        tmp_path, monkeypatch):
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    order_line(data_dir, "po-line-2", product_id="p1", description="Bowl",
               quantity="4")

    created = receive(objects_root, order_id="po-1")
    assert created["lines"] == 2
    assert {line["quantity_received"] for line in created["receipt_lines"]} == {
        "10", "4"}
    assert created["status_of_receipt"] == "received"
    assert "orders/po-1" in receipts(data_dir)[0]["notes"]


def test_a_fully_received_po_answers_ok_with_a_note_not_an_error(
        tmp_path, monkeypatch):
    """A double click must not 500, and must not raise an empty receipt."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    first = receive(objects_root, order_id="po-1")
    post_receipt(objects_root, first["receipt_id"])

    again = receive(objects_root, order_id="po-1")
    assert again["ok"] is True
    assert again["receipt_id"] == ""
    assert "nothing outstanding" in again["note"]
    assert len(receipts(data_dir)) == 1


# --- the paperwork ---------------------------------------------------------------

def sheet(objects_root, order_id="po-1", user_id="shop"):
    payload = {"order_id": order_id}
    if user_id:
        payload["_identity"] = {"user_id": user_id}
    return run(objects_root, "site_receiving_sheet", "GET", payload)


def test_the_receiving_sheet_shows_expected_quantities_and_prior_receipts(
        tmp_path, monkeypatch):
    """The dock is where the paperwork actually happens, and a second
    delivery against a partly-received PO is exactly when a receiver needs
    to see what already arrived."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    body = sheet(objects_root)["body"]
    assert "Enamel Mug" in body
    assert "PO-0001" in body
    assert "Kiln &amp; Clay Ltd" in body
    assert ">10<" in body                       # ordered, and expected today
    assert "No deliveries have been booked in" in body
    assert "@media print" in body

    created = receive(objects_root, order_id="po-1",
                      supplier_reference="DN-77812", received_on="2026-07-20",
                      lines=[{"order_line_id": "po-line-1",
                              "quantity_received": "8",
                              "quantity_rejected": "1",
                              "discrepancy_note": "one carton crushed"}])
    post_receipt(objects_root, created["receipt_id"])

    body = sheet(objects_root)["body"]
    assert "Already received" in body
    assert "DN-77812" in body and "2026-07-20" in body
    assert ">2<" in body                        # still expected today
    assert ">1<" in body                        # the rejected column


def test_the_receiving_sheet_asks_a_stranger_to_sign_in(tmp_path, monkeypatch):
    _, objects_root = stocked_shop(tmp_path, monkeypatch)
    body = sheet(objects_root, user_id="")["body"]
    assert "Sign in" in body
    assert "Enamel Mug" not in body


def test_the_receiving_sheet_refuses_a_sales_order_and_an_unknown_one(
        tmp_path, monkeypatch):
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch,
                                          doc_type="sale")
    result = sheet(objects_root)
    assert result["status"] == 404
    assert "sales order" in result["body"]
    assert "Traceback" not in result["body"]

    missing = sheet(objects_root, order_id="no-such-order")
    assert missing["status"] == 404
    assert "Traceback" not in missing["body"]
