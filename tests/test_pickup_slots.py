"""Pickup: the slot board, the gate that books it, and the promised time.

Everything about the MONEY was already built and almost nothing about the
TIME was, so these are the properties a shop discovers the hard way once
it starts promising people a moment rather than a parcel.

A window that is full, in the past, inside the shop's lead time, or
closed is NEVER OFFERED -- "can I have a pizza at 3am" has to be answered
by the picker not showing 3am, not by a refusal after the card is typed
in. Every one of those is also refused if the id is asked for directly,
because a picker is a courtesy and a gate is a gate. A refusal names the
next free window, because "that time is full" sends somebody away and
"that time is full, the next one is 18:30" keeps the sale. The generator
is idempotent, because it runs every night forever and the second run
must create nothing. And a SHIPPING order is untouched by the whole of
it, which is the regression that matters most: an order raised by a shop
that has never heard of pickup has to be the order it was yesterday.
"""

import json
import pathlib
import shutil
from datetime import datetime, timedelta

from conftest import stage_collection

import object_execution
import object_packages
import object_records
import object_schemas
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
PICKUP_OBJECTS = PACKAGES / "app-pickup" / "objects"
SHOP_OBJECTS = PACKAGES / "app-shop" / "objects"
ORDERS_OBJECTS = PACKAGES / "app-orders" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

TOKEN = "sess-pickup-1"

# One fixed minute to stand at, so "too soon" and "already passed" are
# properties of the arithmetic rather than of how long the test took to
# run. Every slot below is placed relative to it.
NOW = datetime(2026, 7, 27, 12, 0, 0)


def at(minutes):
    """A slot start `minutes` from NOW, as the ISO string a row holds."""
    return (NOW + timedelta(minutes=minutes)).isoformat()


def setup_env(tmp_path, monkeypatch, *, settings=()):
    """A data dir with every collection this slice touches, and ONE object
    root holding all three packages' objects -- which is what an installed
    server actually looks like, since every package installs into the same
    objects directory.
    """
    data_dir = tmp_path / "data"
    for pkg, name in (("app-pickup", "pickup_slots"),
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
    for source in (PICKUP_OBJECTS, SHOP_OBJECTS, ORDERS_OBJECTS):
        shutil.copytree(source, objects_root, dirs_exist_ok=True)

    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_root))
    return data_dir, objects_root


def uninstall_pickup(data_dir):
    """A box that never installed this app: no schema and no records file.

    The schema has to go too. A collection whose schema is still declared
    reads as an empty one rather than an absent one, which is exactly the
    distinction every "not installed" branch in this package turns on.
    """
    shutil.rmtree(data_dir / "collections" / "pickup_slots")
    (data_dir / "schemas" / "pickup_slots.json").unlink()


def run(objects_root, object_id, method, payload):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            object_id, method=method, payload=payload),
        roots=[objects_root]).result


# --- fixtures shaped like a small counter shop ---------------------------------

def slot(data_dir, slot_id, *, starts_in=60, minutes=15, capacity=2,
         taken=0, is_open="true", location=""):
    return object_records.create_collection_record(
        "pickup_slots",
        {"id": slot_id,
         "starts_at": at(starts_in),
         "ends_at": at(starts_in + minutes),
         "capacity": str(capacity),
         "orders_taken": str(taken),
         "is_open": is_open,
         "location_id": location,
         "owner_id": ""},
        base_dir=data_dir)


def product(data_dir, product_id="p1", name="Flat White", cents=350):
    return object_records.create_collection_record(
        "products",
        {"id": product_id, "name": name, "sku": product_id.upper(),
         # service: nothing on a shelf to run out of, so the stock gate is
         # silent and every blocker in these tests is a pickup blocker.
         "product_type": "service", "price_cents": str(cents),
         "currency": "USD", "is_active": "true", "owner_id": "shop"},
        base_dir=data_dir)


def add_to_cart(objects_root, quantity="1", product_id="p1"):
    return run(objects_root, "action_cart", "POST",
               {"session_token": TOKEN, "action": "add",
                "product_id": product_id, "quantity": quantity})


def checkout(objects_root, **payload):
    payload.setdefault("session_token", TOKEN)
    payload.setdefault("customer_email", "buyer@example.test")
    return run(objects_root, "action_checkout", "POST", payload)


def bookable(objects_root, **payload):
    payload.setdefault("now", NOW.isoformat())
    return run(objects_root, "action_pickup_slots", "GET", payload)


def offered_ids(objects_root, **payload):
    return {row["id"] for row in bookable(objects_root, **payload)["slots"]}


def slots(data_dir):
    return object_records.read_collection_records("pickup_slots",
                                                  base_dir=data_dir)


def orders(data_dir):
    return object_records.read_collection_records("orders", base_dir=data_dir)


def one_slot(data_dir, slot_id):
    return object_records.get_collection_record("pickup_slots", slot_id,
                                                base_dir=data_dir)


# --- what a picker may offer ----------------------------------------------------

def test_a_full_slot_is_never_offered_and_is_refused_by_name(tmp_path, monkeypatch):
    """The refusal carries BOTH numbers and the next free time. "Sorry"
    sends a customer away; "sorry, 18:30 is free" keeps the sale."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    slot(data_dir, "full", starts_in=60, capacity=2, taken=2)
    slot(data_dir, "next", starts_in=120, capacity=2, taken=0)
    product(data_dir)
    add_to_cart(objects_root)

    assert offered_ids(objects_root) == {"next"}

    blocked = checkout(objects_root, fulfillment_method="pickup",
                       pickup_slot_id="full", now=NOW.isoformat())
    assert blocked["status"] == 409
    problem = " ".join(blocked["pickup_problems"])
    assert "takes 2 orders and 2 have been taken" in problem
    assert at(120) in problem
    assert blocked["next_free_slot"]["id"] == "next"
    # Refused BEFORE anything was committed: this is the whole point of
    # checking here rather than after the card.
    assert orders(data_dir) == []
    assert one_slot(data_dir, "full")["orders_taken"] == "2"


def test_a_slot_inside_the_lead_time_is_never_offered_and_is_refused(
        tmp_path, monkeypatch):
    """A shop that needs 45 minutes' notice must not show a window 20
    minutes away -- and must still refuse it when the id is typed."""
    data_dir, objects_root = setup_env(
        tmp_path, monkeypatch, settings=(("pickup.lead_minutes", "45"),))
    slot(data_dir, "too_soon", starts_in=20)
    slot(data_dir, "fine", starts_in=90)
    product(data_dir)
    add_to_cart(objects_root)

    assert offered_ids(objects_root) == {"fine"}

    blocked = checkout(objects_root, fulfillment_method="pickup",
                       pickup_slot_id="too_soon", now=NOW.isoformat())
    assert blocked["status"] == 409
    problem = " ".join(blocked["pickup_problems"])
    assert "45 minutes' notice" in problem
    assert blocked["next_free_slot"]["id"] == "fine"
    assert orders(data_dir) == []


def test_a_slot_in_the_past_is_never_offered_and_is_refused(tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(
        tmp_path, monkeypatch, settings=(("pickup.lead_minutes", "0"),))
    slot(data_dir, "gone", starts_in=-60)
    slot(data_dir, "later", starts_in=60)
    product(data_dir)
    add_to_cart(objects_root)

    assert offered_ids(objects_root) == {"later"}

    blocked = checkout(objects_root, fulfillment_method="pickup",
                       pickup_slot_id="gone", now=NOW.isoformat())
    assert blocked["status"] == 409
    assert "has already passed" in " ".join(blocked["pickup_problems"])
    assert blocked["next_free_slot"]["id"] == "later"
    assert orders(data_dir) == []


def test_a_closed_slot_is_never_offered_and_is_refused(tmp_path, monkeypatch):
    """`is_open=false` and `capacity=0` are different sentences: the shop
    is shut then, versus that window is full. Both refuse; only one of
    them is about how many orders exist."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    slot(data_dir, "shut", starts_in=60, is_open="false")
    slot(data_dir, "open", starts_in=180)
    product(data_dir)
    add_to_cart(objects_root)

    assert offered_ids(objects_root) == {"open"}

    blocked = checkout(objects_root, fulfillment_method="pickup",
                       pickup_slot_id="shut", now=NOW.isoformat())
    assert blocked["status"] == 409
    assert "not taking orders" in " ".join(blocked["pickup_problems"])
    assert blocked["next_free_slot"]["id"] == "open"
    assert orders(data_dir) == []


