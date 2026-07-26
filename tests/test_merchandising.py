"""Merchandising: categories, variants, images, and two words from the
shopper.

The design decision under test is that A VARIANT IS A PRODUCT. Nothing in
the basket, the checkout, the stock ledger or the books needed a line
changed to sell sizes and colours, because all of them were already keyed
on product_id -- so what is worth testing is the seam where that decision
becomes visible: one card per parent rather than one per size, a heading
that cannot be bought, a picker whose radios post a child's product_id,
and an out-of-stock size that is SHOWN rather than hidden (hiding it is
how a shopper concludes this shop does not sell their size).

The rest is the two things a text-only shop is missing: a photograph,
which is a pointer to the file endpoint that already exists and an honest
blank when there is none, and the two optional fields a shopper types at
checkout that the person packing the box is the only one who needs.
"""

import json
import pathlib
import shutil

from conftest import stage_collection

import object_execution
import object_ids
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
SHOP_OBJECTS = PACKAGES / "app-shop" / "objects"
SHIPPING_OBJECTS = PACKAGES / "app-shipping" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

TOKEN = "sess-merch-1"
COOKIE = "cart"


def setup_env(tmp_path, monkeypatch, *, settings=()):
    """A data dir with every collection this slice touches, and ONE object
    root holding the shop's and the shipping package's objects.

    The merged root is what an installed server actually looks like, and
    the only way site_shop's sibling calls (action_cart, action_checkout)
    and the packing slip resolve the way they will in production.
    """
    data_dir = tmp_path / "data"
    for pkg, name in (("app-shop", "carts"), ("app-shop", "cart_items"),
                      ("app-catalog", "products"), ("app-catalog", "locations"),
                      ("app-catalog", "stock_moves"),
                      ("app-orders", "orders"), ("app-orders", "order_lines"),
                      ("app-invoices", "invoices"), ("app-invoices", "invoice_lines"),
                      ("app-payments", "payments"),
                      ("app-shipping", "shipments"),
                      ("app-shipping", "shipment_lines")):
        stage_collection(data_dir, pkg, name)
    rows = "".join(f"s{i}\t{k}\t{v}\t\n" for i, (k, v) in enumerate(settings))
    stage_collection(data_dir, "app-settings", "app_settings", rows=rows)

    objects_root = tmp_path / "objects"
    for source in (SHOP_OBJECTS, SHIPPING_OBJECTS):
        shutil.copytree(source, objects_root, dirs_exist_ok=True)

    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_root))
    return data_dir, objects_root


def teach_orders_the_note_fields(data_dir):
    """No-op since orders v4 carries the columns; kept as the seam's name.

    When these tests were written the fields lived only on the cart and
    this helper doctored the staged schema to prove the day-it-lands
    branch. orders v4 shipped them, so the real schema now answers yes and
    the helper has nothing to add -- but the call sites still read as
    "this test is about the columns existing", which is worth more than
    deleting it.
    """
    return


def hide_the_note_fields(data_dir):
    """Strip customer_note/gift_message from the STAGED orders schema.

    The inverse of the original helper, and now the one doing real work:
    action_checkout asks the schema (`_has_field`) rather than assuming,
    exactly as it does for shipping_cents, so BOTH branches deserve a
    test -- including the one an operator running an older app-orders
    still lives in.
    """
    path = data_dir / "schemas" / "orders.json"
    schema = json.loads(path.read_text())
    schema["fields"] = [field for field in schema["fields"]
                        if field["name"] not in ("customer_note", "gift_message")]
    path.write_text(json.dumps(schema))


def run(objects_root, object_id, method, payload):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            object_id, method=method, payload=payload),
        roots=[objects_root]).result


def cart(objects_root, action="get", **payload):
    return run(objects_root, "action_cart", "POST",
               {"session_token": TOKEN, "action": action, **payload})


def checkout(objects_root, **payload):
    return run(objects_root, "action_checkout", "POST",
               {"session_token": TOKEN, **payload})


def page(objects_root, **payload):
    payload.setdefault("_cookies", {COOKIE: TOKEN})
    return run(objects_root, "site_shop", "GET", payload)


def product(data_dir, product_id, name, cents=1200, **fields):
    record = {"id": product_id, "name": name, "sku": product_id.upper(),
              "product_type": "physical", "price_cents": str(cents),
              "currency": "USD", "is_active": "true", "owner_id": "shop"}
    record.update({key: str(value) for key, value in fields.items()})
    return object_records.create_collection_record("products", record,
                                                   base_dir=data_dir)


def location(data_dir, location_id, name, kind="warehouse"):
    return object_records.create_collection_record(
        "locations",
        {"id": location_id, "name": name, "location_type": kind,
         "owner_id": "shop"},
        base_dir=data_dir)


def stock_in(data_dir, product_id, quantity, *, to="loc-shelf"):
    return object_records.create_collection_record(
        "stock_moves",
        {"id": object_ids.new_uuid4(), "product_id": product_id,
         "to_location_id": to, "quantity": str(quantity), "reason": "purchase",
         "occurred_at": "2026-06-01", "owner_id": "shop"},
        base_dir=data_dir)


def tote_with_variants(data_dir, *, parent_cents=0):
    """A parent that is a heading and two variants that are real things.

    parent_cents stays 0 by default: that is what makes the parent
    unsellable, and it is the ordinary case -- nobody sells "a tote bag",
    they sell the medium navy one.
    """
    product(data_dir, "tote", "Tote Bag", parent_cents, category="Bags")
    product(data_dir, "tote-m", "Tote Bag, medium", 2000, category="Bags",
            parent_product_id="tote", options='{"size": "M", "colour": "navy"}')
    product(data_dir, "tote-l", "Tote Bag, large", 2500, category="Bags",
            parent_product_id="tote", options='{"size": "L", "colour": "navy"}')


# --- categories: one flat field, grouped, and nothing hidden -----------------

def test_the_index_groups_cards_under_category_headings(tmp_path, monkeypatch):
    """Alphabetical, because there is no ordering field and inventing one
    would be the first plank of the taxonomy tree this deliberately is
    not."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Enamel Mug", 1200, category="Kitchen")
    product(data_dir, "p2", "Tea Towel", 800, category="Kitchen")
    product(data_dir, "p3", "Linocut Print", 4000, category="Art")
    body = page(objects_root)["body"]

    assert '<h2 class="shop-category">Art</h2>' in body
    assert '<h2 class="shop-category">Kitchen</h2>' in body
    assert body.index("Art</h2>") < body.index("Kitchen</h2>")
    # The cards land under the right heading, not merely on the page.
    kitchen = body[body.index("Kitchen</h2>"):]
    assert "Enamel Mug" in kitchen and "Tea Towel" in kitchen
    assert "Linocut Print" not in kitchen


def test_an_uncategorised_product_lands_in_everything_else_and_is_still_sold(
        tmp_path, monkeypatch):
    """Never hidden. A product nobody got round to filing is still stock
    somebody paid for, and a shop that dropped it off the page would be
    losing sales it could not see it was losing."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Enamel Mug", 1200, category="Kitchen")
    product(data_dir, "p2", "Odd Thing", 500)               # no category
    body = page(objects_root)["body"]

    assert '<h2 class="shop-category">Everything else</h2>' in body
    assert body.index("Kitchen</h2>") < body.index("Everything else</h2>")
    tail = body[body.index("Everything else</h2>"):]
    assert "Odd Thing" in tail
    assert 'name="product_id" value="p2"' in tail          # still addable


def test_a_shop_that_categorises_nothing_gets_the_grid_it_always_had(
        tmp_path, monkeypatch):
    """It is not a shop with one category called "Everything else": it is
    a shop that does not use categories, and a heading over the whole page
    would be furniture pretending to be information."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Enamel Mug", 1200)
    product(data_dir, "p2", "Tea Towel", 800)
    body = page(objects_root)["body"]

    assert '<h2 class="shop-category">' not in body
    assert "Everything else" not in body
    assert body.count('<div class="shop-grid">') == 1


# --- variants: a variant IS a product ----------------------------------------

def test_one_card_per_parent_and_the_variants_are_not_cards_of_their_own(
        tmp_path, monkeypatch):
    """A grid showing Small, Medium and Large as three tiles is a grid
    nobody can read: the shopper is looking for a tote bag, not for the
    medium."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    tote_with_variants(data_dir)
    body = page(objects_root)["body"]

    assert body.count('<div class="shop-card">') == 1
    assert '<a href="/shop/tote">Tote Bag</a>' in body
    assert "Tote Bag, medium" not in body
    assert 'name="product_id" value="tote-m"' not in body

    # The parent is a heading, so the card offers the only thing that is
    # possible -- go and choose -- and prices itself from the cheapest
    # child rather than printing a 0.00 nobody can buy anything at.
    assert "Choose options" in body
    assert "from USD 20.00" in body
    assert 'name="do" value="add"' not in body


def test_a_parent_with_variants_cannot_go_in_a_basket_and_the_refusal_names_them(
        tmp_path, monkeypatch):
    """A "no" that does not say what to do instead is a lost sale."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    tote_with_variants(data_dir)

    refused = cart(objects_root, "add", product_id="tote")
    assert refused["status"] == 409
    assert "size and colour" in refused["error"]            # the seller's words
    assert "M / navy" in refused["error"] and "L / navy" in refused["error"]
    # And the same answer as data, for an API client or an agent that has
    # to choose one without scraping prose.
    assert {choice["product_id"] for choice in refused["options"]} == {
        "tote-m", "tote-l"}
    assert {choice["label"] for choice in refused["options"]} == {
        "M / navy", "L / navy"}
    assert object_records.read_collection_records(
        "cart_items", base_dir=data_dir) == []


def test_a_parent_that_carries_its_own_price_is_still_sellable(
        tmp_path, monkeypatch):
    """Having variants does not make a product a heading -- having no price
    of its own does. A base model that is genuinely for sale stays for
    sale."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    location(data_dir, "loc-shelf", "Shelf")
    tote_with_variants(data_dir, parent_cents=1800)
    stock_in(data_dir, "tote", 4)
    assert cart(objects_root, "add", product_id="tote")["subtotal_cents"] == 1800
    # And it appears in its own picker beside the variants, under its own
    # name rather than a set of options it does not have.
    body = page(objects_root, product_id="tote")["body"]
    assert 'value="tote" required> Tote Bag' in body


def test_adding_a_variant_works_and_the_line_says_which_one(
        tmp_path, monkeypatch):
    """The whole argument in one assertion: the basket took a product_id
    and stamped a description, exactly as it does for anything else. No
    code in this path knows the word "variant"."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    tote_with_variants(data_dir)

    result = cart(objects_root, "add", product_id="tote-m", quantity="2")
    assert result["subtotal_cents"] == 4000
    item = object_records.read_collection_records("cart_items",
                                                  base_dir=data_dir)[0]
    assert item["product_id"] == "tote-m"
    assert item["description"] == "Tote Bag, medium"
    assert item["unit_price_cents"] == "2000"


def test_the_parent_page_renders_a_picker_holding_every_variant(
        tmp_path, monkeypatch):
    """Radios posting a CHILD's product_id into the same add form every
    card uses -- one path into a basket, still, and no JavaScript."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    location(data_dir, "loc-shelf", "Shelf")
    tote_with_variants(data_dir)
    stock_in(data_dir, "tote-m", 3)
    stock_in(data_dir, "tote-l", 2)
    body = page(objects_root, product_id="tote")["body"]

    assert 'name="do" value="add"' in body
    assert '<fieldset class="options">' in body
    assert 'name="product_id" value="tote-m"' in body
    assert 'name="product_id" value="tote-l"' in body
    assert "M / navy" in body and "L / navy" in body
    assert "USD 20.00" in body and "USD 25.00" in body
    # Nothing pre-selected: a default that quietly ships the small is a
    # wrong parcel, a return and a refund.
    assert "checked" not in body
    # The heading has no price of its own to print.
    assert "from USD 20.00" in body


def test_an_out_of_stock_variant_is_shown_and_not_addable(
        tmp_path, monkeypatch):
    """Hiding the medium is how a shopper concludes this shop does not
    sell their size and goes to one that does. Saying so tells them to
    come back."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    location(data_dir, "loc-shelf", "Shelf")
    tote_with_variants(data_dir)
    stock_in(data_dir, "tote-l", 3)                     # medium has none
    body = page(objects_root, product_id="tote")["body"]

    assert 'value="tote-m" disabled' in body
    assert "Out of stock" in body
    assert "M / navy" in body                           # shown, not hidden
    assert 'value="tote-l" required' in body


def test_a_variant_page_says_what_it_is_and_finds_its_way_back(
        tmp_path, monkeypatch):
    """A shopper who landed on the medium from a search result needs the
    other sizes, or the only size this shop appears to sell is the one a
    search engine happened to index."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    tote_with_variants(data_dir)
    body = page(objects_root, product_id="tote-m")["body"]

    assert "size: M, colour: navy" in body
    assert '<a href="/shop/tote">All options of Tote Bag</a>' in body
    # It is a product like any other: its own price and its own add form.
    assert "USD 20.00" in body
    assert 'name="product_id" value="tote-m"' in body


def test_a_variant_whose_parent_is_withdrawn_is_still_a_product(
        tmp_path, monkeypatch):
    """Otherwise it would be collapsed under a card that is not on the
    page at all -- stock nobody can see and nobody can buy."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    tote_with_variants(data_dir)
    object_records.update_collection_record(
        "products", "tote", {"is_active": "false"}, base_dir=data_dir,
        actor="test")
    body = page(objects_root)["body"]

    assert "Tote Bag, medium" in body
    assert 'name="product_id" value="tote-m"' in body       # addable
    assert body.count('<div class="shop-card">') == 2


# --- images: a pointer, and an honest blank ----------------------------------

def test_a_product_with_an_image_renders_one_from_the_file_endpoint(
        tmp_path, monkeypatch):
    """The catalogue stores an id and the bytes come from the endpoint
    app-files already serves. A shop-specific image route would be a
    second way to read the same bytes with its own permission story to get
    wrong."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Enamel Mug", 1200, image_file_id="file-abc")
    for body in (page(objects_root)["body"],
                 page(objects_root, product_id="p1")["body"]):
        assert '<img class="shop-image" src="/api/files/file-abc"' in body
        assert 'alt="Enamel Mug"' in body                # the name, as alt text
        assert '<div class="shop-image placeholder">' not in body


def test_a_product_with_no_image_gets_a_blank_block_not_a_broken_one(
        tmp_path, monkeypatch):
    """A broken-image icon reads as a shop whose pages do not work, which
    is worse than a product nobody has photographed yet."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Enamel Mug", 1200)
    for body in (page(objects_root)["body"],
                 page(objects_root, product_id="p1")["body"]):
        assert '<div class="shop-image placeholder"></div>' in body
        assert "<img" not in body


# --- gift and special instructions -------------------------------------------

NOTE = "Leave it with the neighbour at number 12"
GIFT = "Happy birthday, Ada"


def test_the_checkout_form_offers_both_fields_and_no_gift_flag(
        tmp_path, monkeypatch):
    """No tickbox, because the slip carries no prices by construction and
    every parcel is already gift-safe. A flag somebody forgets is how the
    amount paid ends up stapled to a present."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Mug", 1200, product_type="service")
    cart(objects_root, "add", product_id="p1")
    body = page(objects_root)["body"]

    assert '<textarea name="customer_note"' in body
    assert '<textarea name="gift_message"' in body
    assert "which never shows prices" in body
    assert 'name="is_gift"' not in body


def test_checkout_stamps_both_notes_on_the_order_when_orders_has_them(
        tmp_path, monkeypatch):
    """The packer reads the ORDER. A note that lives only on a basket is a
    note nobody in the warehouse ever sees."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    teach_orders_the_note_fields(data_dir)
    product(data_dir, "p1", "Mug", 1200, product_type="service")
    cart(objects_root, "add", product_id="p1")

    result = checkout(objects_root, customer_email="ada@example.test",
                      customer_note=NOTE, gift_message=GIFT)
    assert result["ok"]
    assert "notes_on_cart_only" not in result

    order = object_records.get_collection_record("orders", result["order_id"],
                                                 base_dir=data_dir)
    assert order["customer_note"] == NOTE
    assert order["gift_message"] == GIFT


def test_without_the_columns_the_notes_stay_on_the_cart_and_it_is_reported(
        tmp_path, monkeypatch):
    """Schema-aware, exactly as shipping_cents is: a missing column costs
    the packer a note, a rejected write would cost the shopper the order.
    Saying so is the difference between a degraded outcome and a silent
    one."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    hide_the_note_fields(data_dir)
    product(data_dir, "p1", "Mug", 1200, product_type="service")
    cart(objects_root, "add", product_id="p1")

    result = checkout(objects_root, customer_email="ada@example.test",
                      customer_note=NOTE, gift_message=GIFT)
    assert result["ok"]
    assert sorted(result["notes_on_cart_only"]) == ["customer_note",
                                                    "gift_message"]
    basket = object_records.get_collection_record("carts", result["cart_id"],
                                                  base_dir=data_dir)
    assert basket["customer_note"] == NOTE
    assert basket["gift_message"] == GIFT


def test_a_checkout_with_no_notes_writes_none_and_says_nothing(
        tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    teach_orders_the_note_fields(data_dir)
    product(data_dir, "p1", "Mug", 1200, product_type="service")
    cart(objects_root, "add", product_id="p1")

    result = checkout(objects_root, customer_email="ada@example.test")
    assert "notes_on_cart_only" not in result
    order = object_records.get_collection_record("orders", result["order_id"],
                                                 base_dir=data_dir)
    assert not order.get("customer_note") and not order.get("gift_message")


def test_the_packing_slip_prints_both_and_still_shows_no_prices(
        tmp_path, monkeypatch):
    """The end of the chain the shopper started at a textarea. And the
    property that makes the gift message safe to print at all: this page
    has no prices on it, ever, so there is nothing to suppress."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    teach_orders_the_note_fields(data_dir)
    product(data_dir, "p1", "Enamel Mug", 1200, product_type="service")
    cart(objects_root, "add", product_id="p1", quantity="2")
    placed = checkout(objects_root, customer_email="ada@example.test",
                      customer_name="Ada Lovelace",
                      customer_note=NOTE, gift_message=GIFT)

    order_line = [row for row in object_records.read_collection_records(
        "order_lines", base_dir=data_dir)
        if row["order_id"] == placed["order_id"]][0]
    shipment = object_records.create_collection_record(
        "shipments",
        {"id": "shp-1", "order_id": placed["order_id"], "status": "shipped",
         "shipped_on": "2026-06-16", "ship_to_name": "Ada Lovelace",
         "owner_id": "shop"},
        base_dir=data_dir)
    object_records.create_collection_record(
        "shipment_lines",
        {"id": "shl-1", "shipment_id": shipment["id"],
         "order_line_id": order_line["id"],
         "description": "Enamel Mug", "quantity": "2", "owner_id": "shop"},
        base_dir=data_dir)

    body = run(objects_root, "site_packing_slip", "GET",
               {"shipment_id": "shp-1"})["body"]
    assert "Special instructions" in body and NOTE in body
    assert "Gift message" in body and GIFT in body
    assert "$" not in body
    assert "12.00" not in body and "24.00" not in body
    assert "_cents" not in body


# --- the regression guard ----------------------------------------------------

def test_a_product_with_no_variants_behaves_exactly_as_it_always_did(
        tmp_path, monkeypatch):
    """Every field this slice added is optional, and a shop that sets none
    of them must not be able to tell the slice happened -- except for the
    photograph placeholder, which is the one deliberate change to a page
    that was text-only."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    location(data_dir, "loc-shelf", "Shelf")
    product(data_dir, "p1", "Enamel Mug", 1200,
            description="Twelve ounces. Chips beautifully.")
    stock_in(data_dir, "p1", 5)

    index = page(objects_root)["body"]
    assert '<a href="/shop/p1">Enamel Mug</a>' in index
    assert "USD 12.00" in index
    assert 'name="product_id" value="p1"' in index
    assert '<h2 class="shop-category">' not in index
    assert "Choose options" not in index

    detail = page(objects_root, product_id="p1")["body"]
    assert "Twelve ounces. Chips beautifully." in detail
    assert "In stock" in detail
    assert "<fieldset" not in detail                     # no picker
    assert "All options of" not in detail

    assert cart(objects_root, "add", product_id="p1", quantity="2")[
        "subtotal_cents"] == 2400
    placed = checkout(objects_root, customer_email="ada@example.test")
    assert placed["ok"] and placed["total_cents"] == 2400
