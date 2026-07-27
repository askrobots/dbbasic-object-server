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
COOKIE = "cart"


def setup_env(tmp_path, monkeypatch, *, settings=(), page=False):
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
    if page:
        # site_shop calls its siblings by object id with no roots of its
        # own, so the page tests need the shop's objects on the search path.
        monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(SHOP_OBJECTS))
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


def page(**payload):
    """GET the public shop page, the way the site router would call it."""
    payload.setdefault("_cookies", {COOKIE: TOKEN})
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "site_shop", method="GET", payload=payload),
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

SHIPPING_OBJECTS = PACKAGES / "app-shipping" / "objects"


def with_shipping(tmp_path, monkeypatch, data_dir):
    """Install app-shipping alongside the shop, the way a real box has it.

    Payment no longer writes stock moves itself: it raises a SHIPMENT and
    lets app-shipping's system_order_fulfillment move the goods (see
    objects/system/shop_fulfillment.py's docstring). Two consequences for a
    test: the two shipping collections have to exist, and both packages'
    objects have to live under ONE object root, because that is what an
    installed server looks like and the only way a sibling call by object id
    resolves the way it will in production.

    shutil is imported here rather than at the top of the file to keep this
    change contained to the fulfillment tests.
    """
    import shutil

    for name in ("shipments", "shipment_lines"):
        stage_collection(data_dir, "app-shipping", name)
    objects_root = tmp_path / "objects"
    for source in (SHOP_OBJECTS, SHIPPING_OBJECTS):
        shutil.copytree(source, objects_root, dirs_exist_ok=True)
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_root))
    return objects_root


def shipments(data_dir):
    return object_records.read_collection_records("shipments", base_dir=data_dir)


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
    with_shipping(tmp_path, monkeypatch, data_dir)
    order_id, payment = paid_order(data_dir)
    result = fulfil(payment)
    assert result["confirmed"] and result["moved"] == 1

    sale = [m for m in moves(data_dir) if m["reason"] == "sale"][0]
    assert sale["quantity"] == "2"
    assert sale["from_location_id"] == "loc-shelf"

    # The same chain, now travelling through the shipment noun: there is a
    # document saying what went out, and the move is stamped with the line
    # it came from rather than with the whole order.
    shipment = shipments(data_dir)[0]
    assert shipment["order_id"] == order_id and shipment["status"] == "shipped"
    assert f"shipments/{shipment['id']}:line/" in sale["reference"]
    assert object_records.get_collection_record(
        "orders", order_id, base_dir=data_dir)["status"] == "shipped"


def test_a_replayed_payment_moves_nothing_twice(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("shop.stock_location", "loc-shelf"),
                                   ("shop.customer_location", "loc-customer")))
    with_shipping(tmp_path, monkeypatch, data_dir)
    _, payment = paid_order(data_dir)
    fulfil(payment)
    again = fulfil(payment)
    assert again["skipped"] == "order already shipped"
    assert len([m for m in moves(data_dir) if m["reason"] == "sale"]) == 1
    assert len(shipments(data_dir)) == 1        # and no second parcel


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
    """A missing setting must not cost somebody a paid order -- and now it
    must not cost them the parcel either: the box still goes out, and the
    gap in the ledger is reported rather than swallowed."""
    data_dir = setup_env(tmp_path, monkeypatch)
    with_shipping(tmp_path, monkeypatch, data_dir)
    order_id, payment = paid_order(data_dir)
    result = fulfil(payment)
    assert result["confirmed"] and result["moved"] == 0
    assert "stock_location" in result["warning"]
    assert shipments(data_dir)[0]["status"] == "shipped"
    assert object_records.get_collection_record(
        "orders", order_id, base_dir=data_dir)["status"] == "shipped"


# --- the page a shopper actually looks at ------------------------------------

def test_a_product_card_links_to_the_product(tmp_path, monkeypatch):
    """The grid was a dead end: names were bold text, so a shopper could add
    a thing to a basket but never find out what it was."""
    data_dir = setup_env(tmp_path, monkeypatch, page=True)
    product(data_dir, "p1", "Enamel Mug", 1200)
    body = page()["body"]
    assert '<a href="/shop/p1">Enamel Mug</a>' in body


