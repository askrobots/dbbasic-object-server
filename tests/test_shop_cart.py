"""The shop: basket, checkout, and a sale that posts itself.

The cart is the easy part and everybody has one. What is worth testing is
the seam: that a basket is not an order, that a price which moved is
surfaced rather than silently resolved either way, and that stock leaves
the shelf when money arrives and not one moment sooner.
"""

import pathlib

from conftest import stage_collection

import object_cart
import object_execution
import object_ids
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
SHOP_OBJECTS = PACKAGES / "app-shop" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

TOKEN = "sess-abc123"


def setup_env(tmp_path, monkeypatch, *, settings=()):
    data_dir = tmp_path / "data"
    for pkg, name in (("app-shop", "carts"), ("app-shop", "cart_items"),
                      ("app-catalog", "products"), ("app-catalog", "locations"),
                      ("app-catalog", "stock_moves"),
                      ("app-orders", "orders"), ("app-orders", "order_lines"),
                      ("app-invoices", "invoices"), ("app-invoices", "invoice_lines"),
                      ("app-payments", "payments")):
        stage_collection(data_dir, pkg, name)
    rows = "".join(f"s{i}\t{k}\t{v}\t\n" for i, (k, v) in enumerate(settings))
    stage_collection(data_dir, "app-settings", "app_settings", rows=rows)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    return data_dir


def product(data_dir, product_id, name, cents, **fields):
    record = {"id": product_id, "name": name, "sku": product_id.upper(),
              "product_type": "physical", "price_cents": str(cents),
              "currency": "USD", "is_active": "true", "owner_id": "shop"}
    record.update({k: str(v) for k, v in fields.items()})
    return object_records.create_collection_record("products", record,
                                                   base_dir=data_dir)


def location(data_dir, location_id, name, kind="warehouse"):
    return object_records.create_collection_record(
        "locations",
        {"id": location_id, "name": name, "location_type": kind, "owner_id": "shop"},
        base_dir=data_dir)


def stock_in(data_dir, product_id, quantity, *, to="loc-shelf"):
    return object_records.create_collection_record(
        "stock_moves",
        {"id": object_ids.new_uuid4(), "product_id": product_id,
         "to_location_id": to, "quantity": str(quantity), "reason": "purchase",
         "occurred_at": "2026-06-01", "owner_id": "shop"},
        base_dir=data_dir)


def cart(action="get", **payload):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "action_cart", method="POST",
            payload={"session_token": TOKEN, "action": action, **payload}),
        roots=[SHOP_OBJECTS]).result


def checkout(**payload):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "action_checkout", method="POST",
            payload={"session_token": TOKEN, **payload}),
        roots=[SHOP_OBJECTS]).result


def fulfil(payment):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "system_shop_fulfillment", method="POST",
            payload={"collection": "payments", "record": payment}),
        roots=[SHOP_OBJECTS]).result


def orders(data_dir):
    return object_records.read_collection_records("orders", base_dir=data_dir)


def moves(data_dir):
    return object_records.read_collection_records("stock_moves", base_dir=data_dir)


def invoices(data_dir):
    return object_records.read_collection_records("invoices", base_dir=data_dir)


def invoice_lines(data_dir):
    return object_records.read_collection_records("invoice_lines", base_dir=data_dir)


# --- the arithmetic ----------------------------------------------------------

def test_a_line_rounds_once_and_the_total_is_the_sum_of_the_lines():
    """Fractional quantities are real -- 1.5 kg, 2.5 hours of setup."""
    assert object_cart.line_total_cents("1.5", 333) == 500      # 499.5 -> 500
    summary = object_cart.totals([
        {"id": "a", "quantity": "1.5", "unit_price_cents": "333"},
        {"id": "b", "quantity": "2", "unit_price_cents": "1000"},
    ])
    assert summary["subtotal_cents"] == 2500
    assert sum(line["line_total_cents"] for line in summary["lines"]) == 2500


def test_a_price_that_moved_is_reported_not_applied():
    changes = object_cart.price_changes(
        [{"id": "a", "product_id": "p1", "unit_price_cents": "1000"}],
        {"p1": {"price_cents": "1200", "name": "Mug"}})
    assert changes[0]["was_cents"] == 1000 and changes[0]["now_cents"] == 1200
    assert changes[0]["direction"] == "up"


def test_an_untracked_product_is_always_available():
    """A service or a download has no stock level, and 'no level' must not
    read as 'none left' -- that would refuse to sell what never runs out."""
    items = [{"id": "a", "product_id": "svc", "quantity": "3"}]
    assert object_cart.availability(items, {}, tracked=set()) == []