def test_a_slot_problem_is_reported_alongside_the_price_blockers(
        tmp_path, monkeypatch):
    """Never returned early on its own. Telling somebody about the slot,
    letting them fix it, then revealing the price change is how a
    checkout gets abandoned."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    slot(data_dir, "full", starts_in=60, capacity=1, taken=1)
    product(data_dir)
    add_to_cart(objects_root)
    object_records.update_collection_record(
        "products", "p1", {"price_cents": "500"}, base_dir=data_dir, actor="shop")

    blocked = checkout(objects_root, fulfillment_method="pickup",
                       pickup_slot_id="full", now=NOW.isoformat())
    assert blocked["status"] == 409
    # Both facts, one response.
    assert blocked["price_changes"][0]["now_cents"] == 500
    assert blocked["pickup_problems"]


# --- booking one ------------------------------------------------------------------

def test_a_pickup_checkout_stamps_the_method_and_the_promise_and_takes_a_place(
        tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    slot(data_dir, "six", starts_in=60, capacity=4)
    product(data_dir)
    add_to_cart(objects_root)

    result = checkout(objects_root, fulfillment_method="pickup",
                      pickup_slot_id="six", now=NOW.isoformat())
    assert result["ok"] is True
    assert result["fulfillment_method"] == "pickup"
    assert result["pickup_slot_id"] == "six"
    assert result["promised_at"] == at(60)

    order = orders(data_dir)[0]
    assert order["fulfillment_method"] == "pickup"
    assert order["promised_at"] == at(60)
    # requested_at defaults to the slot the shopper chose, and stays its
    # own field so a storefront that knows what they actually wanted can
    # say so instead of overwriting the promise.
    assert order["requested_at"] == at(60)
    assert order["ready_at"] == "" and order["collected_at"] == ""
    assert "[pickup_slots/six]" in order["notes"]

    assert one_slot(data_dir, "six")["orders_taken"] == "1"


def test_a_stated_requested_time_is_kept_apart_from_the_promise(
        tmp_path, monkeypatch):
    """"You wanted 6:00, we said 6:20" is the fact that makes every
    capacity failure visible; one field holding whichever was written last
    would hide all of them."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    slot(data_dir, "later", starts_in=80)
    product(data_dir)
    add_to_cart(objects_root)

    checkout(objects_root, fulfillment_method="pickup", pickup_slot_id="later",
             requested_at=at(60), now=NOW.isoformat())
    order = orders(data_dir)[0]
    assert order["requested_at"] == at(60)
    assert order["promised_at"] == at(80)


def test_two_shoppers_racing_for_the_last_place_are_told_before_they_pay(
        tmp_path, monkeypatch):
    """The race is not pretended away. What is promised is that the loser
    finds out at checkout, with the next free time named, rather than
    after handing over money."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    slot(data_dir, "last", starts_in=60, capacity=1)
    slot(data_dir, "spare", starts_in=120, capacity=1)
    product(data_dir)

    add_to_cart(objects_root)
    first = checkout(objects_root, fulfillment_method="pickup",
                     pickup_slot_id="last", now=NOW.isoformat())
    assert first["ok"] is True

    # A second shopper, a second basket, the same window.
    run(objects_root, "action_cart", "POST",
        {"session_token": "sess-pickup-2", "action": "add", "product_id": "p1"})
    second = run(objects_root, "action_checkout", "POST",
                 {"session_token": "sess-pickup-2",
                  "customer_email": "other@example.test",
                  "fulfillment_method": "pickup", "pickup_slot_id": "last",
                  "now": NOW.isoformat()})
    assert second["status"] == 409
    assert second["next_free_slot"]["id"] == "spare"
    # One order, not two, and no invoice raised for the loser.
    assert len(orders(data_dir)) == 1


# --- the regression that matters most ---------------------------------------------

def test_a_shipping_checkout_is_exactly_what_it_was(tmp_path, monkeypatch):
    """No pickup argument, no pickup read, no pickup key in the response,
    no slot touched, and no blocker added -- even with a full slot board
    sitting right there for it to trip over."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    slot(data_dir, "full", starts_in=60, capacity=1, taken=1)
    slot(data_dir, "shut", starts_in=90, is_open="false")
    product(data_dir)
    add_to_cart(objects_root)

    result = checkout(objects_root, today="2026-07-27")
    assert result["ok"] is True
    # No pickup key of any kind. The three money-layer totals are always
    # present and always zero here: discount_cents, credit_applied_cents
    # and an amount_due_cents that equals the total are what "this shop
    # runs no promotions and took no gift card" looks like, and a response
    # that omitted them would make a storefront ask whether the shop had
    # them rather than read the number.
    assert set(result) == {
        "ok", "order_id", "cart_id", "subtotal_cents", "shipping_cents",
        "shipping_free", "tax_cents", "total_cents", "lines",
        "status_of_order", "note", "track_path", "invoice_id", "pay_path",
        "discount_cents", "credit_applied_cents", "amount_due_cents",
    }
    assert result["discount_cents"] == 0
    assert result["credit_applied_cents"] == 0
    assert result["amount_due_cents"] == result["total_cents"]

    order = orders(data_dir)[0]
    # `shipping` arrives from the schema default, not from this file: the
    # order is the row it would have been before v7 existed.
    assert order["fulfillment_method"] == "shipping"
    assert order["requested_at"] == "" and order["promised_at"] == ""
    assert order["notes"] == f"Web checkout [carts/{result['cart_id']}]"
    assert [(row["id"], row["orders_taken"]) for row in slots(data_dir)] == [
        ("full", "1"), ("shut", "0")]


