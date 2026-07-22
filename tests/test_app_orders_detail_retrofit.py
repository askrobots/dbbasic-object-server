"""Stage-6 retrofit of packages/app-orders: the bespoke site_order_view.py
page is replaced by a seeded 59 detail view composed of three GENERIC block
renderers -- an editable+deletable `detail` block (the order; money renders
via /form's cents formatting, no custom JS, and status transitions become
status-field edits in the owner's edit form, guarded by the schema's own
10-flow transitions -- not one-click buttons, the same make-it-basic trade
app-forum's pin/lock takes), a `related` block (order_lines flat by
order_id -- order_lines stays a RELATIONAL collection, not embedded, so a
line remains individually addressable), and a `form` block with a FK
locked to the page's order (Add Line). Owner-only throughout: the
row-filtered collection API only ever returns real data to the order's
owner, same as the page it replaces.
"""

import json
from pathlib import Path

import object_packages
import object_permissions

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_ORDERS_DIR = PACKAGES_ROOT / "app-orders"


def _seed_rows(name):
    import csv

    with open(APP_ORDERS_DIR / "seed" / f"{name}.tsv", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def test_manifest_drops_bespoke_order_view_page_and_seeds_the_detail_view():
    package = object_packages.get_package("app-orders", root=PACKAGES_ROOT)
    assert {obj["id"] for obj in package["objects"]} == {
        "site_orders", "system_order_totals",
    }
    assert {entry["collection"] for entry in package["seed"]} == {
        "orders", "order_lines", "views", "site_routes",
    }
    assert {dep["id"] for dep in package["dependencies"]} == {"app-views"}


def test_order_view_object_file_is_deleted():
    assert not (APP_ORDERS_DIR / "objects" / "site" / "order_view.py").exists()


def test_order_view_composes_detail_related_and_fk_locked_form():
    rows = _seed_rows("views")
    assert len(rows) == 1
    view = rows[0]
    assert view["id"] == "view_order_detail"
    assert view["route"] == "/orders/{order_id}"
    blocks = json.loads(view["blocks"])
    kinds = [b["kind"] for b in blocks]
    assert kinds == ["detail", "related", "form"]

    detail, related, form = blocks

    # 1. editable+deletable order detail
    assert detail["collection"] == "orders"
    assert detail["record_id"] == "$record_id"
    assert detail["editable"] is True and detail["deletable"] is True
    assert detail["delete_redirect"] == "/orders"

    # 2. FLAT order_lines (a plain related list by order_id -- relational,
    # not embedded, so a line stays individually addressable)
    assert related["collection"] == "order_lines"
    assert related["fk_field"] == "order_id"
    assert related["match"] == "$record_id"
    assert related["title"] == "Line Items"

    # 3. add-line form with order_id locked to the page's order
    assert form["collection"] == "order_lines"
    assert form["fixed"] == {"order_id": "$record_id"}
    assert form["title"] == "Add Line"


def test_permalink_route_points_to_view_render():
    rows = _seed_rows("site_routes")
    assert len(rows) == 1
    assert rows[0]["pattern"] == "/orders/{order_id}"
    assert rows[0]["object_id"] == "site_view_render"


def test_permissions_no_longer_reference_the_removed_object():
    payload = json.loads((APP_ORDERS_DIR / "permissions" / "rules.json").read_text())
    object_ids = {rule.get("object_id") for rule in payload["rules"]}
    assert "site_order_view" not in object_ids
    assert "site_orders" in object_ids


def test_no_public_read_survives_the_retrofit():
    """Orders and order_lines stay owner-private: the retrofit must not
    have added a public read rule on either collection, and the removed
    object's public-execute rule must be gone too (already covered
    above)."""
    payload = json.loads((APP_ORDERS_DIR / "permissions" / "rules.json").read_text())
    policy = object_permissions.policy_from_dict(
        {"access_mode": "role_based", "rules": payload["rules"]}
    )
    for collection in ("orders", "order_lines"):
        decision = object_permissions.check_permission(
            None, object_permissions.READ, policy=policy, collection=collection,
            record={"owner_id": "7"},
        )
        assert decision.allowed is False, f"anonymous read should be denied on {collection}"


def test_dry_run_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()
    plan = object_packages.dry_run_package(
        "app-orders", root=PACKAGES_ROOT, base_dir=tmp_path / "data", object_roots=[object_root],
    )
    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []
