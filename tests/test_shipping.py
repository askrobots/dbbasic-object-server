"""Shipments: the document that says what physically moved.

The properties worth holding here are the ones a shop discovers the hard
way. You cannot ship more than was ordered, and being told so is only
useful if the refusal carries the numbers. Two shipments against one order
derive partial then shipped, each exactly once, without anybody typing a
status. A replayed event moves no stock twice, because events ARE replayed
(object_change_dispatch promises at-least-once). A packing slip carries no
prices, ever, which is what makes every parcel gift-safe with no flag to
forget. And the zero-touch shop keeps working: payment still ships the
order and still moves the stock -- it just travels through the shipment
noun now.
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
SHIPPING_OBJECTS = PACKAGES / "app-shipping" / "objects"
SHOP_OBJECTS = PACKAGES / "app-shop" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

TOKEN = "sess-ship-1"


def setup_env(tmp_path, monkeypatch, *, settings=()):
    """A data dir with every collection this slice touches, and ONE object
    root holding both packages' objects.

    The merged root is not a convenience: it is what an installed server
    actually looks like (every package installs into the same objects
    directory), and it is the only way an object that calls a sibling by id
    -- system_shop_fulfillment reaching action_create_shipment -- resolves
    the way it will in production.
    """
    data_dir = tmp_path / "data"
    for pkg, name in (("app-shipping", "shipments"),
                      ("app-shipping", "shipment_lines"),
                      ("app-shop", "carts"), ("app-shop", "cart_items"),
                      ("app-catalog", "products"), ("app-catalog", "locations"),
                      ("app-catalog", "stock_moves"),
                      ("app-orders", "orders"), ("app-orders", "order_lines"),
                      ("app-invoices", "invoices"), ("app-invoices", "invoice_lines"),
                      ("app-payments", "payments")):
        stage_collection(data_dir, pkg, name)
    rows = "".join(f"s{i}\t{k}\t{v}\t\n" for i, (k, v) in enumerate(settings))
    stage_collection(data_dir, "app-settings", "app_settings", rows=rows)

    objects_root = tmp_path / "objects"
    for source in (SHIPPING_OBJECTS, SHOP_OBJECTS):
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
        {"id": location_id, "name": name, "location_type": kind, "owner_id": "shop"},
        base_dir=data_dir)


def order(data_dir, order_id="ord-1", *, status="confirmed", number="SO-0001",
          owner="shop", **fields):
    record = {"id": order_id, "doc_type": "sale", "number": number,
              "customer_name": "Ada Lovelace",
              "customer_email": "ada@example.test", "currency": "USD",
              "status": "draft", "order_date": "2026-07-01", "owner_id": owner}
    record.update({k: str(v) for k, v in fields.items()})
    created = object_records.create_collection_record("orders", record,
                                                      base_dir=data_dir)
    if status != "draft":
        # Through the ladder, the way a human would: draft is where every
        # order starts.
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


def order_line(data_dir, line_id, order_id="ord-1", *, product_id="p1",
               description="Enamel Mug", quantity="3", cents=1200):
    return object_records.create_collection_record(
        "order_lines",
        {"id": line_id, "order_id": order_id, "product_id": product_id,
         "description": description, "quantity": str(quantity),
         "unit_price_cents": str(cents),
         "line_total_cents": str(int(float(quantity) * cents)),
         "owner_id": "shop"},
        base_dir=data_dir)


def stock_in(data_dir, product_id, quantity, *, to="loc-shelf"):
    return object_records.create_collection_record(
        "stock_moves",
        {"id": object_ids.new_uuid4(), "product_id": product_id,
         "to_location_id": to, "quantity": str(quantity), "reason": "purchase",
         "occurred_at": "2026-06-01", "owner_id": "shop"},
        base_dir=data_dir)


def shipments(data_dir):
    return object_records.read_collection_records("shipments", base_dir=data_dir)


def shipment_lines(data_dir):
    return object_records.read_collection_records("shipment_lines",
                                                  base_dir=data_dir)


def sales(data_dir):
    return [move for move in object_records.read_collection_records(
        "stock_moves", base_dir=data_dir) if move["reason"] == "sale"]


def order_status(data_dir, order_id="ord-1"):
    return object_records.get_collection_record("orders", order_id,
                                                base_dir=data_dir)["status"]


def create_shipment(objects_root, **payload):
    return run(objects_root, "action_create_shipment", "POST", payload)


def hook(objects_root, record, action="create"):
    return run(objects_root, "hook_shipment_lines", "BEFORE_WRITE",
               {"action": action, "collection": "shipment_lines", "record": record})


def fulfil_shipment(objects_root, shipment_id):
    """The dispatcher's own payload shape: EVENT verb, record_id, raw action."""
    return run(objects_root, "system_order_fulfillment", "EVENT",
               {"event": "shipments.record.updated", "collection": "shipments",
                "record_id": shipment_id, "action": "update"})


