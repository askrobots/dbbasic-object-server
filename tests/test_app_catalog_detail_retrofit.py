"""Structural tests for packages/app-catalog after the Stage-6 retrofit: the
bespoke ~165-line site_product_view.py permalink page was replaced by a
seeded 59 detail view (site_view_render) whose `detail` block is
owner-aware (editable/deletable), so there is no per-product page object
anymore. Products have no public read path -- the row-filtered products
collection has no public read rule -- so this page only ever served (and
still only ever serves) the owner; non-owners and anonymous visitors are
permission-gated at the collection API the same as before.
"""

import json
from pathlib import Path

import object_packages
import object_permissions

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_CATALOG_DIR = PACKAGES_ROOT / "app-catalog"


def _seed_rows(name):
    import csv

    with open(APP_CATALOG_DIR / "seed" / f"{name}.tsv", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def test_manifest_drops_the_bespoke_view_object_and_seeds_the_detail_view():
    package = object_packages.get_package("app-catalog", root=PACKAGES_ROOT)
    # The permalink page object is gone; the other three pages remain.
    assert {obj["id"] for obj in package["objects"]} == {
        "site_products", "site_locations", "site_stock",
    }
    # The detail view + its route are seeded (into app-views'/site-routing's
    # own shared collections), alongside the existing products/locations/
    # stock_moves seeds.
    assert {entry["collection"] for entry in package["seed"]} == {
        "products", "locations", "stock_moves", "views", "site_routes",
    }


def test_product_view_object_file_is_deleted():
    assert not (APP_CATALOG_DIR / "objects" / "site" / "product_view.py").exists()


def test_seeded_detail_view_uses_an_owner_aware_editable_detail_block():
    rows = _seed_rows("views")
    assert len(rows) == 1
    view = rows[0]
    assert view["id"] == "view_products_detail"
    assert view["route"] == "/products/{product_id:uuid}"
    blocks = json.loads(view["blocks"])
    assert len(blocks) == 1
    block = blocks[0]
    assert block["kind"] == "detail"
    assert block["collection"] == "products"
    assert block["record_id"] == "$record_id"
    # The retrofit's whole point: owner-editable + deletable in place --
    # preserving what the bespoke page did. Non-owners never reach the
    # record at all (no public read rule on products), so the detail
    # block's read-only view is moot for them; the owner affordances are
    # what this page actually existed for.
    assert block["editable"] is True
    assert block["deletable"] is True
    assert block["delete_redirect"] == "/products"


def test_permalink_route_is_seeded_to_the_view_render_generator():
    rows = _seed_rows("site_routes")
    assert len(rows) == 1
    route = rows[0]
    assert route["id"] == "route_products_detail"
    assert route["pattern"] == "/products/{product_id:uuid}"
    assert route["object_id"] == "site_view_render"
    assert route["priority"] == "10"


def test_permissions_no_longer_reference_the_removed_object():
    payload = json.loads((APP_CATALOG_DIR / "permissions" / "rules.json").read_text())
    object_ids = {rule.get("object_id") for rule in payload["rules"]}
    assert "site_product_view" not in object_ids
    # The other page objects (list pages) are untouched.
    assert "site_products" in object_ids
    assert "site_locations" in object_ids
    assert "site_stock" in object_ids


def test_no_public_read_rule_was_added_to_products():
    """The retrofit must not loosen the owner-only posture: products still
    has no public read rule -- the seeded view's detail block is
    read-only-for-everyone in principle, but nobody except the owner can
    actually fetch the record.
    """
    payload = json.loads((APP_CATALOG_DIR / "permissions" / "rules.json").read_text())
    policy = object_permissions.policy_from_dict(
        {"access_mode": "role_based", "rules": payload["rules"]}
    )
    record = {"owner_id": "7", "name": "Widget", "product_type": "physical"}
    decision = object_permissions.check_permission(
        None, object_permissions.READ, policy=policy, collection="products", record=record
    )
    assert decision.allowed is False


def test_dry_run_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()
    plan = object_packages.dry_run_package(
        "app-catalog",
        root=PACKAGES_ROOT,
        base_dir=tmp_path / "data",
        object_roots=[object_root],
    )
    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []
