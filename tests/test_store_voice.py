"""Store voice: the shop finally answers the customer.

Before this slice a shopper paid and heard nothing, then had nowhere to
look. Both halves of that silence were the same missing thing -- a
capability token on the ORDER -- and the properties worth holding here
are the ones that make such a token safe to hand to a stranger and the
ones that make an automatic email safe to send.

On the token: it is minted where the shopper can actually be handed it
(the checkout response) AND durably by a reaction, because a link that
only exists on the happy path is a link the awkward cases never get. It
is never rewritten once it exists, because by then it is in somebody's
inbox. It resolves BY TOKEN ONLY, so a guessed order id opens nothing,
and every failure -- blank, wrong, unknown -- renders one identical
friendly 404 rather than an oracle telling somebody they were close.

On the page: it speaks the customer's language. The orders enum is
warehouse vocabulary, and `partial` on a customer's screen reads as "part
of my money went missing". The tests below assert both directions -- that
the human words appear AND that the internal ones do not -- because a
mapping that is merely present is not the same as a mapping that is used.

On the email: it is queued, never sent, and queued exactly once. The
change dispatcher promises at-least-once delivery, so the handler WILL
see the same order twice; a customer receiving two confirmations is a
support ticket, and receiving two refund notices is a phone call about
money. The replay tests are therefore the point of this file, not a
footnote to it.
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
ORDERS_OBJECTS = PACKAGES / "app-orders" / "objects"
SHOP_OBJECTS = PACKAGES / "app-shop" / "objects"
RETURNS_OBJECTS = PACKAGES / "app-returns" / "objects"
SHIPPING_OBJECTS = PACKAGES / "app-shipping" / "objects"
PAYMENTS_OBJECTS = PACKAGES / "app-payments" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

TRACK_PREFIX = "/orders/track/"


def setup_env(tmp_path, monkeypatch, *, settings=()):
    """A data dir with every collection this slice touches, and ONE object
    root holding every package's objects.

    The merged root is what an installed server actually looks like, and
    it is the only way objects that call a sibling by id resolve the way
    they will in production -- site_return_form hands everything to
    action_authorize_return, and the store-voice objects read collections
    owned by four other packages.
    """
    data_dir = tmp_path / "data"
    for pkg, name in (("app-orders", "orders"), ("app-orders", "order_lines"),
                      ("app-shop", "carts"), ("app-shop", "cart_items"),
                      ("app-catalog", "products"), ("app-catalog", "locations"),
                      ("app-catalog", "stock_moves"),
                      ("app-invoices", "invoices"),
                      ("app-invoices", "invoice_lines"),
                      ("app-shipping", "shipments"),
                      ("app-shipping", "shipment_lines"),
                      ("app-payments", "payments"), ("app-payments", "refunds"),
                      ("app-returns", "return_authorizations"),
                      ("app-email", "email_outbox")):
        stage_collection(data_dir, pkg, name)
    rows = "".join(f"s{i}\t{k}\t{v}\t\n" for i, (k, v) in enumerate(settings))
    stage_collection(data_dir, "app-settings", "app_settings", rows=rows)

    objects_root = tmp_path / "objects"
    for source in (ORDERS_OBJECTS, SHOP_OBJECTS, RETURNS_OBJECTS,
                   SHIPPING_OBJECTS, PAYMENTS_OBJECTS):
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


# --- fixtures shaped like a real shop ----------------------------------------

def product(data_dir, product_id, name, cents=1200):
    return object_records.create_collection_record(
        "products",
        {"id": product_id, "name": name, "sku": product_id.upper(),
         "product_type": "physical", "price_cents": str(cents),
         "currency": "USD", "is_active": "true", "owner_id": "shop"},
        base_dir=data_dir)


def location(data_dir, location_id="loc-shelf", name="Shelf",
             kind="warehouse"):
    return object_records.create_collection_record(
        "locations",
        {"id": location_id, "name": name, "location_type": kind,
         "owner_id": "shop"},
        base_dir=data_dir)


def stock_in(data_dir, product_id, quantity, *, to="loc-shelf"):
    """Goods on the shelf. Checkout refuses to sell what is not there, so
    a store-voice test that never stocks anything is testing the oversell
    gate by accident."""
    return object_records.create_collection_record(
        "stock_moves",
        {"id": object_ids.new_uuid4(), "product_id": product_id,
         "to_location_id": to, "quantity": str(quantity), "reason": "purchase",
         "occurred_at": "2026-06-01", "owner_id": "shop"},
        base_dir=data_dir)


def order(data_dir, order_id="ord-1", *, status="confirmed", number="SO-0001",
          email="ada@example.test", **fields):
    record = {"id": order_id, "doc_type": "sale", "number": number,
              "customer_name": "Ada Lovelace", "customer_email": email,
              "currency": "USD", "status": status, "order_date": "2026-07-01",
              "subtotal_cents": "2400", "total_cents": "2400",
              "owner_id": "shop"}
    record.update({k: str(v) for k, v in fields.items()})
    # preserve_read_only because portal_token is schema read_only -- the
    # same escape hatch every server-side writer of it uses.
    return object_records.create_collection_record(
        "orders", record, base_dir=data_dir, preserve_read_only=True)


def order_line(data_dir, line_id="line-1", *, order_id="ord-1",
               description="Enamel Mug", quantity="2", cents=1200):
    return object_records.create_collection_record(
        "order_lines",
        {"id": line_id, "order_id": order_id,
         "description": description, "quantity": quantity,
         "unit_price_cents": str(cents),
         "line_total_cents": str(int(cents) * int(float(quantity))),
         "owner_id": "shop"},
        base_dir=data_dir)


def invoice(data_dir, invoice_id="inv-1", *, order_id="ord-1", status="sent",
            token="invoice-token-xyz"):
    return object_records.create_collection_record(
        "invoices",
        {"id": invoice_id, "number": "SO-0001", "customer_name": "Ada Lovelace",
         "customer_email": "ada@example.test", "currency": "USD",
         "status": status, "issue_date": "2026-07-01", "due_date": "2026-07-15",
         "total_cents": "2400",
         "source_order_id": order_id, "portal_token": token,
         "owner_id": "shop"},
        base_dir=data_dir, preserve_read_only=True)


def shipment(data_dir, shipment_id="shp-1", *, order_id="ord-1",
             status="shipped", carrier="Royal Mail",
             tracking="RM123456789GB"):
    return object_records.create_collection_record(
        "shipments",
        {"id": shipment_id, "order_id": order_id, "direction": "outbound",
         "status": status, "carrier": carrier, "tracking_number": tracking,
         "shipped_on": "2026-07-03", "owner_id": "shop"},
        base_dir=data_dir)


def payment(data_dir, payment_id="pay-1", *, invoice_id="inv-1", cents=2400):
    """The money that arrived. Only here because refunds.payment_id is a
    validated relation -- a refund with no payment behind it is not a
    thing this system will let you write, and rightly."""
    return object_records.create_collection_record(
        "payments",
        {"id": payment_id, "invoice_id": invoice_id, "amount_cents": str(cents),
         "method": "card", "received_on": "2026-07-02", "status": "received",
         "owner_id": "shop"},
        base_dir=data_dir)


def refund(data_dir, refund_id="ref-1", *, invoice_id="inv-1", cents=1200,
           reason="Arrived damaged"):
    """Written straight into the collection rather than through
    hook_refunds: this file is about what the shop SAYS when a refund
    exists, and app-payments' own tests already own the question of when
    one may. The hook's stamp (invoice_id from the payment) is supplied
    directly for the same reason."""
    return object_records.create_collection_record(
        "refunds",
        {"id": refund_id, "payment_id": "pay-1", "invoice_id": invoice_id,
         "amount_cents": str(cents), "reason": reason,
         "refunded_on": "2026-07-05", "owner_id": "shop"},
        base_dir=data_dir)


def fire(objects_root, object_id, collection, record_id, action="update"):
    """One change event, shaped exactly as object_change_dispatch sends
    them (raw verb in `action`, participle in `event`)."""
    return run(objects_root, object_id, "EVENT",
               {"event": f"{collection}.record.{action}d",
                "collection": collection, "record_id": record_id,
                "action": action})


def orders_row(data_dir, order_id="ord-1"):
    return object_records.get_collection_record("orders", order_id,
                                                base_dir=data_dir)


def outbox(data_dir):
    return object_records.read_collection_records("email_outbox",
                                                  base_dir=data_dir)


def track(objects_root, token):
    return run(objects_root, "site_order_status", "GET", {"token": token})


# --- 1. the token is minted where the shopper can be handed it ---------------

def test_checkout_mints_the_orders_token_and_returns_the_link(tmp_path, monkeypatch):
    """The shopper is standing there. A reaction handler runs post-commit
    and cannot put anything in this response, so checkout mints the token
    itself -- exactly as it already does for the invoice's pay link."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "mug", "Enamel Mug", 1200)
    location(data_dir)
    stock_in(data_dir, "mug", 5)
    run(objects_root, "action_cart", "POST",
        {"session_token": "sess-1", "action": "add", "product_id": "mug",
         "quantity": "2"})

    result = run(objects_root, "action_checkout", "POST",
                 {"session_token": "sess-1", "customer_email": "ada@example.test",
                  "customer_name": "Ada Lovelace"})
    assert result["ok"] is True
    assert result["track_path"].startswith(TRACK_PREFIX)

    token = result["track_path"][len(TRACK_PREFIX):]
    assert len(token) >= 20            # a guessable token is not a token
    stored = orders_row(data_dir, result["order_id"])["portal_token"]
    assert stored == token

    # The pay link the shop already handed over is untouched: this slice
    # rides alongside it, it does not replace it.
    assert result["pay_path"].startswith("/pay/")


