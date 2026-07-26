"""Returns: the document that says goods may come back, and what happened
when they did.

The properties worth holding here are the ones a shop discovers the
expensive way. You cannot return what never left, and you cannot return
more than was shipped -- being told so is only useful if the refusal
carries the numbers, exactly as the over-ship gate does. Goods arriving
and goods being JUDGED are two different events: the parcel landing moves
no stock and no money, because a box on the dock could hold a mug in its
wrapping or the same mug in three pieces, and a human decides which.

Then the three that cost real money if they are wrong. Disposal composes
a stock move like restock does -- a different reason and no destination,
but a MOVE -- because goods leaving inventory with no trace is how
shrinkage hides. A refund is capped by app-payments' existing gate rather
than by a second opinion grown here, and the refusal that comes back is
the gate's own words. And a dispositioned return that is dispositioned
again moves nothing and refunds nothing: retries, double clicks and
replayed queue entries are ordinary, and a customer's money going back
twice is not.

EXPIRY IS DATA IN THIS SLICE, deliberately. expires_on is stamped when the
return is authorized and system_return_posting says `past_expiry` when the
goods land after it, so the person choosing restock-or-refund knows they
are outside the promise before they choose -- but nothing refuses a late
parcel and no pass sweeps stale RMAs to `expired`. Time-driven work
belongs with the tracking poll (plan/fulfillment-logistics-spec.md item
4). The tests below assert the stamp and the flag, and nothing more,
because there is deliberately nothing more.
"""

import pathlib
import shutil

from conftest import stage_collection

import object_execution
import object_ids
import object_records
import object_stock
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
RETURNS_OBJECTS = PACKAGES / "app-returns" / "objects"
SHIPPING_OBJECTS = PACKAGES / "app-shipping" / "objects"
PAYMENTS_OBJECTS = PACKAGES / "app-payments" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()