def ship(data_dir, objects_root, shipment_id):
    """Take a shipment out of the door the way a packer would, then let the
    handler react to it."""
    for status in ("packed", "shipped"):
        object_records.update_collection_record(
            "shipments", shipment_id, {"status": status},
            base_dir=data_dir, actor="test")
    return fulfil_shipment(objects_root, shipment_id)


def stocked_shop(tmp_path, monkeypatch, *, quantity="3", auto=None,
                 status="confirmed"):
    settings = [("shop.stock_location", "loc-shelf"),
                ("shop.customer_location", "loc-customer")]
    if auto is not None:
        settings.append(("shop.auto_fulfill", auto))
    data_dir, objects_root = setup_env(tmp_path, monkeypatch, settings=settings)
    location(data_dir, "loc-shelf", "Shelf")
    location(data_dir, "loc-customer", "Customers", kind="customer")
    product(data_dir, "p1", "Enamel Mug")
    stock_in(data_dir, "p1", 20)
    order(data_dir, status=status)
    order_line(data_dir, "line-1", quantity=quantity)
    return data_dir, objects_root


# --- the gate ------------------------------------------------------------------

def test_over_shipping_is_refused_with_all_three_numbers(tmp_path, monkeypatch):
    """"No" is not an answer a packer can act on. Which of the two shipments
    is wrong is only knowable from the numbers."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    first = create_shipment(objects_root, order_id="ord-1",
                            lines=[{"order_line_id": "line-1", "quantity": "2"}])
    assert first["ok"]

    refused = hook(objects_root, {"id": object_ids.new_uuid4(),
                                  "shipment_id": first["shipment_id"],
                                  "order_line_id": "line-1", "quantity": "2"})
    assert refused["status"] == 409
    assert "ordered 3" in refused["error"]
    assert "already on shipments 2" in refused["error"]
    assert "would make 4" in refused["error"]


def test_the_action_reports_over_shipping_before_writing_anything(
        tmp_path, monkeypatch):
    """The hook guards the generic write path; the action has to guard its
    own, because trusted server-side writes bypass hooks by design."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    create_shipment(objects_root, order_id="ord-1",
                    lines=[{"order_line_id": "line-1", "quantity": "2"}])

    refused = create_shipment(objects_root, order_id="ord-1",
                              lines=[{"order_line_id": "line-1", "quantity": "2"}])
    assert refused["status"] == 409
    assert refused["over_ship"][0] == {
        "order_line_id": "line-1", "description": "Enamel Mug",
        "ordered": "3", "already_on_shipments": "2", "asked_for": "2",
        "would_make": "4"}
    assert len(shipments(data_dir)) == 1        # nothing half-written


def test_a_shipped_shipment_refuses_new_lines(tmp_path, monkeypatch):
    """A manifest is settled once it leaves the dock: a forgotten item is a
    NEW shipment, not a rewrite of what the courier already took."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    created = create_shipment(objects_root, order_id="ord-1",
                              lines=[{"order_line_id": "line-1", "quantity": "1"}])
    ship(data_dir, objects_root, created["shipment_id"])

    refused = hook(objects_root, {"id": object_ids.new_uuid4(),
                                  "shipment_id": created["shipment_id"],
                                  "order_line_id": "line-1", "quantity": "1"})
    assert refused["status"] == 409
    assert "already shipped" in refused["error"]


def test_a_shipment_line_must_ship_something(tmp_path, monkeypatch):
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    created = create_shipment(objects_root, order_id="ord-1",
                              lines=[{"order_line_id": "line-1", "quantity": "1"}])
    refused = hook(objects_root, {"id": object_ids.new_uuid4(),
                                  "shipment_id": created["shipment_id"],
                                  "order_line_id": "line-1", "quantity": "0"})
    assert refused["status"] == 400
    assert "positive quantity" in refused["error"]


def test_lines_of_a_lost_shipment_do_not_consume_the_order(tmp_path, monkeypatch):
    """The goods never arrived, so the order still owes them -- being refused
    for re-sending a lost parcel would be the paperwork punishing the shop
    for doing the right thing."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    created = create_shipment(objects_root, order_id="ord-1")
    ship(data_dir, objects_root, created["shipment_id"])
    object_records.update_collection_record(
        "shipments", created["shipment_id"], {"status": "lost"},
        base_dir=data_dir, actor="test")

    again = create_shipment(objects_root, order_id="ord-1")
    assert again["ok"] and again["lines"] == 1
    assert again["shipment_lines"][0]["quantity"] == "3"