def test_a_retried_checkout_hands_back_the_same_tracking_link(tmp_path, monkeypatch):
    """A double-clicked submit button must not mint a second door onto a
    sale that already has one."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "mug", "Enamel Mug", 1200)
    location(data_dir)
    stock_in(data_dir, "mug", 5)
    run(objects_root, "action_cart", "POST",
        {"session_token": "sess-1", "action": "add", "product_id": "mug"})
    first = run(objects_root, "action_checkout", "POST",
                {"session_token": "sess-1", "customer_email": "ada@example.test"})

    again = run(objects_root, "action_checkout", "POST",
                {"session_token": "sess-1", "customer_email": "ada@example.test"})
    assert again["duplicate"] is True
    assert again["track_path"] == first["track_path"]


def test_the_reaction_mints_for_an_order_that_lacks_one(tmp_path, monkeypatch):
    """The durable path: an order raised anywhere but the web checkout --
    by hand, by an importer -- still gets its door."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    order(data_dir, status="confirmed")
    assert orders_row(data_dir)["portal_token"] == ""

    result = fire(objects_root, "system_order_portal_link", "orders", "ord-1")
    assert result["minted"] is True
    assert orders_row(data_dir)["portal_token"]


def test_the_reaction_skips_an_order_that_already_has_one(tmp_path, monkeypatch):
    """Skipped, not rotated. The dispatcher replays by design, and by the
    time a second event arrives the link is usually already in a
    customer's inbox -- rewriting it would break a link the shop sent."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    order(data_dir, status="confirmed", portal_token="already-minted-token")

    result = fire(objects_root, "system_order_portal_link", "orders", "ord-1")
    assert result.get("minted") is not True
    assert result["skipped"] == "already has a link"
    assert orders_row(data_dir)["portal_token"] == "already-minted-token"


def test_a_draft_order_gets_no_door(tmp_path, monkeypatch):
    """A draft is not a commitment. A tracking URL for something nobody
    has paid for is a link waiting to be forwarded."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    order(data_dir, status="draft")

    result = fire(objects_root, "system_order_portal_link", "orders", "ord-1")
    assert result["skipped"] == "still a draft"
    assert orders_row(data_dir)["portal_token"] == ""


def test_portal_token_is_read_only_and_off_every_generic_surface():
    """A capability URL a client could choose is not a capability, and a
    bearer token that turns up in a list response has already leaked."""
    import json
    schema = json.loads(
        (PACKAGES / "app-orders" / "schemas" / "orders.json").read_text())
    by_name = {f["name"]: f for f in schema["fields"]}
    assert by_name["portal_token"]["read_only"] is True
    assert "portal_token" not in schema["forms"]["default"]["fields"]
    assert "portal_token" not in schema["views"]["list_fields"]
    assert "portal_token" not in schema["search"]["fields"]


# --- 2. the tracking page ----------------------------------------------------

def test_the_tracking_page_renders_the_customers_own_receipt(tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    order(data_dir, status="confirmed", portal_token="track-me")
    order_line(data_dir)

    page = track(objects_root, "track-me")
    assert page.get("status", 200) == 200
    body = page["body"]
    assert "SO-0001" in body                     # order number
    assert "2026-07-01" in body                  # order date
    assert "Enamel Mug" in body                  # what was bought
    assert "24.00 USD" in body                   # and what it cost -- this IS
    assert "12.00 USD" in body                   # the receipt, unlike the slip


def test_an_unknown_token_is_a_friendly_404_that_leaks_nothing(tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    order(data_dir, status="confirmed", portal_token="track-me")
    order_line(data_dir)

    page = track(objects_root, "not-the-right-token")
    assert page["status"] == 404
    body = page["body"]
    assert "Traceback" not in body
    # Never the real order, never a 403 (which would confirm the token
    # namespace is worth attacking), never a hint that another value works.
    assert "SO-0001" not in body and "Enamel Mug" not in body
    assert "403" not in str(page)
    assert "track-me" not in body


def test_a_blank_token_never_matches_an_order_that_has_none(tmp_path, monkeypatch):
    """Without the guard, "no link minted yet" would be an open door onto
    whichever unlinked order happens to be first in the file."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    order(data_dir, status="confirmed")          # portal_token left blank

    assert track(objects_root, "")["status"] == 404


def test_an_order_id_opens_nothing(tmp_path, monkeypatch):
    """There is no enumeration path from a guessed id: the page resolves
    by token and only by token."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    order(data_dir, status="confirmed", portal_token="track-me")

    assert track(objects_root, "ord-1")["status"] == 404


STATUS_WORDS = (("confirmed", "Order received"),
                ("processing", "Being prepared"),
                ("partial", "Being prepared"),
                ("shipped", "On its way"),
                ("delivered", "Delivered"),
                ("cancelled", "Cancelled"))


def test_the_status_is_shown_in_customer_words_not_warehouse_words(tmp_path, monkeypatch):
    """Both directions. A mapping that is present but unused is not a
    mapping, and `partial` on a customer's screen reads as "part of my
    money went missing".

    One order per status rather than one order walked through them: the
    schema's transition table guards the moves, and this test is about
    what a customer READS, not about which moves are legal.
    """
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    for index, (raw, _customer) in enumerate(STATUS_WORDS):
        order(data_dir, f"ord-{index}", status=raw, number=f"SO-{index}",
              portal_token=f"token-{index}")

    for index, (raw, customer) in enumerate(STATUS_WORDS):
        body = track(objects_root, f"token-{index}")["body"]
        assert customer in body, f"{raw} should read as {customer!r}"
        # The warehouse-only words, none of which is ordinary English a
        # customer sentence would use by accident. ("received" is excluded
        # only because "Order received" legitimately contains it.)
        for internal in ("partial", "processing", "confirmed", "draft"):
            assert internal not in body, (
                f"internal word {internal!r} reached the customer on a "
                f"{raw} order")


def test_the_tracking_number_appears_when_a_shipment_carries_one(tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    order(data_dir, status="shipped", portal_token="track-me")
    order_line(data_dir)
    shipment(data_dir)

    body = track(objects_root, "track-me")["body"]
    assert "Royal Mail" in body
    assert "RM123456789GB" in body


def test_a_shipment_with_no_tracking_says_nothing_rather_than_nothing_useful(
        tmp_path, monkeypatch):
    """"Tracking: (none)" tells a worried customer only that the shop has
    a field for it."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    order(data_dir, status="shipped", portal_token="track-me")
    shipment(data_dir, carrier="", tracking="")

    body = track(objects_root, "track-me")["body"]
    assert "Your parcel" not in body
    assert "On its way" in body            # the status still speaks


def test_the_pay_link_shows_while_money_is_owed_and_not_after(tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    order(data_dir, status="confirmed", portal_token="track-me",
          invoice_id="inv-1")
    invoice(data_dir, status="sent")
    assert "/pay/invoice-token-xyz" in track(objects_root, "track-me")["body"]

    object_records.update_collection_record(
        "invoices", "inv-1", {"status": "paid"}, base_dir=data_dir,
        actor="test")
    # Offering "pay now" on a settled order is how a shop gets paid twice
    # and spends a fortnight refunding it.
    assert "/pay/invoice-token-xyz" not in track(objects_root, "track-me")["body"]


def test_the_return_link_appears_once_something_has_left_the_building(
        tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    order(data_dir, status="confirmed", portal_token="track-me")
    assert "/returns/ord-1" not in track(objects_root, "track-me")["body"]

    object_records.update_collection_record(
        "orders", "ord-1", {"status": "shipped"}, base_dir=data_dir,
        actor="test")
    body = track(objects_root, "track-me")["body"]
    # Carrying the token, because the guest it is for has no account.
    assert "/returns/ord-1?token=track-me" in body


def test_the_tracking_page_is_not_a_door_into_the_app(tmp_path, monkeypatch):
    """Handed to a stranger's inbox: it must not be one click from
    somebody else's order or from a sign-in wall."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    order(data_dir, status="confirmed", portal_token="track-me")

    body = track(objects_root, "track-me")["body"]
    assert "/nav" not in body
    assert "/login" not in body
    assert 'href="/orders"' not in body


# --- 3. the three emails -----------------------------------------------------

def kinds(data_dir):
    return sorted(row["subject"] for row in outbox(data_dir))


def test_confirmation_queues_once_and_a_replay_queues_nothing(tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch,
                                       settings=(("portal.base_url",
                                                  "https://shop.example.test"),))
    order(data_dir, status="confirmed", portal_token="track-me",
          invoice_id="inv-1")
    order_line(data_dir)
    invoice(data_dir, status="sent")

    result = fire(objects_root, "system_order_email", "orders", "ord-1")
    assert result["queued"] == ["confirmed"]
    mails = outbox(data_dir)
    assert len(mails) == 1
    mail = mails[0]
    assert mail["to"] == "ada@example.test"
    assert "SO-0001" in mail["subject"]
    assert "Enamel Mug" in mail["text_body"]
    assert "24.00 USD" in mail["text_body"]
    assert "https://shop.example.test/orders/track/track-me" in mail["text_body"]
    assert "https://shop.example.test/pay/invoice-token-xyz" in mail["text_body"]
    assert mail["status"] == "queued"

    # The dispatcher is at-least-once by design. Twice is a support ticket.
    replay = fire(objects_root, "system_order_email", "orders", "ord-1")
    assert replay["queued"] == []
    assert replay["skipped_already_sent"] == ["confirmed"]
    assert len(outbox(data_dir)) == 1


def test_the_shipped_note_carries_carrier_and_tracking_and_queues_once(
        tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch,
                                       settings=(("portal.base_url",
                                                  "https://shop.example.test"),))
    order(data_dir, status="shipped", portal_token="track-me")
    order_line(data_dir)
    shipment(data_dir)

    result = fire(objects_root, "system_order_email", "orders", "ord-1")
    # Reaching `shipped` also means the confirmation was owed and never
    # sent -- the zero-touch shop confirms and ships in one burst, and a
    # buyer who never heard from the shop because their order moved too
    # fast is exactly the silence this slice ends.
    assert sorted(result["queued"]) == ["confirmed", "shipped"]

    shipped = next(row for row in outbox(data_dir)
                   if "has shipped" in row["subject"])
    assert "Royal Mail" in shipped["text_body"]
    assert "RM123456789GB" in shipped["text_body"]
    assert "https://shop.example.test/orders/track/track-me" in shipped["text_body"]

    replay = fire(objects_root, "system_order_email", "orders", "ord-1")
    assert replay["queued"] == []
    assert len(outbox(data_dir)) == 2


def test_the_refund_note_names_the_amount_and_the_order_and_queues_once(
        tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    order(data_dir, status="delivered", portal_token="track-me",
          invoice_id="inv-1")
    invoice(data_dir, status="paid")
    payment(data_dir)
    refund(data_dir, cents=1200)

    result = fire(objects_root, "system_order_email", "refunds", "ref-1",
                  action="create")
    assert result["queued"] == ["refunded"]
    mail = outbox(data_dir)[0]
    assert mail["to"] == "ada@example.test"
    assert "SO-0001" in mail["subject"]
    assert "12.00 USD" in mail["text_body"]
    assert "SO-0001" in mail["text_body"]

    replay = fire(objects_root, "system_order_email", "refunds", "ref-1",
                  action="create")
    assert replay["queued"] == []
    assert len(outbox(data_dir)) == 1


def test_a_second_refund_on_one_order_gets_its_own_message(tmp_path, monkeypatch):
    """The marker carries the refund's id, not just the order's: two
    refunds are two facts, and a customer told about the first but not the
    second is a phone call about money."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    order(data_dir, status="delivered", portal_token="track-me",
          invoice_id="inv-1")
    invoice(data_dir, status="paid")
    payment(data_dir)
    refund(data_dir, "ref-1", cents=1200)
    refund(data_dir, "ref-2", cents=600)

    fire(objects_root, "system_order_email", "refunds", "ref-1", action="create")
    fire(objects_root, "system_order_email", "refunds", "ref-2", action="create")
    bodies = [row["text_body"] for row in outbox(data_dir)]
    assert len(bodies) == 2
    assert any("12.00 USD" in body for body in bodies)
    assert any("6.00 USD" in body for body in bodies)


def test_the_idempotency_marker_lives_on_the_outbox_row(tmp_path, monkeypatch):
    """The claim being recorded is "this message exists", so it is
    recorded on the message. A stamped flag elsewhere is a second record
    that can disagree with the first."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    order(data_dir, status="confirmed", portal_token="track-me")

    fire(objects_root, "system_order_email", "orders", "ord-1")
    assert outbox(data_dir)[0]["source_object_id"] == (
        "system_order_email:orders/ord-1:confirmed")


def test_an_order_with_no_customer_email_is_skipped_with_a_reason(
        tmp_path, monkeypatch):
    """Never a crash, never a message addressed to "". An operator reading
    the reason knows exactly what to add; silence looks like a working
    mailer."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    order(data_dir, status="confirmed", portal_token="track-me", email="")

    result = fire(objects_root, "system_order_email", "orders", "ord-1")
    assert result["ok"] is True
    assert result["skipped"] == "no customer_email on the order"
    assert outbox(data_dir) == []


def test_no_base_url_sends_the_words_without_a_dead_link(tmp_path, monkeypatch):
    """A relative URL in an email is a link that does nothing in every
    mail client there is. Honest text beats a guaranteed 404."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)   # no base_url
    order(data_dir, status="confirmed", portal_token="track-me")

    fire(objects_root, "system_order_email", "orders", "ord-1")
    body = outbox(data_dir)[0]["text_body"]
    assert "http" not in body
    assert "/orders/track/" not in body
    assert "SO-0001" in body            # the message still says something


def test_no_smtp_configured_still_queues_and_does_not_error(tmp_path, monkeypatch):
    """The daemon already logs "queuing only" when there is no transport.
    The shop's voice is recorded even where it cannot yet be heard."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    monkeypatch.delenv("DBBASIC_SMTP_MODE", raising=False)
    order(data_dir, status="confirmed", portal_token="track-me")

    result = fire(objects_root, "system_order_email", "orders", "ord-1")
    assert result["ok"] is True
    assert outbox(data_dir)[0]["status"] == "queued"


def test_a_draft_order_says_nothing_at_all(tmp_path, monkeypatch):
    """Nobody has paid, nothing has been promised, and an email about a
    draft is a message about something that may never exist."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    order(data_dir, status="draft", portal_token="track-me")

    result = fire(objects_root, "system_order_email", "orders", "ord-1")
    assert result["queued"] == []
    assert outbox(data_dir) == []


# --- 4. the return a guest can finally start ---------------------------------

def return_form(objects_root, *, order_id="ord-1", token=None, user_id=None,
                method="GET", **fields):
    payload = {"order_id": order_id}
    if token is not None:
        payload["token"] = token
    if user_id:
        payload["_identity"] = {"user_id": user_id}
    if fields:
        payload["_form"] = fields
    return run(objects_root, "site_return_form", method, payload)


def shop_that_shipped(data_dir):
    """An order whose goods actually left, so there is something to send
    back -- the return form folds shipped-minus-claimed live."""
    product(data_dir, "mug", "Enamel Mug", 1200)
    order(data_dir, status="shipped", portal_token="track-me")
    order_line(data_dir, quantity="2")
    shipment(data_dir)
    object_records.create_collection_record(
        "shipment_lines",
        {"id": "sl-1", "shipment_id": "shp-1", "order_line_id": "line-1",
         "product_id": "mug", "quantity": "2", "owner_id": "shop"},
        base_dir=data_dir)


def test_a_guest_with_the_order_token_can_open_the_return_form(tmp_path, monkeypatch):
    """The whole point. A guest checkout is the default sale here, so a
    returns flow that requires an account is a returns flow nobody can
    use."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    shop_that_shipped(data_dir)

    body = return_form(objects_root, token="track-me")["body"]
    assert "Enamel Mug" in body
    assert 'name="qty_line-1"' in body
    # The token rides through the POST, or the submit button would bounce
    # the very visitor the GET just let in.
    assert 'name="token" value="track-me"' in body


def test_a_guest_with_a_wrong_token_is_refused_without_being_told_why(
        tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    shop_that_shipped(data_dir)

    page = return_form(objects_root, token="wrong-token")
    body = page["body"]
    assert "Enamel Mug" not in body
    assert "tracking link" in body
    assert page.get("status", 200) == 200      # never a 404 that says "close"

    # A real order with a wrong token and an order that does not exist are
    # indistinguishable, so a guessed id never confirms itself. Only the
    # sign-in link's `next=` differs, because it echoes the URL asked for.
    unknown = return_form(objects_root, order_id="no-such-order",
                          token="wrong-token")
    assert unknown.get("status", 200) == 200
    assert (unknown["body"].replace("no-such-order", "ord-1") == body)


def test_a_guest_token_for_another_order_opens_nothing(tmp_path, monkeypatch):
    """The token is checked against THIS order, not against any order."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    shop_that_shipped(data_dir)
    order(data_dir, "ord-2", status="shipped", number="SO-0002",
          portal_token="someone-elses-token")

    body = return_form(objects_root, order_id="ord-1",
                       token="someone-elses-token")["body"]
    assert "Enamel Mug" not in body


def test_a_guest_can_actually_submit_the_return(tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    shop_that_shipped(data_dir)

    body = return_form(objects_root, token="track-me", method="POST",
                       **{"reason": "damaged", "qty_line-1": "1"})["body"]
    assert "authorized" in body
    rmas = object_records.read_collection_records("return_authorizations",
                                                  base_dir=data_dir)
    assert len(rmas) == 1
    assert rmas[0]["order_id"] == "ord-1"


def test_the_signed_in_path_still_works(tmp_path, monkeypatch):
    """Staff raise returns at the counter the way they always have; the
    token is an addition, not a replacement."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    shop_that_shipped(data_dir)

    body = return_form(objects_root, user_id="shop")["body"]
    assert "Enamel Mug" in body
    assert 'name="qty_line-1"' in body
    # No token was used, so none is carried -- a signed-in session does
    # not need one and putting one in the HTML would leak it to a shared
    # screen.
    assert 'name="token"' not in body


def test_the_tracking_pages_return_link_actually_opens_the_form(
        tmp_path, monkeypatch):
    """End to end, because a link that renders and does not work is the
    exact failure this slice exists to remove."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    shop_that_shipped(data_dir)

    page = track(objects_root, "track-me")["body"]
    assert "/returns/ord-1?token=track-me" in page

    href = "/returns/ord-1?token=track-me"
    order_id, query = href[len("/returns/"):].split("?", 1)
    token = query.split("=", 1)[1]
    body = return_form(objects_root, order_id=order_id, token=token)["body"]
    assert "Enamel Mug" in body


# --- 5. the package says what it ships ---------------------------------------

def _rows(package_id, collection):
    import csv
    path = PACKAGES / package_id / "seed" / f"{collection}.tsv"
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def test_the_tracking_route_is_seeded_and_cannot_shadow_the_order_permalink():
    """Three segments against the permalink's two, so they can never match
    the same path -- but the priority is lower anyway, because a route
    named `track` sitting where an order id goes is exactly the collision
    worth being explicit about."""
    routes = {row["pattern"]: row for row in _rows("app-orders", "site_routes")}
    track_route = routes["/orders/track/{token}"]
    assert track_route["object_id"] == "site_order_status"
    assert int(track_route["priority"]) < int(routes["/orders/{order_id}"]["priority"])


def test_the_tracking_page_is_public_and_the_orders_collection_is_not():
    """Public EXECUTE on the page object, never public READ on the
    collection -- the same split app-invoices uses for its portal."""
    import json
    import object_permissions
    payload = json.loads(
        (PACKAGES / "app-orders" / "permissions" / "rules.json").read_text())
    policy = object_permissions.policy_from_dict(
        {"access_mode": "role_based", "rules": payload["rules"]})

    page = object_permissions.check_permission(
        None, object_permissions.EXECUTE, policy=policy,
        object_id="site_order_status")
    assert page.allowed is True

    collection = object_permissions.check_permission(
        None, object_permissions.READ, policy=policy, collection="orders",
        record={"owner_id": "shop", "portal_token": "track-me"})
    assert collection.allowed is False


def test_the_return_form_is_public_now_because_a_guest_shop_needs_it_to_be():
    import json
    import object_permissions
    payload = json.loads(
        (PACKAGES / "app-returns" / "permissions" / "rules.json").read_text())
    policy = object_permissions.policy_from_dict(
        {"access_mode": "role_based", "rules": payload["rules"]})

    decision = object_permissions.check_permission(
        None, object_permissions.EXECUTE, policy=policy,
        object_id="site_return_form")
    assert decision.allowed is True


def test_an_order_with_no_readable_lines_still_shows_the_total(tmp_path, monkeypatch):
    """A "What you ordered" heading over an empty space reads as "we have
    lost your order". The one number the customer cares about survives."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    order(data_dir, status="confirmed", portal_token="track-me")   # no lines

    body = track(objects_root, "track-me")["body"]
    assert "24.00 USD" in body
    assert "Total paid" in body