def test_the_product_page_says_what_it_is_and_offers_to_sell_it(
        tmp_path, monkeypatch):
    """Name, price and full description in one place, with an add form that
    posts to /shop -- the SAME route the cards post to, so there is one way
    into a basket rather than two that can drift apart."""
    data_dir = setup_env(tmp_path, monkeypatch, page=True)
    product(data_dir, "p1", "Enamel Mug", 1200,
            description="Twelve ounces. Chips beautifully.")
    body = page(product_id="p1")["body"]

    assert "Enamel Mug" in body
    assert "P1" in body                                 # the sku
    assert "12.00" in body
    assert "Twelve ounces. Chips beautifully." in body
    assert '<form method="post" action="/shop"' in body
    assert 'name="do" value="add"' in body
    assert f'name="session_token" value="{TOKEN}"' in body
    assert 'name="product_id" value="p1"' in body
    assert '<a href="/shop">Back to shop</a>' in body


def test_a_physical_product_says_whether_it_is_there(tmp_path, monkeypatch):
    """Low-key, but the question every shopper has. Derived from the stock
    ledger, never stored."""
    data_dir = setup_env(tmp_path, monkeypatch, page=True)
    location(data_dir, "loc-shelf", "Shelf")
    product(data_dir, "p1", "Mug", 1200)
    product(data_dir, "p2", "Bowl", 900)
    stock_in(data_dir, "p1", 4)
    assert "In stock" in page(product_id="p1")["body"]
    assert "Out of stock" in page(product_id="p2")["body"]


def test_something_that_never_runs_out_says_nothing_about_stock(
        tmp_path, monkeypatch):
    """An hour of work has no shelf. Saying "Out of stock" about it would
    refuse a sale that checkout would happily take."""
    data_dir = setup_env(tmp_path, monkeypatch, page=True)
    product(data_dir, "p1", "Setup", 2500, product_type="service")
    body = page(product_id="p1")["body"]
    assert "In stock" not in body and "Out of stock" not in body


def test_an_unknown_product_is_a_friendly_404_not_a_traceback(
        tmp_path, monkeypatch):
    """A mistyped link is a shopper who is still in the shop."""
    setup_env(tmp_path, monkeypatch, page=True)
    result = page(product_id="no-such-thing")
    assert result["status"] == 404
    assert result["content_type"].startswith("text/html")
    assert "Back to shop" in result["body"]
    assert "Traceback" not in result["body"]


def test_a_withdrawn_product_reads_the_same_as_a_missing_one(
        tmp_path, monkeypatch):
    """Distinguishing them would tell a stranger which product ids exist."""
    data_dir = setup_env(tmp_path, monkeypatch, page=True)
    product(data_dir, "p1", "Retired Mug", 1000, is_active="false")
    assert page(product_id="p1")["status"] == 404


def test_the_page_reuses_the_basket_the_cookie_names(tmp_path, monkeypatch):
    """The bug this whole change exists for: a returning shopper's basket
    was minted fresh every visit, so nothing ever survived a page load."""
    data_dir = setup_env(tmp_path, monkeypatch, page=True)
    product(data_dir, "p1", "Mug", 1200, product_type="service")
    cart("add", product_id="p1", quantity="2")

    result = page()
    assert "24.00" in result["body"]                     # the basket total
    # A cookie the browser already holds is not re-set on every response.
    assert "set_cookie" not in result


def test_a_shopper_with_no_cookie_is_given_one(tmp_path, monkeypatch):
    """Path-scoped, http-only, SameSite=Lax: it says WHICH basket, never
    who, and it is useless anywhere but here."""
    setup_env(tmp_path, monkeypatch, page=True)
    result = page(_cookies={})
    cookie = result["set_cookie"]
    assert cookie.startswith("cart=")
    assert "Path=/" in cookie and "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie and "Max-Age=1209600" in cookie
    # The old key is gone: the server accepts `set_cookie` and nothing else.
    assert "headers" not in result


# =============================================================================
# TAX AND POSTAGE
#
# The shop charged neither, which was a compliance problem and a straight
# money leak: every parcel went out with the delivery paid by the seller.
# What is worth testing here is not that a percentage can be multiplied --
# it is that the percentage is applied ONCE to the whole sale, that postage
# is a line a customer can read rather than a number welded into a total,
# and that a shop which has configured none of it bills exactly as it did
# before. Absent configuration must look like today, never like a broken
# tax line.
# =============================================================================

# --- the arithmetic, with no data directory in sight -------------------------

