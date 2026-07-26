"""Counter service: an instruction on a line, a queue with a clock in it,
and the message that stops somebody standing there guessing.

Three slices meet in this file and the properties worth holding are the
seams between them.

**The delta is money, so it is money everywhere.** A modifier that shows
up in the basket total and not on the invoice is the shop quietly eating
sixty cents a cup, and nothing in the system would ever say so -- every
document would be internally consistent and one of them would be wrong.
So the modifier test walks the whole chain in one go, basket to invoice,
rather than asserting four separate arithmetics that could each be right
about a different number.

**The note is NOT money, so it is money nowhere.** It reaches the cook,
who needs it, and touches no total, which is the other half of the same
property: the day somebody "helpfully" prices a note is the day an
invoice stops matching its own lines.

**A ticket is not an invoice.** No prices, ever, by construction and not
by a flag -- the same rule the packing slip holds, tested the same way.

**A shipping order is untouched by every pickup rule.** That is the
regression that matters: a warehouse that started printing kitchen
tickets for parcels would be this slice leaking into the one that was
already working.

**The ready message queues exactly once.** The dispatcher promises
at-least-once delivery, so the handler WILL see a ready order again; a
customer told twice is noise, and a customer told never is the whole
failure this message exists to prevent.
"""

import pathlib
import shutil
from datetime import datetime, timedelta

from conftest import stage_collection

import object_execution
import object_ids
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
SHOP_OBJECTS = PACKAGES / "app-shop" / "objects"
ORDERS_OBJECTS = PACKAGES / "app-orders" / "objects"
KITCHEN_OBJECTS = PACKAGES / "app-kitchen" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

TOKEN = "sess-pickup-1"


def setup_env(tmp_path, monkeypatch, *, settings=()):
    """A data dir with every collection this slice touches, and ONE object
    root holding three packages' objects -- what an installed server
    actually looks like, and the only way site_shop reaching action_cart
    or action_checkout reaching object_cart resolves as it will in
    production.
    """
    data_dir = tmp_path / "data"
    for pkg, name in (("app-shop", "carts"), ("app-shop", "cart_items"),
                      ("app-catalog", "products"), ("app-catalog", "locations"),
                      ("app-catalog", "stock_moves"),
                      ("app-orders", "orders"), ("app-orders", "order_lines"),
                      ("app-invoices", "invoices"),
                      ("app-invoices", "invoice_lines"),
                      ("app-email", "email_outbox")):
        stage_collection(data_dir, pkg, name)
    rows = "".join(f"s{i}\t{k}\t{v}\t\n" for i, (k, v) in enumerate(settings))
    stage_collection(data_dir, "app-settings", "app_settings", rows=rows)

    objects_root = tmp_path / "objects"
    for source in (SHOP_OBJECTS, ORDERS_OBJECTS, KITCHEN_OBJECTS):
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


# --- fixtures shaped like a small café ---------------------------------------

def product(data_dir, product_id, name, cents=1200):
    return object_records.create_collection_record(
        "products",
        {"id": product_id, "name": name, "sku": product_id.upper(),
         "product_type": "physical", "price_cents": str(cents),
         "currency": "USD", "is_active": "true", "owner_id": "shop"},
        base_dir=data_dir)


def stock_in(data_dir, product_id, quantity, *, to="loc-shelf"):
    object_records.create_collection_record(
        "locations",
        {"id": to, "name": "Counter", "location_type": "warehouse",
         "owner_id": "shop"}, base_dir=data_dir)
    return object_records.create_collection_record(
        "stock_moves",
        {"id": object_ids.new_uuid4(), "product_id": product_id,
         "to_location_id": to, "quantity": str(quantity), "reason": "purchase",
         "occurred_at": "2026-07-01", "owner_id": "shop"},
        base_dir=data_dir)


def cart(objects_root, action="get", **payload):
    return run(objects_root, "action_cart", "POST",
               {"session_token": TOKEN, "action": action, **payload})


def checkout(objects_root, **payload):
    return run(objects_root, "action_checkout", "POST",
               {"session_token": TOKEN, **payload})