def test_an_unknown_fulfilment_method_is_refused_rather_than_silently_shipped(
        tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir)
    add_to_cart(objects_root)

    blocked = checkout(objects_root, fulfillment_method="teleport")
    assert blocked["status"] == 409
    assert "teleport" in blocked["error"]
    assert orders(data_dir) == []


def test_delivery_and_counter_state_a_method_and_book_no_slot(
        tmp_path, monkeypatch):
    """There is no slot board for the van or the till, and inventing one
    would be a rule nobody asked for."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir)
    add_to_cart(objects_root)

    result = checkout(objects_root, fulfillment_method="counter")
    assert result["ok"] is True
    assert result["fulfillment_method"] == "counter"
    assert "pickup_slot_id" not in result
    order = orders(data_dir)[0]
    assert order["fulfillment_method"] == "counter"
    assert order["promised_at"] == ""


# --- the generator ------------------------------------------------------------------

def generate(objects_root, **payload):
    payload.setdefault("today", "2026-07-27")
    return run(objects_root, "system_slot_generator", "POST", payload)


GENERATOR_SETTINGS = (("pickup.open_time", "09:00"),
                      ("pickup.close_time", "12:00"),
                      ("pickup.slot_minutes", "30"),
                      ("pickup.capacity_per_slot", "3"),
                      ("pickup.days_ahead", "2"))


def test_the_generator_builds_the_board_the_settings_describe(
        tmp_path, monkeypatch):
    """09:00-12:00 in half hours is six windows, and days_ahead=2 means
    today plus two -- today included, because a shop that installs this
    at eight in the morning must be able to take lunch orders today."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch,
                                       settings=GENERATOR_SETTINGS)
    result = generate(objects_root)
    assert result["created"] == 18
    assert [day["created"] for day in result["days"]] == [6, 6, 6]

    rows = sorted(slots(data_dir), key=lambda row: row["starts_at"])
    assert rows[0]["starts_at"] == "2026-07-27T09:00:00"
    assert rows[0]["ends_at"] == "2026-07-27T09:30:00"
    assert rows[0]["capacity"] == "3"
    assert rows[0]["orders_taken"] == "0"
    assert rows[0]["is_open"] == "true"
    # The last window ENDS at closing time; a half-length one would be a
    # promise made by arithmetic rather than by anybody who works there.
    assert rows[5]["starts_at"] == "2026-07-27T11:30:00"
    assert rows[5]["ends_at"] == "2026-07-27T12:00:00"


def test_the_generator_is_idempotent_across_two_runs(tmp_path, monkeypatch):
    """It runs every night forever. The second run must create nothing --
    idempotent by the row's own natural key, (starts_at, location)."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch,
                                       settings=GENERATOR_SETTINGS)
    generate(objects_root)
    before = {row["id"] for row in slots(data_dir)}

    again = generate(objects_root)
    assert again["created"] == 0
    assert again["already_there"] == 18
    assert {row["id"] for row in slots(data_dir)} == before


def test_the_generator_never_restates_a_slot_somebody_edited(
        tmp_path, monkeypatch):
    """A slot an operator closed or resized is a decision. A nightly pass
    that put it back would overwrite them every single night."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch,
                                       settings=GENERATOR_SETTINGS)
    generate(objects_root)
    nine = next(row for row in slots(data_dir)
                if row["starts_at"] == "2026-07-27T09:00:00")
    object_records.update_collection_record(
        "pickup_slots", nine["id"], {"is_open": "false", "capacity": "1"},
        base_dir=data_dir, actor="operator")

    generate(objects_root)
    kept = object_records.get_collection_record("pickup_slots", nine["id"],
                                                base_dir=data_dir)
    assert kept["is_open"] == "false" and kept["capacity"] == "1"
    assert len(slots(data_dir)) == 18