def test_tax_rounds_half_up_once_over_the_whole_sale():
    """Four fifty-cent lines at 5% are 10c of tax, not 12c.

    Rounded per line, 2.5c becomes 3c four times over and the shop
    overcharges by 2c that no customer can reconcile against the rate
    printed on the bill. Tax is owed on a sale, not on each row of it.
    """
    items = [{"id": f"i{n}", "quantity": "1", "unit_price_cents": "50"}
             for n in range(4)]
    folded = object_cart.checkout_totals(items, tax_rate_bps=500)
    assert folded["subtotal_cents"] == 200
    assert folded["tax_cents"] == 10                    # 10.0, not 4 x 3
    assert sum(object_cart.tax_cents(50, 500) for _ in range(4)) == 12
    assert folded["total_cents"] == 210


def test_a_rate_of_zero_is_a_real_answer_not_a_missing_setting():
    """Plenty of small sellers owe none, and must be able to say so."""
    assert object_cart.tax_cents(10_000, 0) == 0
    assert object_cart.tax_cents(0, 1500) == 0
    assert object_cart.tax_cents(10_000, 1500) == 1500        # 1500bps = 15%
    assert object_cart.tax_cents(1099, 825) == 91             # 90.6675 -> 91


def test_postage_is_flat_until_the_basket_earns_its_way_past_the_threshold():
    """The boundary is AT the threshold, not past it: a shop advertising
    'free over $50' and then charging on a $50 basket has lied on its own
    banner."""
    assert object_cart.shipping_cents(4999, 500, 5000) == 500
    assert object_cart.shipping_cents(5000, 500, 5000) == 0     # exactly
    assert object_cart.shipping_cents(5001, 500, 5000) == 0
    # No threshold configured: postage is always charged.
    assert object_cart.shipping_cents(1_000_000, 500, 0) == 500


def test_a_flat_rate_of_zero_disables_shipping_entirely():
    """A digital-only shop charges no postage and must not be made to show
    a zero line for it."""
    assert object_cart.shipping_cents(10_000, 0, 0) == 0
    folded = object_cart.checkout_totals(
        [{"id": "a", "quantity": "1", "unit_price_cents": "1000"}],
        shipping_flat_cents=0)
    assert folded["shipping_cents"] == 0
    assert folded["shipping_free"] is False     # not free -- simply not sold
    assert folded["total_cents"] == 1000


def test_the_flag_decides_whether_the_postage_itself_is_taxed():
    """Jurisdictions genuinely disagree about this, so the seller chooses."""
    items = [{"id": "a", "quantity": "1", "unit_price_cents": "1000"}]
    goods_only = object_cart.checkout_totals(
        items, tax_rate_bps=1000, shipping_flat_cents=500, tax_shipping=False)
    assert goods_only["tax_cents"] == 100                # 10% of 1000
    assert goods_only["total_cents"] == 1600

    with_postage = object_cart.checkout_totals(
        items, tax_rate_bps=1000, shipping_flat_cents=500, tax_shipping=True)
    assert with_postage["tax_cents"] == 150              # 10% of 1500
    assert with_postage["total_cents"] == 1650


def test_free_shipping_is_distinguishable_from_no_shipping():
    """A bare 0 cannot tell 'you saved the postage' from 'this shop does
    not post things', and only one of those is worth saying out loud."""
    items = [{"id": "a", "quantity": "1", "unit_price_cents": "5000"}]
    earned = object_cart.checkout_totals(
        items, shipping_flat_cents=500, free_over_cents=5000)
    assert earned["shipping_cents"] == 0 and earned["shipping_free"] is True
    never = object_cart.checkout_totals(items, shipping_flat_cents=0)
    assert never["shipping_cents"] == 0 and never["shipping_free"] is False


# --- what checkout writes down ------------------------------------------------

TAXED_AND_POSTED = (("shop.tax_rate_bps", "1000"),
                    ("shop.shipping_flat_cents", "500"))


def sold(data_dir, *, cents=1000, quantity="1", products=1):
    """Put something untracked in the basket -- the money is the subject
    here, not the shelf."""
    for n in range(products):
        product(data_dir, f"p{n}", f"Thing {n}", cents, product_type="service")
        cart("add", product_id=f"p{n}", quantity=quantity)