def when(minutes_from_now):
    """A promised time as the shop's own naive wall clock -- the
    convention app-pickup's slot generator writes and every reader of
    promised_at in this repo shares.

    Half a minute of cushion, away from now in whichever direction the
    caller asked. The page reports WHOLE minutes elapsed, so a promise
    written exactly 35 minutes out and read a few milliseconds later is
    honestly 34; the cushion buys the assertion a stable number without
    the page having to round up and claim a minute that is not there.
    """
    cushion = timedelta(seconds=30 if minutes_from_now >= 0 else -30)
    return (datetime.now() + timedelta(minutes=minutes_from_now)
            + cushion).isoformat(timespec="seconds")


def pickup_order(data_dir, order_id, *, status="preparing", number="PU-1",
                 promised=None, method="pickup", owner="", email="",
                 name="Ada Lovelace", **fields):
    record = {"id": order_id, "doc_type": "sale", "number": number,
              "customer_name": name, "customer_email": email,
              "currency": "USD", "status": status, "order_date": "2026-07-26",
              "fulfillment_method": method, "owner_id": owner}
    if promised is not None:
        record["promised_at"] = promised
    record.update({k: str(v) for k, v in fields.items()})
    return object_records.create_collection_record(
        "orders", record, base_dir=data_dir, preserve_read_only=True)


def order_line(data_dir, line_id, *, order_id, description="Flat White",
               quantity="1", cents=400, note="", modifier=0):
    return object_records.create_collection_record(
        "order_lines",
        {"id": line_id, "order_id": order_id, "description": description,
         "quantity": quantity, "unit_price_cents": str(cents),
         "line_note": note, "modifier_cents": str(modifier),
         "line_total_cents": str(int(float(quantity)) * (cents + modifier)),
         "owner_id": "shop"},
        base_dir=data_dir)


def kitchen(objects_root, user_id="dan"):
    payload = {"_identity": {"user_id": user_id}} if user_id else {}
    return run(objects_root, "site_kitchen", "GET", payload)["body"]


def ticket(objects_root, order_id):
    return run(objects_root, "site_kitchen_ticket", "GET",
               {"order_id": order_id})


def fire(objects_root, order_id, action="update"):
    """One change event, shaped exactly as object_change_dispatch sends
    them (raw verb in `action`, participle in `event`)."""
    return run(objects_root, "system_order_email", "EVENT",
               {"event": f"orders.record.{action}d", "collection": "orders",
                "record_id": order_id, "action": action})


def rows(data_dir, collection):
    return object_records.read_collection_records(collection,
                                                  base_dir=data_dir)


def outbox(data_dir):
    return rows(data_dir, "email_outbox")


# --- 1. the delta is money, and money is the same number everywhere ----------

def test_a_modifier_reaches_the_basket_the_order_and_the_invoice(
        tmp_path, monkeypatch):
    """One test on purpose, walking the whole chain.

    Four separate assertions about four totals can all pass while the
    totals disagree with each other -- which is precisely the failure
    mode: every document internally consistent, one of them quietly
    short. So the chain is walked once, and the last assertion is that
    the invoice's own lines add up to the invoice.
    """
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "latte", "Latte", 400)
    stock_in(data_dir, "latte", 20)

    # Two lattes with oat milk: 2 x (400 + 60), because two oat lattes are
    # two lots of oat milk.
    basket = cart(objects_root, "add", product_id="latte", quantity="2",
                  line_note="oat milk", modifier_cents="60")
    assert basket["subtotal_cents"] == 920
    assert basket["lines"][0]["modifier_cents"] == 60
    assert basket["lines"][0]["line_note"] == "oat milk"

    preview = checkout(objects_root, preview="true")
    assert preview["total_cents"] == 920

    result = checkout(objects_root, customer_email="ada@example.test",
                      customer_name="Ada Lovelace")
    assert result["ok"] is True
    assert result["total_cents"] == 920

    order = object_records.get_collection_record("orders", result["order_id"],
                                                 base_dir=data_dir)
    assert order["total_cents"] == "920"

    line = rows(data_dir, "order_lines")[0]
    assert line["modifier_cents"] == "60"
    assert line["unit_price_cents"] == "400"      # the catalogue price, kept
    assert line["line_total_cents"] == "920"      # with the delta inside it

    invoice = rows(data_dir, "invoices")[0]
    invoice_line = rows(data_dir, "invoice_lines")[0]
    # The invoice carries the EFFECTIVE unit price, so the bill is still an
    # ordinary quantity x price document that multiplies out -- and so a
    # restatement by invoice_totals could never drop the delta.
    assert invoice_line["unit_price_cents"] == "460"
    assert invoice_line["line_total_cents"] == "920"
    assert invoice["total_cents"] == "920"
    assert (sum(int(row["line_total_cents"]) for row in rows(data_dir, "invoice_lines"))
            == int(invoice["total_cents"]))