def setup_env(tmp_path, monkeypatch, *, settings=()):
    """A data dir with every collection this slice touches, and ONE object
    root holding all three packages' objects.

    The merged root is not a convenience: it is what an installed server
    actually looks like, and it is the only way the objects that call a
    sibling by id resolve the way they will in production --
    action_disposition_return asks app-payments' hook_refunds what a
    payment can give back rather than deciding for itself, and
    site_return_form hands everything to action_authorize_return.
    """
    data_dir = tmp_path / "data"
    for pkg, name in (("app-returns", "return_authorizations"),
                      ("app-shipping", "shipments"),
                      ("app-shipping", "shipment_lines"),
                      ("app-catalog", "products"), ("app-catalog", "locations"),
                      ("app-catalog", "stock_moves"),
                      ("app-orders", "orders"), ("app-orders", "order_lines"),
                      ("app-invoices", "invoices"),
                      ("app-payments", "payments"),
                      ("app-payments", "refunds")):
        stage_collection(data_dir, pkg, name)
    rows = "".join(f"s{i}\t{k}\t{v}\t\n" for i, (k, v) in enumerate(settings))
    stage_collection(data_dir, "app-settings", "app_settings", rows=rows)

    objects_root = tmp_path / "objects"
    for source in (RETURNS_OBJECTS, SHIPPING_OBJECTS, PAYMENTS_OBJECTS):
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
        {"id": location_id, "name": name, "location_type": kind,
         "owner_id": "shop"},
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
               description="Enamel Mug", quantity="3", cents=1200, tax_bps=0):
    total = int(float(quantity) * cents)
    return object_records.create_collection_record(
        "order_lines",
        {"id": line_id, "order_id": order_id, "product_id": product_id,
         "description": description, "quantity": str(quantity),
         "unit_price_cents": str(cents), "line_total_cents": str(total),
         "tax_rate_bps": str(tax_bps),
         "line_tax_cents": str(total * tax_bps // 10000),
         "owner_id": "shop"},
        base_dir=data_dir)


def stock_in(data_dir, product_id, quantity, *, to="loc-shelf"):
    return object_records.create_collection_record(
        "stock_moves",
        {"id": object_ids.new_uuid4(), "product_id": product_id,
         "to_location_id": to, "quantity": str(quantity), "reason": "purchase",
         "occurred_at": "2026-06-01", "owner_id": "shop"},
        base_dir=data_dir)


def paid(data_dir, *, cents=3600, order_id="ord-1", invoice_id="inv-1"):
    """An order paid for the way app-shop's checkout leaves it: an invoice
    the order points at, and a received payment against that invoice."""
    object_records.create_collection_record(
        "invoices",
        {"id": invoice_id, "number": "SO-0001", "customer_name": "Ada Lovelace",
         "customer_email": "ada@example.test", "status": "sent",
         "issue_date": "2026-07-01", "due_date": "2026-07-15",
         "subtotal_cents": str(cents), "total_cents": str(cents),
         "owner_id": "shop"},
        base_dir=data_dir)
    object_records.update_collection_record(
        "orders", order_id, {"invoice_id": invoice_id},
        base_dir=data_dir, actor="test")
    return object_records.create_collection_record(
        "payments",
        {"id": "pay-1", "invoice_id": invoice_id, "amount_cents": str(cents),
         "method": "card", "received_on": "2026-07-02", "status": "received",
         "owner_id": "shop"},
        base_dir=data_dir)


def shipments(data_dir):
    return object_records.read_collection_records("shipments",
                                                  base_dir=data_dir)


def shipment_lines(data_dir, shipment_id=None):
    rows = object_records.read_collection_records("shipment_lines",
                                                  base_dir=data_dir)
    if shipment_id is None:
        return rows
    return [row for row in rows if row["shipment_id"] == shipment_id]


def rmas(data_dir):
    return object_records.read_collection_records("return_authorizations",
                                                  base_dir=data_dir)


def refunds(data_dir):
    return object_records.read_collection_records("refunds", base_dir=data_dir)


def moves(data_dir, reason=None):
    rows = object_records.read_collection_records("stock_moves",
                                                  base_dir=data_dir)
    if reason is None:
        return rows
    return [row for row in rows if row["reason"] == reason]


def on_shelf(data_dir, product_id="p1", location_id="loc-shelf"):
    return object_stock.quantity_at_location(product_id, location_id,
                                             base_dir=data_dir)


def authorize(objects_root, **payload):
    return run(objects_root, "action_authorize_return", "POST", payload)


def disposition(objects_root, **payload):
    return run(objects_root, "action_disposition_return", "POST", payload)


def post_return(objects_root, shipment_id):
    """The dispatcher's own payload shape: EVENT verb, record_id, raw
    action."""
    return run(objects_root, "system_return_posting", "EVENT",
               {"event": "shipments.record.updated", "collection": "shipments",
                "record_id": shipment_id, "action": "update"})


def outbound_shipment(data_dir, order_id="ord-1", *, lines=(("line-1", "3"),),
                      status="delivered", shipment_id="ship-out"):
    """A parcel that already went, written directly the way a trusted
    server-side write is -- this suite is about what comes BACK, and
    re-proving app-shipping's outbound path here would only couple the two
    test files together."""
    object_records.create_collection_record(
        "shipments",
        {"id": shipment_id, "order_id": order_id, "direction": "outbound",
         "status": "open", "service": "ground", "ship_to_name": "Ada Lovelace",
         "shipped_on": "2026-07-03", "owner_id": "shop"},
        base_dir=data_dir)
    for index, (order_line_id, quantity) in enumerate(lines):
        object_records.create_collection_record(
            "shipment_lines",
            {"id": f"{shipment_id}-line-{index}", "shipment_id": shipment_id,
             "order_line_id": order_line_id, "product_id": "p1",
             "description": "Enamel Mug", "quantity": str(quantity),
             "owner_id": "shop"},
            base_dir=data_dir)
    for step in ("packed", "shipped", status):
        current = object_records.get_collection_record(
            "shipments", shipment_id, base_dir=data_dir)["status"]
        if step != current:
            object_records.update_collection_record(
                "shipments", shipment_id, {"status": step},
                base_dir=data_dir, actor="test")
        if step == status:
            break
    return object_records.get_collection_record("shipments", shipment_id,
                                                base_dir=data_dir)


def receive(data_dir, shipment_id):
    """The parcel lands on our dock: authorized -> received, the walk-in
    shortcut the inbound ladder allows."""
    object_records.update_collection_record(
        "shipments", shipment_id, {"status": "received"},
        base_dir=data_dir, actor="test")


def shop_with_a_return(tmp_path, monkeypatch, *, quantity="3",
                       order_status="delivered", settings=None, tax_bps=0,
                       cents=1200):
    """A shop that sold three mugs, sent them, and got paid."""
    if settings is None:
        settings = [("shop.stock_location", "loc-shelf"),
                    ("shop.customer_location", "loc-customer")]
    data_dir, objects_root = setup_env(tmp_path, monkeypatch, settings=settings)
    location(data_dir, "loc-shelf", "Shelf")
    location(data_dir, "loc-customer", "Customers", kind="customer")
    product(data_dir, "p1", "Enamel Mug")
    stock_in(data_dir, "p1", 20)
    order(data_dir, status="shipped")
    order_line(data_dir, "line-1", quantity=quantity, cents=cents,
               tax_bps=tax_bps)
    outbound_shipment(data_dir, lines=(("line-1", quantity),),
                      status=order_status)
    # The sale move the outbound shipment composed: the goods are with the
    # customer, which is where a return has to come back FROM.
    object_records.create_collection_record(
        "stock_moves",
        {"id": object_ids.new_uuid4(), "product_id": "p1",
         "from_location_id": "loc-shelf", "to_location_id": "loc-customer",
         "quantity": str(quantity), "reason": "sale",
         "reference": "shipments/ship-out:line/ship-out-line-0",
         "occurred_at": "2026-07-03", "owner_id": "shop"},
        base_dir=data_dir)
    paid(data_dir, cents=int(float(quantity) * cents))
    return data_dir, objects_root


def authorized_return(objects_root, **payload):
    request = {"order_id": "ord-1", "reason": "no_longer_wanted",
               "lines": [{"order_line_id": "line-1", "quantity": "1"}]}
    request.update(payload)
    return authorize(objects_root, **request)


# --- the authorization gate -----------------------------------------------------

def test_returning_more_than_was_shipped_is_refused_with_the_numbers(
        tmp_path, monkeypatch):
    """A second RMA for the same mug is how two refunds get paid for one
    sale. "No" is not an answer anybody can act on -- which of the two
    returns is the wrong one is only knowable from the numbers."""
    data_dir, objects_root = shop_with_a_return(tmp_path, monkeypatch)
    first = authorized_return(objects_root)
    assert first["ok"] and first["lines"] == 1

    refused = authorize(objects_root, order_id="ord-1", reason="damaged",
                        lines=[{"order_line_id": "line-1", "quantity": "3"}])
    assert refused["status"] == 409
    assert refused["over_return"][0] == {
        "order_line_id": "line-1", "description": "Enamel Mug",
        "shipped": "3", "already_authorized": "1", "asked_for": "3",
        "would_make": "4"}
    # Nothing half-written: one RMA and one inbound parcel, from the first
    # call only.
    assert len(rmas(data_dir)) == 1
    assert len([row for row in shipments(data_dir)
                if row["direction"] == "inbound"]) == 1


def test_returning_against_an_order_that_never_shipped_is_refused(
        tmp_path, monkeypatch):
    """Goods still on our own shelf come off an order by cancelling or
    amending it -- a different document with different money attached."""
    data_dir, objects_root = setup_env(
        tmp_path, monkeypatch,
        settings=[("shop.stock_location", "loc-shelf"),
                  ("shop.customer_location", "loc-customer")])
    product(data_dir, "p1", "Enamel Mug")
    order(data_dir, status="confirmed")
    order_line(data_dir, "line-1")

    refused = authorized_return(objects_root)
    assert refused["status"] == 409
    assert "nothing has left the building" in refused["error"]
    assert "cannot return what was never sent" in refused["error"]
    assert rmas(data_dir) == []
    assert shipments(data_dir) == []


def test_a_return_has_to_say_why(tmp_path, monkeypatch):
    data_dir, objects_root = shop_with_a_return(tmp_path, monkeypatch)
    refused = authorize(objects_root, order_id="ord-1",
                        lines=[{"order_line_id": "line-1", "quantity": "1"}])
    assert refused["status"] == 409
    assert "has to say why" in refused["missing_reason"]

    unknown = authorize(objects_root, order_id="ord-1", reason="because",
                        lines=[{"order_line_id": "line-1", "quantity": "1"}])
    assert "Unknown reason" in unknown["missing_reason"]
    assert rmas(data_dir) == []


def test_every_blocker_is_reported_at_once(tmp_path, monkeypatch):
    """Whoever is doing this is usually on the phone to the customer."""
    data_dir, objects_root = shop_with_a_return(tmp_path, monkeypatch)
    refused = authorize(
        objects_root, order_id="ord-1",
        lines=[{"order_line_id": "line-1", "quantity": "9"},
               {"order_line_id": "no-such-line", "quantity": "1"},
               {"order_line_id": "line-1", "quantity": "0"}])
    assert refused["status"] == 409
    assert refused["over_return"] and refused["unknown_lines"] == ["no-such-line"]
    assert refused["bad_quantities"][0]["order_line_id"] == "line-1"
    assert refused["missing_reason"]
    assert rmas(data_dir) == []


# --- the RMA is an inbound shipment ---------------------------------------------

def test_an_rma_creates_an_inbound_shipment_carrying_the_lines(
        tmp_path, monkeypatch):
    """A return is not a new noun: it is the shipment document with the
    sign reversed, and the lines live where lines have always lived."""
    data_dir, objects_root = shop_with_a_return(tmp_path, monkeypatch)
    created = authorized_return(objects_root, reason="damaged",
                                reason_note="handle snapped off",
                                lines=[{"order_line_id": "line-1",
                                        "quantity": "2"}],
                                today="2026-07-10", expires_days=14)
    assert created["ok"]

    parcel = object_records.get_collection_record(
        "shipments", created["shipment_id"], base_dir=data_dir)
    assert parcel["direction"] == "inbound"
    assert parcel["status"] == "authorized"
    assert parcel["order_id"] == "ord-1"
    assert parcel["ship_to_name"] == "Ada Lovelace"
    assert "orders/ord-1" in parcel["notes"]

    lines = shipment_lines(data_dir, created["shipment_id"])
    assert len(lines) == 1
    assert lines[0]["order_line_id"] == "line-1"
    assert lines[0]["quantity"] == "2"
    assert lines[0]["description"] == "Enamel Mug"

    rma = object_records.get_collection_record(
        "return_authorizations", created["return_id"], base_dir=data_dir)
    assert rma["status"] == "authorized"
    assert rma["shipment_id"] == created["shipment_id"]
    assert rma["reason"] == "damaged"
    assert "handle snapped" in rma["reason_note"]
    assert rma["customer_email"] == "ada@example.test"
    # An RMA is an offer with a deadline: goods trickling back six months
    # later against a closed order is how a returns process stops being
    # auditable. DATA in this slice -- stamped, never enforced.
    assert rma["expires_on"] == "2026-07-24"
    assert created["expires_on"] == "2026-07-24"

    # Nothing has moved and nothing has been paid back.
    assert moves(data_dir, "return") == []
    assert refunds(data_dir) == []


def test_the_original_shipment_is_never_touched(tmp_path, monkeypatch):
    """A return does not un-ship anything: what left the building on the
    3rd left the building on the 3rd (docs/logic-decisions.md #3)."""
    data_dir, objects_root = shop_with_a_return(tmp_path, monkeypatch)
    before = object_records.get_collection_record("shipments", "ship-out",
                                                  base_dir=data_dir)
    authorized_return(objects_root, lines=[{"order_line_id": "line-1",
                                            "quantity": "3"}])
    after = object_records.get_collection_record("shipments", "ship-out",
                                                 base_dir=data_dir)
    assert after["status"] == before["status"] == "delivered"
    assert len(shipment_lines(data_dir, "ship-out")) == 1
    assert shipment_lines(data_dir, "ship-out")[0]["quantity"] == "3"


# --- goods arriving is not a decision -------------------------------------------

def test_the_arrival_handler_stamps_the_rma_and_touches_no_money(
        tmp_path, monkeypatch):
    """A box on the dock could hold a mug in its wrapping or the same mug
    in three pieces. The handler keeps the paperwork in step and leaves the
    judgement to a human."""
    data_dir, objects_root = shop_with_a_return(tmp_path, monkeypatch)
    created = authorized_return(objects_root)

    early = post_return(objects_root, created["shipment_id"])
    assert early["skipped"] == "shipment is authorized, not received"

    receive(data_dir, created["shipment_id"])
    result = post_return(objects_root, created["shipment_id"])
    assert result["ok"] and result["return_id"] == created["return_id"]
    assert result["return_status"] == "authorized"
    assert "nothing financial has happened" in result["note"]
    assert moves(data_dir, "return") == [] and refunds(data_dir) == []


def test_the_arrival_handler_ignores_outbound_parcels(tmp_path, monkeypatch):
    """Two handlers reacting to the same row is how one undoes the other's
    work."""
    data_dir, objects_root = shop_with_a_return(tmp_path, monkeypatch)
    result = post_return(objects_root, "ship-out")
    assert result["skipped"] == "outbound shipments are not returns"


def test_a_parcel_arriving_after_the_deadline_says_so_and_refuses_nothing(
        tmp_path, monkeypatch):
    """Expiry is DATA in this slice. A box on the dock is a box on the
    dock; what the date buys is that whoever chooses restock-or-refund
    learns they are outside the promise before they choose."""
    data_dir, objects_root = shop_with_a_return(tmp_path, monkeypatch)
    created = authorized_return(objects_root, today="2020-01-01",
                                expires_days=30)
    receive(data_dir, created["shipment_id"])

    result = post_return(objects_root, created["shipment_id"])
    assert result["ok"] is True
    assert result["past_expiry"] is True
    assert result["expires_on"] == "2020-01-31"
    # And the return can still be dispositioned: nothing anywhere refuses a
    # late parcel, and no pass sweeps stale RMAs to `expired` -- that is
    # the tracking slice's daemon work, deliberately absent here.
    done = disposition(objects_root, shipment_id=created["shipment_id"],
                       lines=[{"shipment_line_id": shipment_lines(
                           data_dir, created["shipment_id"])[0]["id"],
                           "disposition": "restock", "quantity": "1"}])
    assert done["ok"] is True


# --- the disposition ------------------------------------------------------------

def dispositioned(objects_root, data_dir, created, *, disposition_kind="restock",
                  quantity="1", **payload):
    line_id = shipment_lines(data_dir, created["shipment_id"])[0]["id"]
    request = {"shipment_id": created["shipment_id"],
               "lines": [{"shipment_line_id": line_id,
                          "disposition": disposition_kind,
                          "quantity": quantity}]}
    request.update(payload)
    return disposition(objects_root, **request)


def test_dispositioning_a_parcel_that_has_not_arrived_is_refused(
        tmp_path, monkeypatch):
    """Deciding what is in a box nobody has opened is a guess."""
    data_dir, objects_root = shop_with_a_return(tmp_path, monkeypatch)
    created = authorized_return(objects_root)

    refused = dispositioned(objects_root, data_dir, created)
    assert refused["status"] == 409
    assert "is authorized, not received" in refused["error"]
    assert "in transit is a guess" in refused["error"]
    assert moves(data_dir, "return") == []
    parcel = object_records.get_collection_record(
        "shipments", created["shipment_id"], base_dir=data_dir)
    assert parcel["status"] == "authorized"


def test_restock_composes_a_return_move_and_the_shelf_goes_back_up(
        tmp_path, monkeypatch):
    data_dir, objects_root = shop_with_a_return(tmp_path, monkeypatch)
    created = authorized_return(objects_root)
    receive(data_dir, created["shipment_id"])
    before = on_shelf(data_dir)

    result = dispositioned(objects_root, data_dir, created)
    assert result["ok"] and result["moved"] == 1
    assert on_shelf(data_dir) == before + 1

    move = moves(data_dir, "return")[0]
    assert move["from_location_id"] == "loc-customer"
    assert move["to_location_id"] == "loc-shelf"
    assert move["quantity"] == "1"
    assert f"returns/{created['shipment_id']}:line/" in move["reference"]

    parcel = object_records.get_collection_record(
        "shipments", created["shipment_id"], base_dir=data_dir)
    assert parcel["status"] == "dispositioned"
    rma = object_records.get_collection_record(
        "return_authorizations", created["return_id"], base_dir=data_dir)
    assert rma["status"] == "closed"


def test_dispose_composes_a_loss_move_and_the_shelf_does_not_go_up(
        tmp_path, monkeypatch):
    """Disposal is still a MOVE. Goods that left inventory with no trace is
    exactly how shrinkage hides, and a loss report that only counts what
    somebody remembered to write down is guaranteed to be optimistic."""
    data_dir, objects_root = shop_with_a_return(tmp_path, monkeypatch)
    created = authorized_return(objects_root)
    receive(data_dir, created["shipment_id"])
    before = on_shelf(data_dir)

    result = dispositioned(objects_root, data_dir, created,
                           disposition_kind="dispose")
    assert result["ok"] and result["moved"] == 1
    assert on_shelf(data_dir) == before          # nothing came back to sell
    assert moves(data_dir, "return") == []

    move = moves(data_dir, "waste")[0]
    assert move["from_location_id"] == "loc-customer"
    # The loss shape hook_stock_moves enforces: out of a real location, to
    # nowhere. Goods leave the system; they are not wasted INTO a shelf.
    assert move["to_location_id"] == ""
    assert move["quantity"] == "1"


def test_a_damaged_return_is_disposed_of_as_damage_not_waste(
        tmp_path, monkeypatch):
    """'This arrived broken' and 'we cannot resell this' are different
    arguments, and the loss taxonomy exists so the difference survives."""
    data_dir, objects_root = shop_with_a_return(tmp_path, monkeypatch)
    created = authorized_return(objects_root, reason="damaged")
    receive(data_dir, created["shipment_id"])

    dispositioned(objects_root, data_dir, created, disposition_kind="dispose")
    assert moves(data_dir, "waste") == []
    assert len(moves(data_dir, "damage")) == 1


def test_a_replayed_disposition_moves_nothing_twice_and_refunds_nothing_twice(
        tmp_path, monkeypatch):
    """The property this whole slice is judged on. Retries, double clicks
    and replayed queue entries are ordinary; a customer's money going back
    twice is not."""
    data_dir, objects_root = shop_with_a_return(tmp_path, monkeypatch)
    created = authorized_return(objects_root)
    receive(data_dir, created["shipment_id"])

    first = dispositioned(objects_root, data_dir, created, refund="full")
    assert first["moved"] == 1 and first["refund_id"]
    assert len(refunds(data_dir)) == 1

    again = dispositioned(objects_root, data_dir, created, refund="full")
    assert again["ok"] is True
    assert "already dispositioned" in again["note"]
    assert again["moved"] == 0 and again["refund_id"] == ""
    assert again["return_id"] == created["return_id"]
    assert again["refund_ref"] == first["refund_ref"]

    assert len(moves(data_dir, "return")) == 1
    assert len(refunds(data_dir)) == 1


def test_dispositioning_more_than_came_back_is_refused(tmp_path, monkeypatch):
    data_dir, objects_root = shop_with_a_return(tmp_path, monkeypatch)
    created = authorized_return(objects_root)
    receive(data_dir, created["shipment_id"])

    refused = dispositioned(objects_root, data_dir, created, quantity="5")
    assert refused["status"] == 409
    assert refused["over_disposition"][0]["came_back"] == "1"
    assert refused["over_disposition"][0]["asked_for"] == "5"
    assert moves(data_dir, "return") == []


def test_a_disposition_has_to_name_what_each_line_is(tmp_path, monkeypatch):
    """No default: guessing restock puts damaged goods on the shelf and
    guessing dispose bins sellable stock."""
    data_dir, objects_root = shop_with_a_return(tmp_path, monkeypatch)
    created = authorized_return(objects_root)
    receive(data_dir, created["shipment_id"])

    refused = disposition(objects_root, shipment_id=created["shipment_id"])
    assert refused["status"] == 409
    assert "no default" in refused["bad_quantities"][0]["reason"]


# --- the money ------------------------------------------------------------------

def test_a_full_refund_pays_back_what_was_paid_for_those_lines(
        tmp_path, monkeypatch):
    """The line money plus the tax charged ON that line, computed the way
    order_totals computed it -- not a number that looks close."""
    data_dir, objects_root = shop_with_a_return(tmp_path, monkeypatch,
                                                tax_bps=825)
    created = authorized_return(objects_root)
    receive(data_dir, created["shipment_id"])

    result = dispositioned(objects_root, data_dir, created, refund="full")
    assert result["ok"]
    # One mug at 1200, tax 8.25% floored: 1200 + 99.
    assert result["refund_cents"] == "1299"

    row = refunds(data_dir)[0]
    assert row["amount_cents"] == "1299"
    assert row["payment_id"] == "pay-1"
    # Stamped by hook_refunds from the payment, never trusted from us.
    assert row["invoice_id"] == "inv-1"
    assert row["refunded_on"]

    rma = object_records.get_collection_record(
        "return_authorizations", created["return_id"], base_dir=data_dir)
    assert rma["refund_ref"] == f"refunds/{row['id']}"
    assert result["refund_ref"] == rma["refund_ref"]


def test_an_over_large_partial_refund_is_refused_by_the_existing_gate(
        tmp_path, monkeypatch):
    """The ceiling is not reimplemented here: app-payments' hook already
    knows what a payment can give back, and its words are what surfaces --
    so a customer is quoted the same number whichever door they came
    through."""
    data_dir, objects_root = shop_with_a_return(tmp_path, monkeypatch)
    created = authorized_return(objects_root)
    receive(data_dir, created["shipment_id"])

    refused = dispositioned(objects_root, data_dir, created, refund="partial",
                            refund_cents="99999")
    assert refused["status"] == 409
    assert "exceeds the refundable" in refused["refund"]
    assert "3600" in refused["refund"]           # what the payment can give
    assert "pay-1" in refused["refund"]

    # And nothing happened: no refund, no moves, and the return is still
    # open for somebody to get right.
    assert refunds(data_dir) == []
    assert moves(data_dir, "return") == []
    parcel = object_records.get_collection_record(
        "shipments", created["shipment_id"], base_dir=data_dir)
    assert parcel["status"] == "received"


def test_a_partial_refund_within_the_ceiling_is_written(tmp_path, monkeypatch):
    data_dir, objects_root = shop_with_a_return(tmp_path, monkeypatch)
    created = authorized_return(objects_root)
    receive(data_dir, created["shipment_id"])

    result = dispositioned(objects_root, data_dir, created, refund="partial",
                           refund_cents="900")
    assert result["ok"] and result["refund_cents"] == "900"
    assert refunds(data_dir)[0]["amount_cents"] == "900"


def test_refund_none_closes_the_return_silently(tmp_path, monkeypatch):
    """A restocking-fee-only return, a replacement instead of a refund, a
    goodwill restock: all ordinary. Warning about them would teach
    operators to click past warnings."""
    data_dir, objects_root = shop_with_a_return(tmp_path, monkeypatch)
    created = authorized_return(objects_root)
    receive(data_dir, created["shipment_id"])

    result = dispositioned(objects_root, data_dir, created)
    assert result["ok"] and result["moved"] == 1
    assert result["refund"] == "none"
    assert result["refund_id"] == "" and result["refund_ref"] == ""
    assert "warning" not in result and "note" not in result
    assert refunds(data_dir) == []

    rma = object_records.get_collection_record(
        "return_authorizations", created["return_id"], base_dir=data_dir)
    assert rma["status"] == "closed"
    assert rma["refund_ref"] == ""


# --- the page -------------------------------------------------------------------

def form(objects_root, order_id="ord-1", user_id="shop", method="GET", **fields):
    payload = {"order_id": order_id}
    if user_id:
        payload["_identity"] = {"user_id": user_id}
    if fields:
        payload["_form"] = fields
    return run(objects_root, "site_return_form", method, payload)


def test_the_return_form_shows_the_shipped_lines(tmp_path, monkeypatch):
    data_dir, objects_root = shop_with_a_return(tmp_path, monkeypatch)
    body = form(objects_root)["body"]
    assert "Enamel Mug" in body
    assert "SO-0001" in body
    assert ">3<" in body                          # three of them can come back
    assert 'name="qty_line-1"' in body
    assert "No longer wanted" in body
    # Nothing has been returned yet, so there is no RMA block and no label
    # placeholder to show.
    assert "Returns on this order" not in body


def test_the_return_form_raises_the_rma_and_shows_it_back(tmp_path, monkeypatch):
    data_dir, objects_root = shop_with_a_return(tmp_path, monkeypatch)
    result = form(objects_root, method="POST",
                  **{"do": "request", "reason": "damaged",
                     "reason_note": "handle snapped", "qty_line-1": "1"})
    body = result["body"]
    assert "authorized" in body
    assert len(rmas(data_dir)) == 1
    rma = rmas(data_dir)[0]
    assert rma["reason"] == "damaged"
    assert rma["status"] == "authorized"
    assert "Returns on this order" in body
    assert "return label will appear here" in body
    assert "carrier connector" in body


def test_the_return_form_surfaces_the_actions_refusal_with_its_numbers(
        tmp_path, monkeypatch):
    """Rewording the gate here would guarantee two vocabularies for one
    rule."""
    data_dir, objects_root = shop_with_a_return(tmp_path, monkeypatch)
    result = form(objects_root, method="POST",
                  **{"reason": "damaged", "qty_line-1": "9"})
    body = result["body"]
    assert "shipped 3" in body
    assert "would make 9" in body
    assert rmas(data_dir) == []


def test_the_return_form_asks_a_stranger_to_sign_in(tmp_path, monkeypatch):
    _, objects_root = shop_with_a_return(tmp_path, monkeypatch)
    body = form(objects_root, user_id="")["body"]
    assert "Sign in" in body
    assert "Enamel Mug" not in body


def test_the_return_form_is_a_friendly_404_for_an_unknown_order(
        tmp_path, monkeypatch):
    _, objects_root = shop_with_a_return(tmp_path, monkeypatch)
    result = form(objects_root, order_id="no-such-order")
    assert result["status"] == 404
    assert "Traceback" not in result["body"]
