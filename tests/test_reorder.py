"""Backorders and reorder points: selling what is not on the shelf, and
noticing before the shelf is empty.

Three claims.

**The default is yesterday.** products.backorder_policy defaults to
`refuse`, so a catalogue that has never heard of the field -- which is
every catalogue on every box today -- behaves byte-for-byte as it did.
That regression is the first test in this file and it is the one that
makes the rest of the feature safe to ship.

**Refusing while remembering beats refusing and forgetting.** `notify`
still turns the customer away, and writes down that they came, because
losing the sale is survivable and losing the customer twice is not.

**The pass suggests; the person decides.** system_reorder_check folds the
stock ledger and writes a suggestion. It does not raise a purchase order
and it never will: the fold can see the shelf and cannot see the lead
time, the case size, the supplier's minimum or the cash in the bank, and
a machine that orders on its own is how a business ends up with forty
pallets of the wrong thing.

The two halves that are NOT done are held at the bottom as strict xfails,
the same way tests/test_dropship.py held its two: each needs one
condition in a package this slice does not own (app-orders' order_lines
column, app-shipping's fulfilment gate), and a green test over a shelf
that silently ships is worse than a red one.
"""

import pathlib
import shutil

import pytest
from conftest import stage_collection

import object_cart
import object_execution
import object_ids
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
SHOP_OBJECTS = PACKAGES / "app-shop" / "objects"
CATALOG_OBJECTS = PACKAGES / "app-catalog" / "objects"
SHIPPING_OBJECTS = PACKAGES / "app-shipping" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

TOKEN = "sess-back"
TODAY = "2026-06-15"


# --- the pure decision, with no data directory in sight -----------------------

def test_a_product_that_has_never_heard_of_the_field_still_refuses():
    """The whole safety of this feature is in one default. A blank, a
    missing column and a value typed wrong must all read as `refuse`."""
    for product in ({}, {"backorder_policy": ""},
                    {"backorder_policy": "yes please"}, None):
        assert object_cart.backorder_policy(product) == "refuse"
    assert object_cart.backorder_policy({"backorder_policy": "allow"}) == "allow"


def test_out_of_stock_splits_three_ways_and_only_one_of_them_sells():
    items = [{"id": "a", "product_id": "strict", "quantity": "5"},
             {"id": "b", "product_id": "loose", "quantity": "5"},
             {"id": "c", "product_id": "curious", "quantity": "5"}]
    products = {
        "strict": {"is_active": "true", "backorder_policy": "refuse"},
        "loose": {"is_active": "true", "backorder_policy": "allow"},
        "curious": {"is_active": "true", "backorder_policy": "notify"},
    }
    on_hand = {"strict": 1, "loose": 1, "curious": 1}
    blockers = object_cart.checkout_blockers(
        items, products, on_hand, tracked=set(products))

    assert [row["product_id"] for row in blockers["unavailable"]] == [
        "strict", "curious"]
    assert [row["product_id"] for row in blockers["backordered"]] == ["loose"]
    assert [row["product_id"] for row in blockers["notify"]] == ["curious"]
    assert blockers["can_checkout"] is False        # two of them still block

    # With only the backorderable line short, the basket goes through.
    only_loose = object_cart.checkout_blockers(
        [items[1]], products, on_hand, tracked=set(products))
    assert only_loose["can_checkout"] is True
    assert only_loose["backordered"][0]["short_by"] == "4"


# --- the shop -----------------------------------------------------------------