def test_order_totals_restating_a_line_keeps_the_modifier(tmp_path, monkeypatch):
    """The fold has to be able to reproduce the number it is restating.

    order_totals recomputes line_total_cents from the line's own columns.
    If it multiplied the base price alone it would silently replace a
    correct total with a smaller one every time anybody touched the line,
    and the shop would be short the delta with nothing to point at.
    """
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    pickup_order(data_dir, "ord-mod", number="PU-MOD")
    order_line(data_dir, "line-mod", order_id="ord-mod", quantity="2",
               cents=400, modifier=60)

    run(objects_root, "system_order_totals", "EVENT",
        {"event": "order_lines.record.updated", "collection": "order_lines",
         "record_id": "line-mod", "action": "update"})

    line = object_records.get_collection_record("order_lines", "line-mod",
                                                base_dir=data_dir)
    assert line["line_total_cents"] == "920"
    order = object_records.get_collection_record("orders", "ord-mod",
                                                 base_dir=data_dir)
    assert order["subtotal_cents"] == "920"


def test_two_notes_on_one_product_are_two_lines(tmp_path, monkeypatch):
    """One burger with no onions and one with are two things to make.

    Merging them on product_id would hand the cook a line of two carrying
    one instruction, and one of those two customers gets the wrong lunch.
    """
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "burger", "Burger", 900)
    stock_in(data_dir, "burger", 20)

    cart(objects_root, "add", product_id="burger", quantity="1",
         line_note="no onions")
    basket = cart(objects_root, "add", product_id="burger", quantity="1")
    assert len(basket["lines"]) == 2
    assert {line["line_note"] for line in basket["lines"]} == {"no onions", ""}

    # And the ordinary case is untouched: same product, same instruction,
    # same delta still merges into one line of two.
    basket = cart(objects_root, "add", product_id="burger", quantity="1",
                  line_note="no onions")
    assert len(basket["lines"]) == 2
    noted = [line for line in basket["lines"] if line["line_note"]][0]
    assert noted["quantity"] == "2"


def test_a_shopper_cannot_price_their_own_lunch(tmp_path, monkeypatch):
    """action_cart is a PUBLIC object, so anything the request can put
    into money is something a stranger can put into money."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "latte", "Latte", 400)
    stock_in(data_dir, "latte", 20)

    result = cart(objects_root, "add", product_id="latte",
                  modifier_cents="-350")
    assert result["status"] == 400
    assert "never take money off" in result["error"]
    assert rows(data_dir, "cart_items") == []


def test_the_shop_page_takes_a_note_and_shows_it_back(tmp_path, monkeypatch):
    """The box is on the ADD form, because a note belongs to a line.
    /shop's own `customer_note` is a different field addressed to a
    different person -- the packer, not the cook -- and one box at the
    bottom of a checkout cannot say which burger it meant.
    """
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "burger", "Burger", 900)
    stock_in(data_dir, "burger", 20)

    body = run(objects_root, "site_shop", "GET",
               {"_cookies": {"cart": TOKEN}})["body"]
    assert 'name="line_note"' in body
    # And no way to type a price: action_cart is public, so a delta input
    # on a public page is a shopper pricing their own lunch.
    assert 'name="modifier_cents"' not in body

    cart(objects_root, "add", product_id="burger", quantity="1",
         line_note="no onions", modifier_cents="150")
    body = run(objects_root, "site_shop", "GET",
               {"_cookies": {"cart": TOKEN}})["body"]
    assert "no onions" in body
    assert "+1.50 each" in body
    # The Update button says WHICH line, now that two can share a product.
    assert 'name="cart_item_id"' in body


# --- 2. the note is not money, and reaches the person who needs it ----------

def test_a_line_note_reaches_the_kitchen_ticket_and_never_the_invoice_total(
        tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "burger", "Burger", 900)
    stock_in(data_dir, "burger", 20)

    cart(objects_root, "add", product_id="burger", quantity="2",
         line_note="no onions, cut in half")
    result = checkout(objects_root, customer_email="ada@example.test")
    order_id = result["order_id"]

    # An instruction with no delta must move NO total: 2 x 900, exactly
    # what the same basket cost before anybody typed anything.
    assert result["total_cents"] == 1800
    invoice = rows(data_dir, "invoices")[0]
    assert invoice["total_cents"] == "1800"
    invoice_line = rows(data_dir, "invoice_lines")[0]
    assert invoice_line["line_total_cents"] == "1800"
    assert "onions" not in invoice_line["description"]

    # And it does reach the cook, under the line it is true of.
    body = ticket(objects_root, order_id)["body"]
    assert "no onions, cut in half" in body
    assert "Burger" in body


def test_the_kitchen_ticket_shows_no_prices(tmp_path, monkeypatch):
    """By construction, not behind a flag -- the packing slip's rule. A
    cook cannot act on what it cost, and the money conversation already
    happened with somebody who may not be the person cooking."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    pickup_order(data_dir, "ord-t", number="PU-TICKET", promised=when(20))
    order_line(data_dir, "line-t", order_id="ord-t", quantity="1",
               cents=1234, modifier=567, note="extra hot")

    body = ticket(objects_root, "ord-t")["body"]
    assert "extra hot" in body
    assert "Flat White" in body
    for money in ("1234", "12.34", "567", "5.67", "1801", "18.01"):
        assert money not in body, f"a price ({money}) reached a kitchen ticket"


def test_an_unknown_order_is_a_friendly_404_not_a_traceback(tmp_path, monkeypatch):
    _data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    page = ticket(objects_root, "no-such-order")
    assert page["status"] == 404
    assert "Not found" in page["body"]


# --- 3. the queue, and the clock in it ---------------------------------------

def test_the_queue_sorts_by_promised_time_and_shouts_about_a_late_one(
        tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    pickup_order(data_dir, "ord-late", number="PU-LATE", promised=when(-7),
                 name="Grace Hopper")
    pickup_order(data_dir, "ord-soon", number="PU-SOON", promised=when(35),
                 name="Ada Lovelace", owner="dan")
    order_line(data_dir, "line-late", order_id="ord-late",
               description="Bacon Roll", note="brown sauce")
    order_line(data_dir, "line-soon", order_id="ord-soon")

    body = kitchen(objects_root)
    assert "LATE by 7 min" in body
    assert "in 35 min" in body
    # Soonest promise first, and the late one is the soonest of all.
    assert body.index("PU-LATE") < body.index("PU-SOON")
    # The lines and their notes are on the queue, not only on the ticket:
    # a cook glancing at the screen is reading the instruction there.
    assert "brown sauce" in body


def test_a_shipping_order_never_appears_on_the_kitchen_queue(
        tmp_path, monkeypatch):
    """The regression that matters. A warehouse that started printing
    kitchen tickets for parcels would be this slice leaking into the one
    that was already working."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    pickup_order(data_dir, "ord-ship", number="PU-SHIPPED", status="confirmed",
                 method="shipping", promised=when(5))
    pickup_order(data_dir, "ord-blank", number="PU-NOMETHOD",
                 status="confirmed", method="", promised=when(5))
    order_line(data_dir, "line-ship", order_id="ord-ship")
    order_line(data_dir, "line-blank", order_id="ord-blank")

    body = kitchen(objects_root)
    assert "PU-SHIPPED" not in body
    assert "PU-NOMETHOD" not in body
    assert "Nothing is being prepared" in body


def test_a_ready_order_has_left_the_queue(tmp_path, monkeypatch):
    """`ready` is made and on the shelf, and the customer has been told.
    A queue that still showed it would never empty."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    pickup_order(data_dir, "ord-ready", number="PU-DONE", status="ready",
                 promised=when(-2))
    order_line(data_dir, "line-ready", order_id="ord-ready")
    assert "PU-DONE" not in kitchen(objects_root)


def test_an_order_with_no_promised_time_sorts_last_and_claims_nothing(
        tmp_path, monkeypatch):
    """'We do not know when this was promised' is not a claim to urgency,
    and must never invent an emergency at the top of the queue."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    pickup_order(data_dir, "ord-none", number="PU-UNSET")
    pickup_order(data_dir, "ord-soon", number="PU-SOON", promised=when(30))
    order_line(data_dir, "line-none", order_id="ord-none")
    order_line(data_dir, "line-soon", order_id="ord-soon")

    body = kitchen(objects_root)
    assert body.index("PU-SOON") < body.index("PU-UNSET")
    assert "no promised time" in body
    assert "LATE" not in body


def test_the_queue_asks_a_stranger_to_sign_in(tmp_path, monkeypatch):
    """A sign-in prompt, not a 403 -- the same shape site_pick_list uses,
    because the person reading this is staff who have not logged in yet."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    pickup_order(data_dir, "ord-1", number="PU-SECRET", promised=when(10))
    body = kitchen(objects_root, user_id="")
    assert "Sign in" in body
    assert "PU-SECRET" not in body