def test_checkout_charges_postage_as_a_line_and_tax_as_a_total(
        tmp_path, monkeypatch):
    """The bill a customer can actually check: every line adds up to the
    subtotal, the tax is stated, and the three come to the total."""
    data_dir = setup_env(tmp_path, monkeypatch, settings=TAXED_AND_POSTED)
    sold(data_dir, cents=1000)
    result = checkout(customer_email="buyer@example.test")

    assert result["subtotal_cents"] == 1000
    assert result["shipping_cents"] == 500
    assert result["tax_cents"] == 100           # 10% of goods; postage untaxed
    assert result["total_cents"] == 1600        # what the payer owes

    lines = sorted(invoice_lines(data_dir), key=lambda row: row["description"])
    assert [line["description"] for line in lines] == ["Shipping", "Thing 0"]
    shipping_line = lines[0]
    assert shipping_line["quantity"] == "1"
    assert shipping_line["unit_price_cents"] == "500"
    assert shipping_line["line_total_cents"] == "500"
    # Postage is not taxable here, so the line says nothing about tax.
    assert not shipping_line["tax_rate_bps"]

    invoice = invoices(data_dir)[0]
    assert invoice["subtotal_cents"] == "1500"          # goods + postage
    assert invoice["tax_cents"] == "100"
    assert invoice["total_cents"] == "1600"
    # The property that makes a bill checkable rather than disputable.
    assert (sum(int(line["line_total_cents"]) for line in lines)
            + int(invoice["tax_cents"])) == int(invoice["total_cents"])


def test_the_order_records_the_tax_and_the_grand_total(tmp_path, monkeypatch):
    """Subtotal stays goods -- it is defined as the sum of the order's own
    lines, and postage is not one of them. Postage on an order is implied
    by total - subtotal - tax until orders grows a shipping_cents column;
    the itemisation lives on the invoice, where a customer reads it."""
    data_dir = setup_env(tmp_path, monkeypatch, settings=TAXED_AND_POSTED)
    sold(data_dir, cents=1000)
    checkout(customer_email="buyer@example.test")

    order = orders(data_dir)[0]
    assert order["subtotal_cents"] == "1000"
    assert order["tax_cents"] == "100"
    assert order["total_cents"] == "1600"
    assert (int(order["total_cents"]) - int(order["subtotal_cents"])
            - int(order["tax_cents"])) == 500


def test_taxable_postage_says_so_on_its_own_line(tmp_path, monkeypatch):
    """Somebody auditing 'was delivery taxed?' should find the answer on
    the line, not reverse-engineer it out of a total."""
    data_dir = setup_env(tmp_path, monkeypatch, settings=(
        ("shop.tax_rate_bps", "1000"), ("shop.tax_shipping", "true"),
        ("shop.shipping_flat_cents", "500")))
    sold(data_dir, cents=1000)
    result = checkout(customer_email="buyer@example.test")

    assert result["tax_cents"] == 150               # 10% of 1000 + 500
    assert result["total_cents"] == 1650
    shipping_line = [l for l in invoice_lines(data_dir)
                     if l["description"] == "Shipping"][0]
    assert shipping_line["tax_rate_bps"] == "1000"
    assert shipping_line["line_tax_cents"] == "50"


def test_a_basket_over_the_threshold_ships_free_and_is_billed_that_way(
        tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch, settings=(
        ("shop.shipping_flat_cents", "500"),
        ("shop.shipping_free_over_cents", "2000")))
    sold(data_dir, cents=2000)                      # exactly the threshold
    result = checkout(customer_email="buyer@example.test")

    assert result["shipping_cents"] == 0 and result["shipping_free"] is True
    assert result["total_cents"] == 2000
    # No postage charged means no postage line: a free-shipping invoice
    # with a "Shipping 0.00" row on it invites a question that has no
    # answer.
    assert [l["description"] for l in invoice_lines(data_dir)] == ["Thing 0"]


def test_rounding_once_survives_the_whole_checkout(tmp_path, monkeypatch):
    """The same 10c-not-12c case, driven end to end -- because the fold
    being right is worth nothing if checkout re-derives tax per line on
    its way to the invoice."""
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("shop.tax_rate_bps", "500"),))
    sold(data_dir, cents=50, products=4)
    result = checkout(customer_email="buyer@example.test")
    assert result["subtotal_cents"] == 200
    assert result["tax_cents"] == 10
    assert result["total_cents"] == 210
    assert invoices(data_dir)[0]["tax_cents"] == "10"


def test_a_shop_that_configures_nothing_bills_exactly_as_it_did(
        tmp_path, monkeypatch):
    """The regression that matters most: absent configuration must be
    invisible. No postage line, no tax, and an invoice line carrying the
    same populated fields it carried before any of this existed."""
    data_dir = setup_env(tmp_path, monkeypatch)         # no settings at all
    sold(data_dir, cents=1200, quantity="2")
    result = checkout(customer_email="buyer@example.test")

    assert result["total_cents"] == 2400 == result["subtotal_cents"]
    assert result["shipping_cents"] == 0 and result["tax_cents"] == 0

    lines = invoice_lines(data_dir)
    assert len(lines) == 1                              # no Shipping row
    populated = {k for k, v in lines[0].items() if str(v or "").strip()}
    # line_tax_cents is the schema's own default of "0" and was there
    # before any of this; tax_rate_bps has no default and stays empty,
    # which is what "this line was never taxed" looks like on disk.
    assert populated == {"id", "invoice_id", "description", "quantity",
                         "unit_price_cents", "line_total_cents", "owner_id",
                         "created_at", "line_tax_cents"}
    assert lines[0]["line_tax_cents"] == "0"
    assert lines[0]["tax_rate_bps"] == ""
    invoice = invoices(data_dir)[0]
    assert invoice["subtotal_cents"] == "2400" == invoice["total_cents"]
    assert invoice["tax_cents"] == "0"
    assert orders(data_dir)[0]["total_cents"] == "2400"


def test_preview_shows_the_whole_breakdown_before_anybody_commits(
        tmp_path, monkeypatch):
    """A preview exists so somebody can see what they are about to owe; a
    subtotal alone hides the two numbers they most want to check."""
    data_dir = setup_env(tmp_path, monkeypatch, settings=TAXED_AND_POSTED)
    sold(data_dir, cents=1000)
    result = checkout(preview="true")
    assert result["subtotal_cents"] == 1000
    assert result["shipping_cents"] == 500
    assert result["tax_cents"] == 100
    assert result["total_cents"] == 1600
    assert orders(data_dir) == [] and invoices(data_dir) == []


# --- what the shopper sees before deciding -------------------------------------

def test_the_basket_page_breaks_the_total_down(tmp_path, monkeypatch):
    """A shopper who meets a surprise at the last step abandons the
    basket. The footer is where postage and tax stop being a surprise."""
    data_dir = setup_env(tmp_path, monkeypatch, settings=TAXED_AND_POSTED,
                         page=True)
    product(data_dir, "p1", "Mug", 1000, product_type="service")
    cart("add", product_id="p1")
    body = page()["body"]
    assert "Subtotal" in body and "10.00" in body
    assert "Shipping" in body and "5.00" in body
    assert "Tax" in body and "1.00" in body
    assert "16.00" in body                              # the total


def test_the_basket_page_says_free_shipping_when_it_is_earned(
        tmp_path, monkeypatch):
    """The one delighter in a basket. A silent 0.00 reads as a shop that
    forgot to charge, not one that gave the shopper something."""
    data_dir = setup_env(tmp_path, monkeypatch, page=True, settings=(
        ("shop.shipping_flat_cents", "500"),
        ("shop.shipping_free_over_cents", "2000")))
    product(data_dir, "p1", "Mug", 2000, product_type="service")
    cart("add", product_id="p1")
    body = page()["body"]
    assert "Free shipping" in body
    assert "Subtotal" in body


def test_the_basket_page_is_untouched_when_nothing_is_configured(
        tmp_path, monkeypatch):
    """Zero-rows are not neutral: 'Shipping 0.00' and 'Tax 0.00' read as a
    broken shop rather than one that does not do those things."""
    data_dir = setup_env(tmp_path, monkeypatch, page=True)
    product(data_dir, "p1", "Mug", 1200, product_type="service")
    cart("add", product_id="p1", quantity="2")
    body = page()["body"]
    assert "Shipping" not in body and "Subtotal" not in body
    assert "24.00" in body
    assert '<tfoot><tr><th colspan="3">Total</th>' in body


# =============================================================================
# MERCHANDISING
#
# Categories, variants and the two words a shopper types at checkout. The
# basket did not change to support any of it -- a variant IS a product, so
# every add, price check and stock check here is the same code path it
# always was -- which is exactly why the tests belong beside the basket's:
# what they hold is that the page collapses and groups without the basket
# learning a new noun, and that the one refusal this adds says what to do
# instead. The full variant/image/gift story lives in
# tests/test_merchandising.py; this section is the basket's own view of it.
# =============================================================================

def variants(data_dir, *, parent_cents=0):
    """A parent that is a heading and one variant that is a real thing."""
    product(data_dir, "tote", "Tote Bag", parent_cents, category="Bags")
    product(data_dir, "tote-m", "Tote Bag, medium", 2000, category="Bags",
            product_type="service", parent_product_id="tote",
            options='{"size": "M", "colour": "navy"}')


def test_the_index_groups_by_category_and_never_hides_the_unfiled(
        tmp_path, monkeypatch):
    """Alphabetical headings, uncategorised last under "Everything else".
    Last, not gone: a product nobody got round to filing is still stock
    somebody paid for."""
    data_dir = setup_env(tmp_path, monkeypatch, page=True)
    product(data_dir, "p1", "Enamel Mug", 1200, category="Kitchen")
    product(data_dir, "p2", "Linocut Print", 4000, category="Art")
    product(data_dir, "p3", "Odd Thing", 500)
    body = page()["body"]

    assert '<h2 class="shop-category">Art</h2>' in body
    assert '<h2 class="shop-category">Everything else</h2>' in body
    assert body.index("Art</h2>") < body.index("Kitchen</h2>")
    assert body.index("Kitchen</h2>") < body.index("Everything else</h2>")
    assert "Odd Thing" in body[body.index("Everything else</h2>"):]


def test_a_parent_shows_one_card_and_the_basket_refuses_it_by_name(
        tmp_path, monkeypatch):
    """The card is a doorway, not an Add button: the parent has no price
    of its own, so there is nothing to charge and nothing to pick. The
    refusal names the options, because a "no" that does not say what to do
    instead is a lost sale."""
    data_dir = setup_env(tmp_path, monkeypatch, page=True)
    variants(data_dir)
    body = page()["body"]

    assert body.count('<div class="shop-card">') == 1
    assert "Tote Bag, medium" not in body               # collapsed, not listed
    assert "Choose options" in body and "from USD 20.00" in body
    assert 'name="do" value="add"' not in body

    refused = cart("add", product_id="tote")
    assert refused["status"] == 409
    assert "M / navy" in refused["error"]
    assert refused["options"][0]["product_id"] == "tote-m"


def test_adding_a_variant_is_an_ordinary_add(tmp_path, monkeypatch):
    """The whole argument, in the basket: a product_id went in and a
    description came out. Nothing here knows what a variant is."""
    data_dir = setup_env(tmp_path, monkeypatch, page=True)
    variants(data_dir)
    assert cart("add", product_id="tote-m")["subtotal_cents"] == 2000
    item = object_records.read_collection_records("cart_items",
                                                  base_dir=data_dir)[0]
    assert item["description"] == "Tote Bag, medium"

    body = page()["body"]
    assert "Tote Bag, medium" in body                   # the basket line
    assert "20.00" in body


def test_the_checkout_form_carries_the_note_and_the_gift_message(
        tmp_path, monkeypatch):
    """Straight through the page to action_checkout, which decides where
    they land. Both optional, and no gift flag: the packing slip shows no
    prices by construction, so every parcel is already gift-safe."""
    data_dir = setup_env(tmp_path, monkeypatch, page=True)
    product(data_dir, "p1", "Mug", 1200, product_type="service")
    cart("add", product_id="p1")
    assert '<textarea name="customer_note"' in page()["body"]

    result = checkout(customer_email="buyer@example.test",
                      customer_note="Leave with the neighbour",
                      gift_message="Happy birthday, Ada")
    basket = object_records.get_collection_record("carts", result["cart_id"],
                                                  base_dir=data_dir)
    assert basket["customer_note"] == "Leave with the neighbour"
    assert basket["gift_message"] == "Happy birthday, Ada"


def test_a_product_with_no_category_and_no_variants_renders_as_it_always_did(
        tmp_path, monkeypatch):
    """The regression guard. Every field this slice added is optional, and
    a shop that sets none of them must not be able to tell it happened --
    apart from the photograph placeholder, which is the one deliberate
    change to a page that was text-only."""
    data_dir = setup_env(tmp_path, monkeypatch, page=True)
    product(data_dir, "p1", "Enamel Mug", 1200, product_type="service")
    body = page()["body"]

    assert '<a href="/shop/p1">Enamel Mug</a>' in body
    assert "USD 12.00" in body
    assert 'name="product_id" value="p1"' in body
    assert '<h2 class="shop-category">' not in body
    assert '<div class="shop-image placeholder"></div>' in body
    assert cart("add", product_id="p1", quantity="2")["subtotal_cents"] == 2400


# --- money bugs found by walking the live shop -------------------------------
#
# Neither of these was caught by the suite, and the reason is worth
# recording: the tests exercised objects directly, while the live server
# runs them through the event dispatcher. The gap between "the object is
# right" and "the system is right" is exactly where both of these lived.

def test_a_totals_recompute_never_zeroes_tax_it_cannot_see(tmp_path, monkeypatch):
    """The live failure: a customer was quoted 2116 at checkout, and by the
    time the invoice lines had been written the totals handler had restated
    the invoice to 2000 with tax 0 -- because tax is computed once over the
    whole sale and stamped on the DOCUMENT, and that pass only reads lines.
    A fold must not replace a value it cannot reproduce."""
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("shop.tax_rate_bps", "825"),
                                   ("shop.shipping_flat_cents", "600")))
    product(data_dir, "p1", "Mug", 1400, product_type="service")
    cart("add", product_id="p1")
    quoted = checkout(customer_email="grace@example.test")

    invoice = object_records.get_collection_record(
        "invoices", quoted["invoice_id"], base_dir=data_dir)
    assert int(invoice["total_cents"]) == quoted["total_cents"]

    # Now run the recompute the live server runs on every line write.
    object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "system_invoice_totals", method="EVENT",
            payload={"collection": "invoice_lines", "action": "created",
                     "record_id": ""}),
        roots=[PACKAGES / "app-invoices" / "objects"])

    after = object_records.get_collection_record(
        "invoices", quoted["invoice_id"], base_dir=data_dir)
    assert int(after["total_cents"]) == quoted["total_cents"], (
        "the recompute silently dropped the tax the customer agreed to")
    assert int(after["tax_cents"]) > 0


def test_a_part_payment_does_not_ship_the_goods(tmp_path, monkeypatch):
    """Goods leave when the BILL is settled, not when money merely
    arrives. Paying 14.00 against a 20.00 invoice used to fulfil the order
    in full while the invoice sat at `partial` saying otherwise."""
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("shop.stock_location", "loc-shelf"),
                                   ("shop.customer_location", "loc-customer")))
    order_id, _ = paid_order(data_dir)          # pays in full
    order = object_records.get_collection_record("orders", order_id,
                                                 base_dir=data_dir)
    invoice_id = order["invoice_id"]

    # A second order, deliberately underpaid.
    cart("add", product_id="p1", quantity="2")
    second = checkout(customer_email="short@example.test")
    part = object_records.create_collection_record(
        "payments",
        {"id": object_ids.new_uuid4(), "invoice_id": second["invoice_id"],
         "amount_cents": "1", "method": "card", "received_on": "2026-07-26",
         "status": "received", "owner_id": "shop"},
        base_dir=data_dir)

    result = fulfil(part)
    assert "not settled" in result["skipped"]
    assert object_records.get_collection_record(
        "orders", second["order_id"], base_dir=data_dir)["status"] == "draft"
    assert invoice_id       # the fully paid one is untouched by this test