def test_every_blocker_is_reported_at_once():
    """Revealing problems one at a time is how a checkout gets abandoned."""
    items = [{"id": "a", "product_id": "p1", "quantity": "5",
              "unit_price_cents": "1000", "description": "Mug"},
             {"id": "b", "product_id": "gone", "quantity": "1"}]
    blockers = object_cart.checkout_blockers(
        items, {"p1": {"price_cents": "1200", "is_active": "true"}},
        {"p1": 2}, tracked={"p1"})
    assert blockers["price_changes"] and blockers["unavailable"] and blockers["inactive"]
    assert blockers["can_checkout"] is False


# --- the basket ---------------------------------------------------------------

def test_adding_stamps_the_price_it_had_at_the_time(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Enamel Mug", 1200)
    result = cart("add", product_id="p1", quantity="2")
    assert result["subtotal_cents"] == 2400

    item = object_records.read_collection_records("cart_items", base_dir=data_dir)[0]
    assert item["unit_price_cents"] == "1200"
    assert item["description"] == "Enamel Mug"


def test_adding_the_same_thing_twice_increases_the_quantity(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Mug", 1000)
    cart("add", product_id="p1", quantity="1")
    result = cart("add", product_id="p1", quantity="2")
    assert result["count"] == "3"
    assert len(object_records.read_collection_records("cart_items", base_dir=data_dir)) == 1


def test_setting_a_quantity_to_zero_removes_the_line(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Mug", 1000)
    cart("add", product_id="p1")
    result = cart("set", product_id="p1", quantity="0")
    assert result["lines"] == []


def test_an_empty_basket_is_an_answer_not_a_404(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    result = cart("get")
    assert result["ok"] and result["lines"] == [] and result["subtotal_cents"] == 0


def test_something_not_for_sale_cannot_go_in_the_basket(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Retired Mug", 1000, is_active="false")
    assert cart("add", product_id="p1")["status"] == 409
    assert cart("add", product_id="nope")["status"] == 404


def test_no_stock_is_touched_by_putting_something_in_a_basket(
        tmp_path, monkeypatch):
    """Reserving on add is how a shop shows 'sold out' for goods nobody
    bought."""
    data_dir = setup_env(tmp_path, monkeypatch)
    location(data_dir, "loc-shelf", "Shelf")
    product(data_dir, "p1", "Mug", 1000)
    stock_in(data_dir, "p1", 5)
    cart("add", product_id="p1", quantity="3")
    assert len(moves(data_dir)) == 1                # only the purchase


# --- checkout -------------------------------------------------------------------

def test_checkout_raises_a_draft_order_and_closes_the_basket(
        tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    location(data_dir, "loc-shelf", "Shelf")
    product(data_dir, "p1", "Mug", 1200)
    stock_in(data_dir, "p1", 10)
    cart("add", product_id="p1", quantity="2")

    result = checkout(customer_email="buyer@example.test", customer_name="Ada")
    assert result["ok"] and result["status_of_order"] == "draft"

    order = orders(data_dir)[0]
    assert order["total_cents"] == "2400"
    assert order["customer_email"] == "buyer@example.test"
    assert order["doc_type"] == "sale"

    lines = object_records.read_collection_records("order_lines", base_dir=data_dir)
    assert len(lines) == 1 and lines[0]["line_total_cents"] == "2400"
    assert len(moves(data_dir)) == 1                # still nothing moved


def test_checkout_raises_the_invoice_that_the_buyer_pays(tmp_path, monkeypatch):
    """An order with no invoice gives the buyer nothing to pay: the money is
    owed and there is no document saying so and no door to walk through.

    It is issued ("sent"), not a draft -- a draft would mean somebody in the
    back office has to press a button before the shopper standing at the
    counter can hand over money.
    """
    data_dir = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Mug", 1200, product_type="service")
    cart("add", product_id="p1", quantity="2")

    result = checkout(customer_email="buyer@example.test", customer_name="Ada")
    invoice = invoices(data_dir)[0]
    assert invoice["status"] == "sent"
    assert invoice["total_cents"] == "2400" == orders(data_dir)[0]["total_cents"]
    assert invoice["customer_email"] == "buyer@example.test"
    assert invoice["number"] == orders(data_dir)[0]["number"]

    # The join system_shop_fulfillment walks backwards from a payment.
    assert orders(data_dir)[0]["invoice_id"] == invoice["id"]
    assert result["invoice_id"] == invoice["id"]

    # The response has to carry the door: the shopper is here NOW, and the
    # portal-link handler mints post-commit and best-effort.
    assert invoice["portal_token"]
    assert result["pay_path"] == f"/pay/{invoice['portal_token']}"


def test_the_invoice_lines_say_what_was_bought(tmp_path, monkeypatch):
    """A single opaque total is a bill a customer can only dispute, not
    check."""
    data_dir = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Enamel Mug", 1200, product_type="service")
    product(data_dir, "p2", "Setup", 2500, product_type="service")
    cart("add", product_id="p1", quantity="2")
    cart("add", product_id="p2")
    checkout(customer_email="buyer@example.test")

    lines = sorted(invoice_lines(data_dir), key=lambda row: row["description"])
    assert [line["description"] for line in lines] == ["Enamel Mug", "Setup"]
    assert [line["line_total_cents"] for line in lines] == ["2400", "2500"]
    assert lines[0]["quantity"] == "2"
    assert lines[0]["unit_price_cents"] == "1200"
    assert {line["invoice_id"] for line in lines} == {invoices(data_dir)[0]["id"]}


def test_the_invoice_records_the_order_it_came_from(tmp_path, monkeypatch):
    """Provenance in notes, the house pattern: invoices carry no
    generated_from column, so the marker goes where anyone asking where this
    bill came from can find it."""
    data_dir = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Mug", 1000, product_type="service")
    cart("add", product_id="p1")
    order_id = checkout(customer_email="buyer@example.test")["order_id"]
    assert f"orders/{order_id}" in invoices(data_dir)[0]["notes"]


def test_the_due_date_follows_the_billing_setting(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("billing.invoice_due_days", "30"),))
    product(data_dir, "p1", "Mug", 1000, product_type="service")
    cart("add", product_id="p1")
    checkout(customer_email="buyer@example.test", today="2026-06-15")
    invoice = invoices(data_dir)[0]
    assert invoice["issue_date"] == "2026-06-15"
    assert invoice["due_date"] == "2026-07-15"


def test_checking_out_twice_makes_one_order(tmp_path, monkeypatch):
    """And one invoice, with the SAME door handed back -- a shopper who
    double-clicked needs the link again, not a second bill."""
    data_dir = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Download", 500, product_type="service")
    cart("add", product_id="p1")
    first = checkout(customer_email="buyer@example.test")
    again = checkout(customer_email="buyer@example.test")
    assert again["duplicate"] is True
    assert again["order_id"] == first["order_id"]
    assert again["pay_path"] == first["pay_path"]
    assert len(orders(data_dir)) == 1
    assert len(invoices(data_dir)) == 1


def test_a_price_change_stops_checkout_and_shows_both_numbers(
        tmp_path, monkeypatch):
    """Charging the new price silently is a bait-and-switch; honouring a
    three-week-old basket forever is an open-ended liability."""
    data_dir = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Mug", 1000, product_type="service")
    cart("add", product_id="p1")
    object_records.update_collection_record(
        "products", "p1", {"price_cents": "1400"}, base_dir=data_dir, actor="shop")

    blocked = checkout(customer_email="buyer@example.test")
    assert blocked["status"] == 409
    assert blocked["price_changes"][0]["was_cents"] == 1000
    assert blocked["price_changes"][0]["now_cents"] == 1400
    assert orders(data_dir) == []


def test_confirming_the_new_price_orders_at_the_new_price(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Mug", 1000, product_type="service")
    cart("add", product_id="p1")
    object_records.update_collection_record(
        "products", "p1", {"price_cents": "1400"}, base_dir=data_dir, actor="shop")

    result = checkout(customer_email="buyer@example.test", confirm_prices="true")
    assert result["ok"] and result["total_cents"] == 1400
    assert orders(data_dir)[0]["total_cents"] == "1400"


def test_not_enough_stock_stops_checkout_with_the_numbers(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    location(data_dir, "loc-shelf", "Shelf")
    product(data_dir, "p1", "Mug", 1000)
    stock_in(data_dir, "p1", 2)
    cart("add", product_id="p1", quantity="5")

    blocked = checkout(customer_email="buyer@example.test")
    assert blocked["status"] == 409
    assert blocked["unavailable"][0]["available"] == "2"
    assert blocked["unavailable"][0]["short_by"] == "3"


def test_an_emptied_basket_cannot_be_checked_out(tmp_path, monkeypatch):
    """A basket somebody filled and then emptied still exists; there is
    just nothing in it to sell."""
    data_dir = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Mug", 1000, product_type="service")
    cart("add", product_id="p1")
    cart("clear")
    assert checkout(customer_email="buyer@example.test")["status"] == 400


def test_checking_out_a_session_with_no_basket_says_so(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    assert checkout(customer_email="buyer@example.test")["status"] == 404


def test_checkout_needs_somewhere_to_send_the_receipt(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Mug", 1000, product_type="service")
    cart("add", product_id="p1")
    assert checkout()["status"] == 400


def test_preview_prices_the_basket_without_ordering(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Mug", 1000, product_type="service")
    cart("add", product_id="p1", quantity="2")
    result = checkout(preview="true")
    assert result["subtotal_cents"] == 2000 and orders(data_dir) == []


# --- money arrives, the sale becomes real -----------------------------------------

def paid_order(data_dir, *, quantity="2"):
    """Take an order all the way to a received payment.

    The invoice is the one checkout raised, not a hand-made stand-in: paying
    against a fake invoice would let the order/invoice join rot without a
    single test noticing.
    """
    location(data_dir, "loc-shelf", "Shelf")
    location(data_dir, "loc-customer", "Customers", kind="customer")
    product(data_dir, "p1", "Mug", 1200)
    stock_in(data_dir, "p1", 10)
    cart("add", product_id="p1", quantity=quantity)
    placed = checkout(customer_email="buyer@example.test", customer_name="Ada")
    order_id, invoice_id = placed["order_id"], placed["invoice_id"]

    payment = object_records.create_collection_record(
        "payments",
        {"id": object_ids.new_uuid4(), "invoice_id": invoice_id,
         "amount_cents": "2400", "method": "card", "received_on": "2026-06-15",
         "status": "received", "owner_id": "shop"},
        base_dir=data_dir)
    return order_id, payment


def test_stock_leaves_the_shelf_when_the_money_arrives(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("shop.stock_location", "loc-shelf"),
                                   ("shop.customer_location", "loc-customer")))
    order_id, payment = paid_order(data_dir)
    result = fulfil(payment)
    assert result["confirmed"] and result["moved"] == 1

    sale = [m for m in moves(data_dir) if m["reason"] == "sale"][0]
    assert sale["quantity"] == "2"
    assert sale["from_location_id"] == "loc-shelf"
    assert f"orders/{order_id}:fulfil" in sale["reference"]
    assert object_records.get_collection_record(
        "orders", order_id, base_dir=data_dir)["status"] == "confirmed"


def test_a_replayed_payment_moves_nothing_twice(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("shop.stock_location", "loc-shelf"),
                                   ("shop.customer_location", "loc-customer")))
    _, payment = paid_order(data_dir)
    fulfil(payment)
    again = fulfil(payment)
    assert "already fulfilled" in again["skipped"]
    assert len([m for m in moves(data_dir) if m["reason"] == "sale"]) == 1


def test_a_payment_for_something_else_is_left_alone(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    object_records.create_collection_record(
        "invoices",
        {"id": "inv-unrelated", "number": "INV-9", "customer_name": "Someone",
         "status": "sent", "issue_date": "2026-06-15", "due_date": "2026-06-29",
         "subtotal_cents": "500", "total_cents": "500", "owner_id": "shop"},
        base_dir=data_dir)
    payment = object_records.create_collection_record(
        "payments",
        {"id": object_ids.new_uuid4(), "invoice_id": "inv-unrelated",
         "amount_cents": "500", "method": "card", "received_on": "2026-06-15",
         "status": "received", "owner_id": "shop"},
        base_dir=data_dir)
    assert fulfil(payment)["skipped"] == "no web order for this payment"


def test_a_bounced_payment_fulfils_nothing(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("shop.stock_location", "loc-shelf"),))
    order_id, payment = paid_order(data_dir)
    object_records.update_collection_record(
        "payments", payment["id"], {"status": "bounced"},
        base_dir=data_dir, actor="shop")
    bounced = object_records.get_collection_record("payments", payment["id"],
                                                   base_dir=data_dir)
    assert fulfil(bounced)["skipped"] == "payment not received"
    assert object_records.get_collection_record(
        "orders", order_id, base_dir=data_dir)["status"] == "draft"


def test_an_unconfigured_location_still_lets_the_sale_stand(tmp_path, monkeypatch):
    """A missing setting must not cost somebody a paid order."""
    data_dir = setup_env(tmp_path, monkeypatch)
    order_id, payment = paid_order(data_dir)
    result = fulfil(payment)
    assert result["confirmed"] and result["moved"] == 0
    assert "stock_location" in result["warning"]
    assert object_records.get_collection_record(
        "orders", order_id, base_dir=data_dir)["status"] == "confirmed"
