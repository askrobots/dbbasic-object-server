"""Behavior tests for object_stock.py: the derived-levels fold over the
immutable stock_moves append log (packages/app-catalog's STOCK slice).

Mirrors tests/test_app_orders_totals.py's install-then-exercise shape:
install app-catalog into an isolated data dir, write products/locations/
stock_moves records directly via object_records, then call object_stock's
functions and assert on the result. Structural/manifest/permission tests
for the schemas live in tests/test_app_catalog_package.py.
"""

import os
from decimal import Decimal
from pathlib import Path

import object_packages
import object_records
import object_stock

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"


def _install(tmp_path):
    """Install app-catalog into an isolated data dir/object root and
    return data_dir. Keeps DBBASIC_DATA_DIR in sync with tmp_path the same
    way test_app_orders_totals.py's _install() does, since object_stock's
    callers (objects/site/stock.py) resolve base_dir from that env var.
    """
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    os.environ["DBBASIC_DATA_DIR"] = str(data_dir)

    object_packages.install_package(
        "app-catalog", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root]
    )
    return data_dir


def _make_product(data_dir, **overrides):
    record = {"id": "p1", "name": "Widget", "owner_id": "u1"}
    record.update(overrides)
    return object_records.create_collection_record("products", record, base_dir=data_dir, actor="test")


def _make_location(data_dir, **overrides):
    record = {"id": "loc1", "name": "Main Warehouse", "location_type": "warehouse", "owner_id": "u1"}
    record.update(overrides)
    return object_records.create_collection_record("locations", record, base_dir=data_dir, actor="test")


def _make_move(data_dir, **overrides):
    record = {"id": "m1", "product_id": "p1", "quantity": "1", "owner_id": "u1"}
    record.update(overrides)
    return object_records.create_collection_record("stock_moves", record, base_dir=data_dir, actor="test")


# -- quantity_at_location ---------------------------------------------------

def test_quantity_at_location_nets_moves_in_and_out(tmp_path):
    data_dir = _install(tmp_path)
    _make_product(data_dir)
    _make_location(data_dir, id="wh", location_type="warehouse")
    _make_move(data_dir, id="m1", to_location_id="wh", quantity="10", reason="purchase")
    _make_move(data_dir, id="m2", from_location_id="wh", quantity="4", reason="sale")

    qty = object_stock.quantity_at_location("p1", "wh", base_dir=data_dir, owner="u1")
    assert qty == Decimal("6")
    assert isinstance(qty, Decimal)


def test_quantity_at_location_is_zero_for_a_never_touched_location(tmp_path):
    data_dir = _install(tmp_path)
    _make_product(data_dir)
    _make_location(data_dir, id="wh")

    assert object_stock.quantity_at_location("p1", "wh", base_dir=data_dir, owner="u1") == Decimal("0")


def test_quantity_at_location_returns_zero_for_a_blank_location_id(tmp_path):
    """Blank location ids are the source model's "external
    origin/destination" sentinel -- they name no real location, so there
    is nothing to hold a balance at.
    """
    data_dir = _install(tmp_path)
    _make_product(data_dir)

    assert object_stock.quantity_at_location("p1", "", base_dir=data_dir, owner="u1") == Decimal("0")


def test_a_transfer_moves_quantity_between_two_real_locations(tmp_path):
    data_dir = _install(tmp_path)
    _make_product(data_dir)
    _make_location(data_dir, id="wh_a", name="Warehouse A")
    _make_location(data_dir, id="wh_b", name="Warehouse B")
    _make_move(data_dir, id="m1", to_location_id="wh_a", quantity="10", reason="purchase")
    _make_move(data_dir, id="m2", from_location_id="wh_a", to_location_id="wh_b", quantity="6", reason="transfer")

    assert object_stock.quantity_at_location("p1", "wh_a", base_dir=data_dir, owner="u1") == Decimal("4")
    assert object_stock.quantity_at_location("p1", "wh_b", base_dir=data_dir, owner="u1") == Decimal("6")


# -- total_quantity: virtual-location exclusion ------------------------------

def test_total_quantity_excludes_virtual_customer_and_supplier_locations(tmp_path):
    """total_quantity is real on-hand only -- matches the source model's
    own total_quantity semantics: a sale to a virtual customer location
    reduces on-hand (it leaves the real warehouse) but the virtual
    location's own positive balance is never counted as "on hand."
    """
    data_dir = _install(tmp_path)
    _make_product(data_dir)
    _make_location(data_dir, id="wh", location_type="warehouse")
    _make_location(data_dir, id="cust", location_type="customer")
    _make_location(data_dir, id="supp", location_type="supplier")

    _make_move(data_dir, id="m1", from_location_id="supp", to_location_id="wh", quantity="20", reason="purchase")
    _make_move(data_dir, id="m2", from_location_id="wh", to_location_id="cust", quantity="8", reason="sale")

    # wh: +20 -8 = 12 (real, counted). cust: +8 (virtual, excluded).
    # supp: -20 (virtual, excluded) -- would otherwise cancel wh's +20.
    total = object_stock.total_quantity("p1", base_dir=data_dir, owner="u1")
    assert total == Decimal("12")


def test_total_quantity_excludes_plain_virtual_type_too(tmp_path):
    data_dir = _install(tmp_path)
    _make_product(data_dir)
    _make_location(data_dir, id="wh", location_type="warehouse")
    _make_location(data_dir, id="void", location_type="virtual")
    _make_move(data_dir, id="m1", to_location_id="wh", quantity="5", reason="purchase")
    _make_move(data_dir, id="m2", from_location_id="wh", to_location_id="void", quantity="2", reason="adjustment")

    assert object_stock.total_quantity("p1", base_dir=data_dir, owner="u1") == Decimal("3")


def test_virtual_location_ids_resolves_by_type(tmp_path):
    data_dir = _install(tmp_path)
    _make_location(data_dir, id="wh", location_type="warehouse")
    _make_location(data_dir, id="cust", location_type="customer")
    _make_location(data_dir, id="supp", location_type="supplier")
    _make_location(data_dir, id="void", location_type="virtual")

    virtual_ids = object_stock.virtual_location_ids(base_dir=data_dir, owner="u1")
    assert virtual_ids == {"cust", "supp", "void"}


# -- Decimal precision for fractional quantities -----------------------------

def test_fractional_quantity_folds_exactly_via_decimal(tmp_path):
    """2.5 - 0.7 must be exactly 1.8, not a binary-float artifact like
    1.7999999999999998 -- Decimal arithmetic throughout, never a bare
    float, same discipline app-orders' order_totals.py documents.
    """
    data_dir = _install(tmp_path)
    _make_product(data_dir)
    _make_location(data_dir, id="wh")
    _make_move(data_dir, id="m1", to_location_id="wh", quantity="2.5", reason="purchase")
    _make_move(data_dir, id="m2", from_location_id="wh", quantity="0.7", reason="sale")

    qty = object_stock.quantity_at_location("p1", "wh", base_dir=data_dir, owner="u1")
    assert qty == Decimal("1.8")
    assert str(qty) == "1.8"


# -- Oversold / negative levels: documented choice, no floor ----------------

def test_overselling_a_location_goes_negative_rather_than_floored_at_zero(tmp_path):
    """object_stock.py's documented choice (the source audit does not
    spell out clamp-vs-negative behavior): no floor is applied anywhere
    in the fold. Shipping out more than was ever received folds to a
    visible negative Decimal, not zero -- an oversold location is a real
    data-quality signal this module does not hide.
    """
    data_dir = _install(tmp_path)
    _make_product(data_dir)
    _make_location(data_dir, id="wh")
    _make_move(data_dir, id="m1", to_location_id="wh", quantity="5", reason="purchase")
    _make_move(data_dir, id="m2", from_location_id="wh", quantity="9", reason="sale")

    qty = object_stock.quantity_at_location("p1", "wh", base_dir=data_dir, owner="u1")
    assert qty == Decimal("-4")

    total = object_stock.total_quantity("p1", base_dir=data_dir, owner="u1")
    assert total == Decimal("-4")


# -- Owner scoping ------------------------------------------------------------

def test_moves_are_scoped_to_the_requesting_owner(tmp_path):
    """A second owner's stock_moves rows must never leak into another
    owner's derived levels, matching permissions/rules.json's row_filter
    owner_id == $user_id on both locations and stock_moves.
    """
    data_dir = _install(tmp_path)
    _make_product(data_dir, id="p1", owner_id="u1")
    _make_location(data_dir, id="wh", owner_id="u1")
    _make_move(data_dir, id="m1", to_location_id="wh", quantity="10", owner_id="u1")
    # A different owner's move against the same product/location ids.
    _make_move(data_dir, id="m2", to_location_id="wh", quantity="99", owner_id="u2")

    assert object_stock.quantity_at_location("p1", "wh", base_dir=data_dir, owner="u1") == Decimal("10")
    assert object_stock.quantity_at_location("p1", "wh", base_dir=data_dir, owner="u2") == Decimal("99")

    unscoped = object_stock.quantity_at_location("p1", "wh", base_dir=data_dir, owner=None)
    assert unscoped == Decimal("109")


# -- stock_levels: the page-ready summary ------------------------------------

def test_stock_levels_summary_shape(tmp_path):
    data_dir = _install(tmp_path)
    _make_product(data_dir, id="p1")
    _make_location(data_dir, id="wh", location_type="warehouse")
    _make_location(data_dir, id="cust", location_type="customer")
    _make_move(data_dir, id="m1", to_location_id="wh", quantity="10", reason="purchase")
    _make_move(data_dir, id="m2", from_location_id="wh", to_location_id="cust", quantity="3", reason="sale")

    summary = object_stock.stock_levels(base_dir=data_dir, owner="u1")

    levels_by_key = {(row["product_id"], row["location_id"]): row["quantity"] for row in summary["levels"]}
    assert levels_by_key == {("p1", "wh"): "7", ("p1", "cust"): "3"}

    totals_by_product = {row["product_id"]: row["quantity"] for row in summary["totals"]}
    # cust is virtual -- excluded from the on-hand total.
    assert totals_by_product == {"p1": "7"}


def test_stock_levels_is_empty_when_no_moves_exist(tmp_path):
    data_dir = _install(tmp_path)
    _make_product(data_dir)

    summary = object_stock.stock_levels(base_dir=data_dir, owner="u1")
    assert summary == {"levels": [], "totals": []}


def test_quantities_are_returned_as_exact_strings_not_floats(tmp_path):
    data_dir = _install(tmp_path)
    _make_product(data_dir)
    _make_location(data_dir, id="wh")
    _make_move(data_dir, id="m1", to_location_id="wh", quantity="2.5", reason="purchase")

    summary = object_stock.stock_levels(base_dir=data_dir, owner="u1")
    row = summary["levels"][0]
    assert row["quantity"] == "2.5"
    assert isinstance(row["quantity"], str)