def setup_env(tmp_path, monkeypatch, *, with_shipping=False):
    data_dir = tmp_path / "data"
    for pkg, name in (("app-shop", "carts"), ("app-shop", "cart_items"),
                      ("app-catalog", "products"), ("app-catalog", "locations"),
                      ("app-catalog", "stock_moves"),
                      ("app-catalog", "backorders"),
                      ("app-catalog", "reorder_suggestions"),
                      ("app-orders", "orders"), ("app-orders", "order_lines"),
                      ("app-invoices", "invoices"),
                      ("app-invoices", "invoice_lines"),
                      ("app-payments", "payments")):
        stage_collection(data_dir, pkg, name)
    if with_shipping:
        for name in ("shipments", "shipment_lines"):
            stage_collection(data_dir, "app-shipping", name)
    stage_collection(data_dir, "app-settings", "app_settings")

    objects_root = tmp_path / "objects"
    sources = [SHOP_OBJECTS, CATALOG_OBJECTS]
    if with_shipping:
        sources.append(SHIPPING_OBJECTS)
    for source in sources:
        shutil.copytree(source, objects_root, dirs_exist_ok=True)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_root))
    object_records.create_collection_record(
        "locations",
        {"id": "loc-shelf", "name": "Shelf", "location_type": "warehouse",
         "owner_id": "shop"}, base_dir=data_dir)
    return data_dir, objects_root


def product(data_dir, product_id, name, cents, **fields):
    record = {"id": product_id, "name": name, "sku": product_id.upper(),
              "product_type": "physical", "price_cents": str(cents),
              "currency": "USD", "is_active": "true", "owner_id": "shop"}
    record.update({k: str(v) for k, v in fields.items()})
    return object_records.create_collection_record("products", record,
                                                    base_dir=data_dir)


def stock_in(data_dir, product_id, quantity):
    return object_records.create_collection_record(
        "stock_moves",
        {"id": object_ids.new_uuid4(), "product_id": product_id,
         "to_location_id": "loc-shelf", "quantity": str(quantity),
         "reason": "purchase", "occurred_at": "2026-06-01", "owner_id": "shop"},
        base_dir=data_dir)


def run(object_id, payload, objects_root, *, method="POST"):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(object_id, method=method,
                                                payload=payload),
        roots=[objects_root]).result


def cart(objects_root, action="get", **payload):
    return run("action_cart", {"session_token": TOKEN, "action": action,
                               **payload}, objects_root)


def checkout(objects_root, **payload):
    payload.setdefault("today", TODAY)
    return run("action_checkout", {"session_token": TOKEN, **payload},
               objects_root)


def rows(data_dir, collection):
    return object_records.read_collection_records(collection, base_dir=data_dir)


# --- backorders ---------------------------------------------------------------

def test_refuse_behaves_exactly_as_the_shop_behaved_yesterday(tmp_path, monkeypatch):
    """The regression that makes the rest of this shippable: a product
    with no policy set is refused with the same numbers, in the same
    shape, and writes no backorder row at all."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Enamel Mug", 1000)
    stock_in(data_dir, "p1", 1)
    cart(objects_root, "add", product_id="p1", quantity="3")

    refused = checkout(objects_root, customer_email="ada@example.com")
    assert refused["status"] == 409
    assert refused["error"] == "Some items cannot be ordered right now."
    short = refused["unavailable"][0]
    assert short["product_id"] == "p1"
    assert short["wanted"] == "3" and short["available"] == "1"
    assert short["short_by"] == "2"
    assert not rows(data_dir, "orders")
    assert not rows(data_dir, "backorders")


def test_allow_takes_the_order_and_marks_what_is_owed(tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Enamel Mug", 1000, backorder_policy="allow")
    stock_in(data_dir, "p1", 1)
    cart(objects_root, "add", product_id="p1", quantity="3")

    placed = checkout(objects_root, customer_email="ada@example.com")
    assert placed["ok"] is True
    assert placed["total_cents"] == 3000            # the whole line is sold
    assert placed["backordered"][0]["short_by"] == "2"
    assert "backordered" in placed["note"]

    waiting = rows(data_dir, "backorders")
    assert len(waiting) == 1
    assert waiting[0]["kind"] == "backorder"
    assert waiting[0]["quantity"] == "2"
    assert waiting[0]["status"] == "open"
    assert waiting[0]["order_id"] == placed["order_id"]
    assert waiting[0]["customer_email"] == "ada@example.com"
    assert waiting[0]["requested_on"] == TODAY
    # The row names the LINE, which is what lets fulfilment leave it behind
    # while the rest of the order goes out today.
    line = rows(data_dir, "order_lines")[0]
    assert waiting[0]["order_line_id"] == line["id"]


def test_notify_still_refuses_but_writes_down_who_asked(tmp_path, monkeypatch):
    """Refusing while remembering beats refusing and forgetting, which is
    how the same customer is lost twice."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Enamel Mug", 1000, backorder_policy="notify")
    stock_in(data_dir, "p1", 0)
    cart(objects_root, "add", product_id="p1", quantity="2")

    refused = checkout(objects_root, customer_email="ada@example.com")
    assert refused["status"] == 409
    assert refused["unavailable"]                    # still a refusal
    assert not rows(data_dir, "orders")

    interest = rows(data_dir, "backorders")
    assert len(interest) == 1
    assert interest[0]["kind"] == "notify"
    assert interest[0]["order_id"] == ""             # nothing was sold
    assert interest[0]["customer_email"] == "ada@example.com"
    assert refused["interest_recorded"][0]["product_id"] == "p1"

    # And pressing the button five times is one person waiting, not five.
    checkout(objects_root, customer_email="ada@example.com")
    checkout(objects_root, customer_email="ADA@example.com")
    assert len(rows(data_dir, "backorders")) == 1