def test_paying_the_balance_later_does_ship(tmp_path, monkeypatch):
    """A deposit followed by the balance must work: the payment that tips
    the invoice to settled is the one that ships."""
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("shop.stock_location", "loc-shelf"),
                                   ("shop.customer_location", "loc-customer")))
    location(data_dir, "loc-shelf", "Shelf")
    location(data_dir, "loc-customer", "Customers", kind="customer")
    product(data_dir, "p1", "Mug", 1200)
    stock_in(data_dir, "p1", 10)
    cart("add", product_id="p1", quantity="1")
    order = checkout(customer_email="deposit@example.test")
    total = order["total_cents"]

    half = object_records.create_collection_record(
        "payments",
        {"id": object_ids.new_uuid4(), "invoice_id": order["invoice_id"],
         "amount_cents": str(total // 2), "method": "card",
         "received_on": "2026-07-26", "status": "received", "owner_id": "shop"},
        base_dir=data_dir)
    assert "not settled" in fulfil(half)["skipped"]

    rest = object_records.create_collection_record(
        "payments",
        {"id": object_ids.new_uuid4(), "invoice_id": order["invoice_id"],
         "amount_cents": str(total - total // 2), "method": "card",
         "received_on": "2026-07-26", "status": "received", "owner_id": "shop"},
        base_dir=data_dir)
    assert fulfil(rest)["confirmed"] is True


# --- one payment, one shipment -------------------------------------------------

def test_two_concurrent_deliveries_of_one_payment_ship_once(tmp_path,
                                                            monkeypatch):
    """Goods leaving twice against one payment, reproduced before it was
    fixed: two shipments, FOUR units against an order of two, two sale
    moves.

    Nothing in the shipping layer could have stopped it.
    action_create_shipment works out what is unshipped by reading
    shipment_lines, and hook_shipment_lines refuses an over-ship by summing
    them -- but both are check-then-write, and a hook runs BEFORE the write
    lock, so both passes read an empty history and both wrote. Layered
    advisory checks do not compose into an atomic one.

    The fix is the one thing on this box that IS atomic: a compare-and-set
    on a single record (63). The claim is the write that moves the order
    out of the shippable set, and the loser finds the row already changed.
    """
    import threading

    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("shop.stock_location", "loc-shelf"),
                                   ("shop.customer_location", "loc-customer")))
    with_shipping(tmp_path, monkeypatch, data_dir)
    order_id, payment = paid_order(data_dir)

    out = []
    threads = [threading.Thread(target=lambda: out.append(fulfil(payment)))
               for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = object_records.read_collection_records("shipment_lines",
                                                    base_dir=data_dir)
    shipments = object_records.read_collection_records("shipments",
                                                        base_dir=data_dir)
    sales = [m for m in moves(data_dir) if m["reason"] == "sale"]

    assert len(shipments) == 1, out
    assert sum(float(line["quantity"]) for line in lines) == 2.0
    assert len(sales) == 1

    # And the loser says so rather than reporting a silent success.
    skipped = [r for r in out if r.get("skipped")]
    assert len(skipped) == 1
    assert "claimed" in skipped[0]["skipped"] or "handling" in skipped[0]["skipped"]


def test_an_order_waiting_on_the_pick_list_is_not_claimed(tmp_path, monkeypatch):
    """With auto_fulfill off the order is supposed to SIT at `confirmed`
    waiting for a person. Claiming it would take it off the very list it
    needs to be on."""
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("shop.stock_location", "loc-shelf"),
                                   ("shop.customer_location", "loc-customer"),
                                   ("shop.auto_fulfill", "false")))
    with_shipping(tmp_path, monkeypatch, data_dir)
    order_id, payment = paid_order(data_dir)

    result = fulfil(payment)
    assert result["shipped"] is False
    assert "pick list" in result["note"]

    order = object_records.get_collection_record("orders", order_id,
                                                  base_dir=data_dir)
    assert order["status"] == "confirmed"


def test_an_already_confirmed_order_is_also_shipped_only_once(tmp_path,
                                                              monkeypatch):
    """The case the draft->confirmed promote cannot cover, and the reason
    the claim exists as a second step.

    A deposit followed by a balance payment is the ordinary shape: the
    first payment confirms the order without settling the invoice, and the
    SECOND is the one that tips it to paid and ships. That second event
    finds the order already at `confirmed`, so the promote above is not
    reached and only the claim stands between two concurrent deliveries
    and two shipments.
    """
    import threading

    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("shop.stock_location", "loc-shelf"),
                                   ("shop.customer_location", "loc-customer")))
    with_shipping(tmp_path, monkeypatch, data_dir)
    order_id, payment = paid_order(data_dir)

    object_records.update_collection_record(
        "orders", order_id, {"status": "confirmed"}, base_dir=data_dir)

    out = []
    threads = [threading.Thread(target=lambda: out.append(fulfil(payment)))
               for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    shipments = object_records.read_collection_records("shipments",
                                                        base_dir=data_dir)
    lines = object_records.read_collection_records("shipment_lines",
                                                    base_dir=data_dir)
    assert len(shipments) == 1, out
    assert sum(float(line["quantity"]) for line in lines) == 2.0
    assert len([m for m in moves(data_dir) if m["reason"] == "sale"]) == 1