# --- partial fulfillment is a count, not a flag ---------------------------------

def test_two_shipments_derive_partial_then_shipped_exactly_once_each(
        tmp_path, monkeypatch):
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)

    first = create_shipment(objects_root, order_id="ord-1",
                            lines=[{"order_line_id": "line-1", "quantity": "2"}])
    result = ship(data_dir, objects_root, first["shipment_id"])
    assert result["order_status"] == "partial"
    assert order_status(data_dir) == "partial"
    assert result["moved"] == 1

    second = create_shipment(objects_root, order_id="ord-1")
    assert second["shipment_lines"][0]["quantity"] == "1"   # the remainder
    result = ship(data_dir, objects_root, second["shipment_id"])
    assert result["order_status"] == "shipped"
    assert order_status(data_dir) == "shipped"

    assert len(sales(data_dir)) == 2
    assert sorted(move["quantity"] for move in sales(data_dir)) == ["1", "2"]


def test_a_replayed_shipped_event_moves_nothing_twice(tmp_path, monkeypatch):
    """Events are replayed by design -- the change dispatcher promises
    at-least-once. The per-line provenance marker is what makes that safe."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    created = create_shipment(objects_root, order_id="ord-1")
    first = ship(data_dir, objects_root, created["shipment_id"])
    assert first["moved"] == 1

    again = fulfil_shipment(objects_root, created["shipment_id"])
    assert again["moved"] == 0
    assert len(sales(data_dir)) == 1
    marker = f"shipments/{created['shipment_id']}:line/"
    assert marker in sales(data_dir)[0]["reference"]


def test_every_shipment_delivered_makes_the_order_delivered(tmp_path, monkeypatch):
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    created = create_shipment(objects_root, order_id="ord-1")
    ship(data_dir, objects_root, created["shipment_id"])
    object_records.update_collection_record(
        "shipments", created["shipment_id"], {"status": "delivered"},
        base_dir=data_dir, actor="test")

    result = fulfil_shipment(objects_root, created["shipment_id"])
    assert result["order_status"] == "delivered"
    assert len(sales(data_dir)) == 1            # delivery moves nothing new


def test_a_cancelled_order_is_never_touched_by_fulfillment(tmp_path, monkeypatch):
    """Fulfillment must not resurrect an order somebody called off -- a
    parcel sent against it is a mistake to reverse, not a status to derive."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    created = create_shipment(objects_root, order_id="ord-1")
    object_records.update_collection_record(
        "orders", "ord-1", {"status": "cancelled"}, base_dir=data_dir, actor="test")

    result = ship(data_dir, objects_root, created["shipment_id"])
    assert result["skipped"] == "order is cancelled"
    assert sales(data_dir) == []
    assert order_status(data_dir) == "cancelled"


def test_a_missing_stock_location_still_lets_the_parcel_go(tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Enamel Mug")
    order(data_dir)
    order_line(data_dir, "line-1")

    created = create_shipment(objects_root, order_id="ord-1")
    result = ship(data_dir, objects_root, created["shipment_id"])
    assert result["moved"] == 0
    assert "shop.stock_location" in result["warning"]
    assert result["order_status"] == "shipped"   # the box still went out


# --- creating the shipment ------------------------------------------------------

def test_the_default_shipment_is_everything_still_unshipped(tmp_path, monkeypatch):
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    order_line(data_dir, "line-2", product_id="p1", description="Bowl",
               quantity="1")

    created = create_shipment(objects_root, order_id="ord-1")
    assert created["lines"] == 2
    assert {line["quantity"] for line in created["shipment_lines"]} == {"3", "1"}
    assert created["status_of_shipment"] == "open"


def test_the_address_is_stamped_when_the_box_is_made(tmp_path, monkeypatch):
    """An address is what it was when the box left; re-reading the customer
    record later would rewrite where last month's parcel went."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    created = create_shipment(objects_root, order_id="ord-1")
    shipment = object_records.get_collection_record(
        "shipments", created["shipment_id"], base_dir=data_dir)
    assert shipment["ship_to_name"] == "Ada Lovelace"
    assert "orders/ord-1" in shipment["notes"]

    object_records.update_collection_record(
        "orders", "ord-1", {"customer_name": "Ada Byron"},
        base_dir=data_dir, actor="test")
    unchanged = object_records.get_collection_record(
        "shipments", created["shipment_id"], base_dir=data_dir)
    assert unchanged["ship_to_name"] == "Ada Lovelace"


def test_a_fully_shipped_order_answers_ok_with_a_note_not_an_error(
        tmp_path, monkeypatch):
    """A double click must not 500, and must not raise an empty parcel."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    first = create_shipment(objects_root, order_id="ord-1")
    ship(data_dir, objects_root, first["shipment_id"])

    again = create_shipment(objects_root, order_id="ord-1")
    assert again["ok"] is True
    assert again["shipment_id"] == ""
    assert "nothing left to ship" in again["note"]
    assert len(shipments(data_dir)) == 1


def test_a_draft_order_cannot_be_packed(tmp_path, monkeypatch):
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch, status="draft")
    refused = create_shipment(objects_root, order_id="ord-1")
    assert refused["status"] == 409
    assert "Commit before you pack" in refused["error"]


def test_an_unknown_order_is_a_404(tmp_path, monkeypatch):
    _, objects_root = stocked_shop(tmp_path, monkeypatch)
    assert create_shipment(objects_root, order_id="nope")["status"] == 404
    assert create_shipment(objects_root)["status"] == 400


def test_every_blocker_is_reported_at_once(tmp_path, monkeypatch):
    """Checkout-style: revealing problems one at a time is how a warehouse
    screen gets abandoned in favour of a spreadsheet."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    refused = create_shipment(
        objects_root, order_id="ord-1",
        lines=[{"order_line_id": "line-1", "quantity": "9"},
               {"order_line_id": "no-such-line", "quantity": "1"},
               {"order_line_id": "line-1", "quantity": "0"}])
    assert refused["status"] == 409
    assert refused["over_ship"] and refused["unknown_lines"] == ["no-such-line"]
    assert refused["bad_quantities"][0]["order_line_id"] == "line-1"
    assert shipments(data_dir) == []


# --- the paperwork --------------------------------------------------------------

def slip(objects_root, shipment_id):
    return run(objects_root, "site_packing_slip", "GET",
               {"shipment_id": shipment_id})


def test_the_packing_slip_says_what_is_in_the_box_and_never_what_it_cost(
        tmp_path, monkeypatch):
    """Pricelessness by construction is what makes every parcel gift-safe --
    a flag somebody forgets to tick is how a present arrives with the
    amount paid stapled to it."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    created = create_shipment(objects_root, order_id="ord-1",
                              lines=[{"order_line_id": "line-1", "quantity": "2"}])
    body = slip(objects_root, created["shipment_id"])["body"]

    assert "Enamel Mug" in body
    assert ">2<" in body                        # the quantity cell
    assert "SO-0001" in body
    assert "Ada Lovelace" in body

    assert "$" not in body
    assert "1200" not in body and "12.00" not in body
    assert "unit_price" not in body and "_cents" not in body
    assert "@media print" in body


def test_the_slip_prints_the_note_and_the_gift_message_when_they_exist(
        tmp_path, monkeypatch):
    """Read with .get: the merchandising slice that adds these two fields to
    orders has not landed, and a slip that crashed on an older order would
    be a page breaking itself while waiting for a feature."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    created = create_shipment(objects_root, order_id="ord-1")
    assert "Gift message" not in slip(objects_root, created["shipment_id"])["body"]

    object_records.update_collection_record(
        "orders", "ord-1",
        {"customer_note": "Leave with the neighbour",
         "gift_message": "Happy birthday, Ada"},
        base_dir=data_dir, actor="test")
    body = slip(objects_root, created["shipment_id"])["body"]
    assert "Leave with the neighbour" in body
    assert "Happy birthday, Ada" in body
    assert "$" not in body


def test_an_unknown_shipment_is_a_friendly_404_not_a_traceback(
        tmp_path, monkeypatch):
    _, objects_root = stocked_shop(tmp_path, monkeypatch)
    result = slip(objects_root, "no-such-shipment")
    assert result["status"] == 404
    assert result["content_type"].startswith("text/html")
    assert "Traceback" not in result["body"]


def pick_list(objects_root, user_id="shop"):
    payload = {"_identity": {"user_id": user_id}} if user_id else {}
    return run(objects_root, "site_pick_list", "GET", payload)


def test_the_pick_list_groups_two_orders_into_one_walk_to_the_shelf(
        tmp_path, monkeypatch):
    """A picker walks the room once: two orders wanting mugs is one trip
    carrying five, not two trips."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    order(data_dir, "ord-2", number="SO-0002", order_date="2026-06-20")
    order_line(data_dir, "line-2", order_id="ord-2", quantity="2")

    body = pick_list(objects_root)["body"]
    assert body.count("Enamel Mug") == 1
    assert ">5<" in body                        # 3 + 2, one row
    assert "SO-0001" in body and "SO-0002" in body
    # Oldest first: SO-0002 was placed in June, so its date leads the row.
    assert "2026-06-20" in body