def test_the_generator_says_so_on_a_box_without_the_pickup_app(
        tmp_path, monkeypatch):
    """Not an exception every night on a shop that never wanted pickup."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    uninstall_pickup(data_dir)
    result = generate(objects_root)
    assert result["ok"] is True and "pickup not installed" in result["skipped"]


# --- the hook -----------------------------------------------------------------------

def hook(objects_root, record, action="create"):
    return run(objects_root, "hook_pickup_slots", "BEFORE_WRITE",
               {"collection": "pickup_slots", "action": action,
                "record": record, "existing": None, "changes": record})


def test_the_hook_refuses_more_orders_than_the_slot_can_hold_with_the_numbers(
        tmp_path, monkeypatch):
    """A gate that only says "no" leaves whoever is fixing the slot
    guessing which of the two figures is the wrong one."""
    _data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    refused = hook(objects_root, {"id": "s1", "starts_at": at(60),
                                  "capacity": "2", "orders_taken": "5"},
                   action="update")
    assert refused["status"] == 409
    assert "capacity 2" in refused["error"]
    assert "orders taken 5" in refused["error"]


def test_the_hook_refuses_a_negative_capacity_and_allows_zero(
        tmp_path, monkeypatch):
    """Zero is a real statement -- this window exists and is full -- and
    is a different one from is_open=false."""
    _data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    refused = hook(objects_root, {"id": "s1", "starts_at": at(60),
                                  "capacity": "-1", "orders_taken": "0"})
    assert refused["status"] == 400
    assert "-1" in refused["error"]

    assert hook(objects_root, {"id": "s2", "starts_at": at(60),
                               "capacity": "0", "orders_taken": "0"}) is None


# --- the band and the customer's page -------------------------------------------------

# Every rung between draft and the status a test wants, because orders'
# transitions are guarded and there is no shortcut through them.
LADDER = {
    "draft": ["draft"],
    "confirmed": ["confirmed"],
    "preparing": ["confirmed", "preparing"],
    "ready": ["confirmed", "preparing", "ready"],
    "collected": ["confirmed", "preparing", "ready", "collected"],
    "shipped": ["confirmed", "shipped"],
    "delivered": ["confirmed", "shipped", "delivered"],
}


def order_row(data_dir, order_id, *, status="confirmed", method="pickup",
              promised=None, token=""):
    record = {"id": order_id, "doc_type": "sale", "number": order_id.upper(),
              "customer_name": "Ada Lovelace",
              "customer_email": "ada@example.test", "currency": "USD",
              "status": "draft", "order_date": "2026-07-27",
              "fulfillment_method": method, "owner_id": "shop"}
    if promised is not None:
        record["promised_at"] = promised
    if token:
        record["portal_token"] = token
    object_records.create_collection_record("orders", record, base_dir=data_dir,
                                            actor="test", preserve_read_only=True)
    # Up the ladder a rung at a time, the way a human would: the schema's
    # own transitions are what make `collected` reachable only through
    # `ready`, and a test that jumped straight there would be exercising a
    # move the server refuses.
    for rung in LADDER.get(status, [status]):
        if rung == "draft":
            continue
        object_records.update_collection_record(
            "orders", order_id, {"status": rung}, base_dir=data_dir, actor="test")
    return object_records.get_collection_record("orders", order_id,
                                                base_dir=data_dir)


def test_an_order_past_its_promised_time_and_not_ready_needs_a_human(
        tmp_path, monkeypatch):
    """Late is a fact about a time that passed, so the count keys on
    promised_at -- which is why a shipping order can never appear in it."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    past = (datetime.now() - timedelta(minutes=25)).isoformat()
    future = (datetime.now() + timedelta(minutes=25)).isoformat()

    order_row(data_dir, "late", promised=past)
    order_row(data_dir, "soon", promised=future)
    order_row(data_dir, "done", status="ready", promised=past)
    order_row(data_dir, "parcel", method="shipping")

    count = run(objects_root, "system_pickup_attention", "COUNT", {})
    assert count["count"] == 1
    assert "past its promised time" in count["detail"]