# --- 4. "your order is ready" -------------------------------------------------

def test_the_ready_message_queues_exactly_once_and_a_replay_queues_nothing(
        tmp_path, monkeypatch):
    """The dispatcher promises at-least-once delivery, so this handler
    WILL see the same ready order again."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    pickup_order(data_dir, "ord-r", number="PU-READY", status="ready",
                 email="ada@example.test", promised=when(-1),
                 ready_at="2026-07-26T18:15:00")

    first = fire(objects_root, "ord-r")
    # `confirmed` rides along on purpose: the confirmation fires anywhere
    # at or past confirmed, and `ready` is past it. A counter shop that
    # takes an order and has it made in one burst of writes would
    # otherwise never send a confirmation at all.
    assert first["queued"] == ["confirmed", "ready"]

    ready_mail = [row for row in outbox(data_dir)
                  if row["source_object_id"].endswith(":ready")]
    assert len(ready_mail) == 1
    assert "ready to collect" in ready_mail[0]["subject"]
    assert "PU-READY" in ready_mail[0]["text_body"]
    assert "ready to collect" in ready_mail[0]["text_body"]

    second = fire(objects_root, "ord-r")
    assert second["queued"] == []
    assert "ready" in second["skipped_already_sent"]
    assert len([row for row in outbox(data_dir)
                if row["source_object_id"].endswith(":ready")]) == 1


def test_a_shipping_order_is_never_told_to_come_and_collect(
        tmp_path, monkeypatch):
    """'Ready for collection' said to somebody whose parcel is on a van is
    a customer driving to a shop for nothing."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    pickup_order(data_dir, "ord-s", number="SO-SHIP", status="shipped",
                 method="shipping", email="ada@example.test")

    fire(objects_root, "ord-s")
    assert [row for row in outbox(data_dir)
            if row["source_object_id"].endswith(":ready")] == []


def test_a_pickup_order_with_no_email_skips_with_a_reason(tmp_path, monkeypatch):
    """Say so plainly. An operator reading 'no customer_email' knows
    exactly what to add, whereas silence looks like a working mailer."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    pickup_order(data_dir, "ord-n", number="PU-NOMAIL", status="ready",
                 email="")

    result = fire(objects_root, "ord-n")
    assert "customer_email" in result["skipped"]
    assert outbox(data_dir) == []


def test_with_no_sms_provider_the_customer_is_still_told_by_email(
        tmp_path, monkeypatch):
    """The absence of a connector degrades exactly the way the carrier
    work degrades: the shop loses nothing, the message still goes, and
    nothing pretends to have sent a text."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    pickup_order(data_dir, "ord-r", number="PU-READY", status="ready",
                 email="ada@example.test")

    result = fire(objects_root, "ord-r")
    assert "ready" in result["queued"]
    assert "no SMS provider configured" in result["sms"]
    assert len(outbox(data_dir)) == 2       # the confirmation, and this


def test_a_named_sms_provider_with_no_adapter_says_so_and_still_emails(
        tmp_path, monkeypatch):
    """An operator who pasted a provider name in and heard nothing would
    reasonably conclude the system was texting people. It is not, and it
    says which channel actually carried the message."""
    data_dir, objects_root = setup_env(
        tmp_path, monkeypatch,
        settings=(("notify.sms_provider", "twilio"),))
    pickup_order(data_dir, "ord-r", number="PU-READY", status="ready",
                 email="ada@example.test")

    result = fire(objects_root, "ord-r")
    assert "ready" in result["queued"]
    assert "twilio" in result["sms"]
    assert "no SMS connector is installed" in result["sms"]
    assert len(outbox(data_dir)) == 2


def test_a_manual_sms_shop_is_told_the_counter_does_it_by_hand(
        tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(
        tmp_path, monkeypatch,
        settings=(("notify.sms_provider", "manual"),))
    pickup_order(data_dir, "ord-r", number="PU-READY", status="ready",
                 email="ada@example.test")

    result = fire(objects_root, "ord-r")
    assert "ready" in result["queued"]
    assert "by hand" in result["sms"]