def test_what_is_already_on_a_shipment_leaves_the_pick_list(tmp_path, monkeypatch):
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch)
    create_shipment(objects_root, order_id="ord-1",
                    lines=[{"order_line_id": "line-1", "quantity": "2"}])
    body = pick_list(objects_root)["body"]
    assert ">1<" in body                        # only the remainder is left

    create_shipment(objects_root, order_id="ord-1")
    assert "Nothing is waiting to be picked" in pick_list(objects_root)["body"]


def test_the_pick_list_asks_a_stranger_to_sign_in(tmp_path, monkeypatch):
    _, objects_root = stocked_shop(tmp_path, monkeypatch)
    body = pick_list(objects_root, user_id="")["body"]
    assert "Sign in" in body
    assert "Enamel Mug" not in body


# --- the shop's zero-touch path, through the new noun ---------------------------

def paid_web_order(data_dir, objects_root):
    """An order paid for the way app-shop's checkout leaves it: an invoice
    the order points at, and a received payment against that invoice."""
    object_records.create_collection_record(
        "invoices",
        {"id": "inv-1", "number": "SO-0001", "customer_name": "Ada Lovelace",
         "customer_email": "ada@example.test", "status": "sent",
         "issue_date": "2026-07-01", "due_date": "2026-07-15",
         "subtotal_cents": "3600", "total_cents": "3600", "owner_id": "shop"},
        base_dir=data_dir)
    object_records.update_collection_record(
        "orders", "ord-1", {"invoice_id": "inv-1"},
        base_dir=data_dir, actor="test")
    return object_records.create_collection_record(
        "payments",
        {"id": object_ids.new_uuid4(), "invoice_id": "inv-1",
         "amount_cents": "3600", "method": "card", "received_on": "2026-07-02",
         "status": "received", "owner_id": "shop"},
        base_dir=data_dir)


def fulfil_payment(objects_root, payment):
    return run(objects_root, "system_shop_fulfillment", "EVENT",
               {"collection": "payments", "record": payment})


def test_auto_fulfill_ships_the_whole_order_and_moves_the_stock(
        tmp_path, monkeypatch):
    """The proven chain, unchanged from a shopper's point of view -- there
    is simply a document now saying what went out."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch, status="draft")
    payment = paid_web_order(data_dir, objects_root)

    result = fulfil_payment(objects_root, payment)
    assert result["confirmed"] and result["shipped"] is True
    assert result["moved"] == 1

    shipment = shipments(data_dir)[0]
    assert shipment["status"] == "shipped"
    assert shipment["order_id"] == "ord-1"
    assert len(shipment_lines(data_dir)) == 1
    assert sales(data_dir)[0]["quantity"] == "3"
    assert sales(data_dir)[0]["from_location_id"] == "loc-shelf"
    assert order_status(data_dir) == "shipped"


def test_auto_fulfill_off_confirms_the_order_and_ships_nothing(
        tmp_path, monkeypatch):
    """A shop with a packing bench: the order lands on the pick list and a
    human decides what goes in which box."""
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch, auto="false",
                                          status="draft")
    payment = paid_web_order(data_dir, objects_root)

    result = fulfil_payment(objects_root, payment)
    assert result["confirmed"] and result["shipped"] is False
    assert "auto_fulfill is off" in result["note"]
    assert shipments(data_dir) == [] and sales(data_dir) == []
    assert order_status(data_dir) == "confirmed"
    assert "Enamel Mug" in pick_list(objects_root)["body"]


def test_a_replayed_payment_raises_no_second_parcel(tmp_path, monkeypatch):
    data_dir, objects_root = stocked_shop(tmp_path, monkeypatch, status="draft")
    payment = paid_web_order(data_dir, objects_root)
    fulfil_payment(objects_root, payment)

    again = fulfil_payment(objects_root, payment)
    assert again["skipped"] == "order already shipped"
    assert len(shipments(data_dir)) == 1
    assert len(sales(data_dir)) == 1