def test_the_band_is_silent_when_nothing_is_late(tmp_path, monkeypatch):
    """A count that reads zero is rendered nowhere; a band that is always
    lit is a band nobody reads."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    order_row(data_dir, "soon",
              promised=(datetime.now() + timedelta(hours=2)).isoformat())
    assert run(objects_root, "system_pickup_attention", "COUNT", {}) == {"count": 0}


def track(objects_root, token):
    return run(objects_root, "site_order_status", "GET", {"token": token})["body"]


def test_the_customer_reads_ready_for_collection_on_a_pickup_order(
        tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    order_row(data_dir, "pick", status="ready", token="tok-pickup",
              promised="2026-07-27T18:00:00")

    page = track(objects_root, "tok-pickup")
    assert "Ready for collection" in page
    assert "Ready by" in page
    # Never the warehouse's word for it.
    assert "On its way" not in page
    assert "shipped" not in page


def test_a_shipping_order_still_reads_on_its_way(tmp_path, monkeypatch):
    """The regression beside the feature: adding the pickup vocabulary
    must not change one word a parcel customer sees."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    order_row(data_dir, "parcel", status="shipped", method="shipping",
              token="tok-shipping")

    page = track(objects_root, "tok-shipping")
    assert "On its way" in page
    assert "Your order has left us and is with the carrier." in page
    assert "Ready for collection" not in page
    assert "Ready by" not in page


def test_the_pickup_ladder_reads_in_the_customers_words_at_every_rung(
        tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    expected = {"confirmed": "Order received", "preparing": "Being prepared",
                "ready": "Ready for collection", "collected": "Collected"}
    for index, (status, word) in enumerate(expected.items()):
        order_row(data_dir, f"p{index}", status=status, token=f"tok-{index}")
        assert word in track(objects_root, f"tok-{index}"), status


# --- the package itself -----------------------------------------------------------------

def test_the_package_installs_on_a_bare_box(tmp_path):
    """Structural, and worth its two lines: a package whose dry run is not
    clean is a package nobody can ship, however well its objects behave
    under a test harness that copies them into place by hand."""
    object_root = tmp_path / "objects"
    object_root.mkdir()
    plan = object_packages.dry_run_package(
        "app-pickup", root=PACKAGES, base_dir=tmp_path / "data",
        object_roots=[object_root])
    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []

    object_packages.install_package(
        "app-pickup", root=PACKAGES, base_dir=tmp_path / "data",
        object_roots=[object_root])
    schema = object_schemas.get_schema("pickup_slots",
                                       base_dir=tmp_path / "data")
    assert schema["name"] == "pickup_slots"
    # The gate is declared on the collection, not merely shipped beside it:
    # a hook nothing points at is a hook that never runs.
    assert schema["hooks"]["before_write"] == "hook_pickup_slots"


def test_capacity_is_counted_in_orders_and_the_schema_says_why():
    """The load-bearing decision in this package, asserted so a later
    refactor to "items" has to argue with a test rather than a comment."""
    schema = json.loads(
        (PACKAGES / "app-pickup" / "schemas" / "pickup_slots.json").read_text())
    by_name = {field["name"]: field for field in schema["fields"]}
    assert by_name["capacity"]["type"] == "integer"
    assert "orders" in by_name["capacity"]["label"].lower()
    assert "ORDERS, NOT ITEMS" in schema["description"].upper()
    # orders_taken is maintained by the checkout gate, so it is kept out of
    # the default form for the same reason the stamped totals are on
    # orders: a number a person types over is a number that stops being a
    # record of what happened.
    assert "orders_taken" not in schema["forms"]["default"]["fields"]


# --- the object ids a fresh box must be able to find ------------------------------------

def test_an_empty_box_offers_no_slots_rather_than_an_error_page(
        tmp_path, monkeypatch):
    """A storefront on a box without the pickup app gets an empty picker,
    not a traceback where its time buttons should be."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    uninstall_pickup(data_dir)
    answer = bookable(objects_root)
    assert answer["ok"] is True and answer["slots"] == []