def test_a_preview_of_a_refused_basket_files_no_interest(tmp_path, monkeypatch):
    """A preview is a look. The rule that a preview writes nothing has to
    hold on the refusal path too, or a storefront that quotes before it
    submits files a waiting-list entry for everybody who glanced at an
    empty shelf."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Enamel Mug", 1000, backorder_policy="notify")
    stock_in(data_dir, "p1", 0)
    cart(objects_root, "add", product_id="p1", quantity="1")

    quoted = checkout(objects_root, preview="true",
                      customer_email="ada@example.com")
    assert quoted["status"] == 409
    assert rows(data_dir, "backorders") == []


def test_interest_with_nowhere_to_send_it_is_not_recorded(tmp_path, monkeypatch):
    """A record of interest with no way to reach the interested party is a
    record of nothing."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Enamel Mug", 1000, backorder_policy="notify")
    stock_in(data_dir, "p1", 0)
    cart(objects_root, "add", product_id="p1", quantity="1")

    refused = checkout(objects_root)                 # no email at all
    assert refused["status"] == 409
    assert rows(data_dir, "backorders") == []
    assert "interest_recorded" not in refused


def test_the_backorder_count_is_the_open_ones_and_names_the_longest_wait(tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Mug", 1000)
    for row_id, status, day in (("b1", "open", "2026-06-01"),
                                ("b2", "open", "2026-06-10"),
                                ("b3", "filled", "2026-01-01")):
        object_records.create_collection_record(
            "backorders",
            {"id": row_id, "product_id": "p1", "kind": "backorder",
             "quantity": "1", "status": status, "requested_on": day,
             "owner_id": "shop"}, base_dir=data_dir)

    counted = run("system_backorder_attention", {}, objects_root,
                  method="COUNT")
    assert counted["count"] == 2
    assert "oldest waiting" in counted["detail"]


# --- reorder points -----------------------------------------------------------

def reorder(objects_root, **payload):
    payload.setdefault("today", TODAY)
    return run("system_reorder_check", payload, objects_root)


def test_the_pass_suggests_and_never_orders(tmp_path, monkeypatch):
    """A machine that raises purchase orders on its own is how a business
    orders forty pallets of the wrong thing."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Enamel Mug", 1000, reorder_point=5,
            reorder_quantity=24)
    stock_in(data_dir, "p1", 3)

    result = reorder(objects_root)
    assert result["suggested"] == 1
    assert result["ordered"] == 0
    assert "never raises a purchase order" in result["note"]

    suggestion = rows(data_dir, "reorder_suggestions")[0]
    assert suggestion["product_id"] == "p1"
    assert suggestion["on_hand"] == "3"
    assert suggestion["reorder_point"] == "5"
    assert suggestion["suggested_quantity"] == "24"
    assert suggestion["status"] == "open"
    assert suggestion["suggested_on"] == TODAY

    # Nothing anywhere that looks like a purchase.
    assert not rows(data_dir, "orders")
    assert not rows(data_dir, "stock_moves") or all(
        row["reason"] == "purchase" and row["id"] != suggestion["id"]
        for row in rows(data_dir, "stock_moves"))


def test_the_threshold_is_at_or_below_and_not_merely_below(tmp_path, monkeypatch):
    """'Reorder at 5' means five is already the moment. A shop that had to
    fall to four has been given a number that means something else."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "at", "At the line", 1000, reorder_point=5)
    product(data_dir, "over", "Above it", 1000, reorder_point=5)
    stock_in(data_dir, "at", 5)
    stock_in(data_dir, "over", 6)

    reorder(objects_root)
    assert [row["product_id"] for row in rows(data_dir, "reorder_suggestions")] == ["at"]


def test_a_reorder_point_of_zero_is_off_not_reorder_at_zero(tmp_path, monkeypatch):
    """Most catalogues have a handful of lines anybody actually reorders,
    and a queue lit up by every service and made-to-order item is a queue
    nobody reads."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Never reordered", 1000)          # no point set
    stock_in(data_dir, "p1", 0)

    result = reorder(objects_root)
    assert result["checked"] == 0
    assert rows(data_dir, "reorder_suggestions") == []


def test_an_inactive_product_is_not_reordered(tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Discontinued", 1000, reorder_point=5,
            is_active="false")
    stock_in(data_dir, "p1", 0)
    reorder(objects_root)
    assert rows(data_dir, "reorder_suggestions") == []


def test_a_second_pass_refreshes_the_open_row_instead_of_adding_another(tmp_path, monkeypatch):
    """A nightly pass that appended would turn one true fact into ninety
    within a quarter, which is how a good signal becomes noise."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Enamel Mug", 1000, reorder_point=5,
            reorder_quantity=24)
    stock_in(data_dir, "p1", 3)
    reorder(objects_root)

    stock_in(data_dir, "p1", -1)
    second = reorder(objects_root, today="2026-06-16")
    assert second["suggested"] == 0 and second["refreshed"] == 1

    suggestions = rows(data_dir, "reorder_suggestions")
    assert len(suggestions) == 1
    assert suggestions[0]["on_hand"] == "2"
    assert suggestions[0]["suggested_on"] == "2026-06-16"


def test_a_dismissed_suggestion_is_left_alone_and_stops_counting(tmp_path, monkeypatch):
    """A count nobody can dismiss stays lit forever and teaches people to
    ignore the band it sits in."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Enamel Mug", 1000, reorder_point=5)
    stock_in(data_dir, "p1", 3)
    reorder(objects_root)
    suggestion = rows(data_dir, "reorder_suggestions")[0]

    object_records.update_collection_record(
        "reorder_suggestions", suggestion["id"], {"status": "dismissed"},
        base_dir=data_dir, actor="operator")
    assert run("system_reorder_attention", {}, objects_root,
               method="COUNT")["count"] == 0

    # The next pass raises a fresh one rather than reopening the answered
    # row: the human said no to what they were shown, not to the question
    # forever.
    again = reorder(objects_root, today="2026-06-16")
    assert again["suggested"] == 1
    assert {row["status"] for row in rows(data_dir, "reorder_suggestions")} == {
        "dismissed", "open"}


def test_the_reorder_count_keys_on_the_open_rows_and_names_the_worst(tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "deep", "Very low", 1000, reorder_point=10)
    product(data_dir, "shallow", "Just under", 1000, reorder_point=5)
    stock_in(data_dir, "deep", 2)
    stock_in(data_dir, "shallow", 4)
    reorder(objects_root)

    counted = run("system_reorder_attention", {}, objects_root, method="COUNT")
    assert counted["count"] == 2
    assert counted["detail"] == "one is 8 below its point"


def test_the_pass_costs_nothing_on_a_box_with_no_catalogue(tmp_path, monkeypatch):
    """A pass should not log an error every night about an app nobody
    installed."""
    data_dir = tmp_path / "data"
    (data_dir / "schemas").mkdir(parents=True)
    objects_root = tmp_path / "objects"
    shutil.copytree(CATALOG_OBJECTS, objects_root, dirs_exist_ok=True)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_root))

    assert reorder(objects_root)["suggested"] == 0
    assert run("system_reorder_attention", {}, objects_root,
               method="COUNT") == {"count": 0}
    assert run("system_backorder_attention", {}, objects_root,
               method="COUNT") == {"count": 0}


# --- the halves that are NOT done, held as specifications ---------------------
#
# Each needs one condition in a package this slice does not own, and
# hacking around a sibling package's handler would be exactly the kind of
# "make it work by the data" a stock rule cannot afford. Writing the
# acceptance test first and leaving it red is the honest move -- the same
# thing tests/test_dropship.py did for its two conditions -- because a
# green test over a shelf that silently ships is worse than a red one.
#
# FOLLOW-UP 1 (app-orders): order_lines needs a `backordered` boolean.
#   action_checkout already stamps it through the same _has_field check it
#   uses for shipping_cents and line_note, so the day the column exists
#   this passes with no change here and no change there.
#
# FOLLOW-UP 2 (app-shipping): action_create_shipment defaults to
#   EVERYTHING unshipped, which today includes a line the shop has openly
#   agreed it does not have. It needs to skip lines that are backordered
#   -- readable either from the order_lines column above or from the
#   backorders rows this slice writes -- and system_order_fulfillment must
#   agree, or the shop ships goods that are not in the building and the
#   stock ledger goes negative on a sale nobody could pick.


def test_a_backordered_line_is_marked_on_the_order_itself(tmp_path, monkeypatch):
    """The picker and the packer read the ORDER. A fact that lives only in
    another app's collection is a fact the warehouse never sees."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Enamel Mug", 1000, backorder_policy="allow")
    stock_in(data_dir, "p1", 1)
    cart(objects_root, "add", product_id="p1", quantity="3")
    checkout(objects_root, customer_email="ada@example.com")

    line = rows(data_dir, "order_lines")[0]
    assert line.get("backordered") == "true"


def test_a_backordered_line_does_not_go_in_the_box(tmp_path, monkeypatch):
    """The whole promise of `allow` is that the sale happens and the goods
    follow. Shipping the part that does not exist breaks both halves at
    once: the customer gets a short parcel and the shelf shows minus two."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch, with_shipping=True)
    product(data_dir, "p1", "Enamel Mug", 1000, backorder_policy="allow")
    stock_in(data_dir, "p1", 1)
    cart(objects_root, "add", product_id="p1", quantity="3")
    placed = checkout(objects_root, customer_email="ada@example.com")
    object_records.update_collection_record(
        "orders", placed["order_id"], {"status": "confirmed"},
        base_dir=data_dir, actor="test")

    shipment = run("action_create_shipment",
                   {"order_id": placed["order_id"], "today": TODAY},
                   objects_root)
    packed = [row for row in rows(data_dir, "shipment_lines")
              if row["shipment_id"] == shipment.get("shipment_id")]
    # One on the shelf, two owed: the box holds one.
    assert [row["quantity"] for row in packed] == ["1"]
