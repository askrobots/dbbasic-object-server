"""Stage-6 retrofit of packages/app-invoices: the bespoke
site_invoice_view.py page is replaced by a seeded 59 detail view composed
of three GENERIC block renderers -- an editable+deletable `detail` block
(the invoice; the status field's own 10-flow transition guards enforce
legal moves through the owner edit form, so no one-click Send/Void
buttons are built, the same make-it-basic trade app-forum's pin/lock
retrofit made), a `related` block (invoice_lines by invoice_id -- lines
stay their own relational collection, never embedded, because they are
the thing receipts/notes attach to later), and a `form` block with a FK
locked to the page's invoice (Add Line). No hand-written money-totals
table either: the detail block's own /form cents formatting renders
subtotal/tax/total/paid/balance correctly.
"""

import json
from pathlib import Path

import object_packages
import object_permissions

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_INVOICES_DIR = PACKAGES_ROOT / "app-invoices"


def _seed_rows(name):
    import csv

    with open(APP_INVOICES_DIR / "seed" / f"{name}.tsv", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def test_manifest_drops_bespoke_invoice_view_page_and_seeds_the_detail_view():
    package = object_packages.get_package("app-invoices", root=PACKAGES_ROOT)
    assert {obj["id"] for obj in package["objects"]} == {
        "site_invoices", "system_invoice_totals",
    }
    assert {entry["collection"] for entry in package["seed"]} == {
        "invoices", "invoice_lines", "views", "site_routes",
    }
    assert {dep["id"] for dep in package["dependencies"]} == {"app-views"}


def test_invoice_view_object_file_is_deleted():
    assert not (APP_INVOICES_DIR / "objects" / "site" / "invoice_view.py").exists()


def test_invoice_view_composes_detail_flat_related_and_fk_locked_form():
    rows = _seed_rows("views")
    assert len(rows) == 1
    view = rows[0]
    assert view["id"] == "view_invoice_detail"
    assert view["route"] == "/invoices/{invoice_id}"
    assert view["layout"] == "single"
    blocks = json.loads(view["blocks"])
    kinds = [b["kind"] for b in blocks]
    assert kinds == ["detail", "related", "form"]

    detail, related, form = blocks

    # 1. editable+deletable invoice detail
    assert detail["collection"] == "invoices"
    assert detail["record_id"] == "$record_id"
    assert detail["editable"] is True and detail["deletable"] is True
    assert detail["delete_redirect"] == "/invoices"

    # 2. FLAT invoice_lines (a plain related list by invoice_id)
    assert related["collection"] == "invoice_lines"
    assert related["fk_field"] == "invoice_id"
    assert related["match"] == "$record_id"
    assert related["title"] == "Line Items"

    # 3. add-line form with invoice_id locked to the page's invoice
    assert form["collection"] == "invoice_lines"
    assert form["fixed"] == {"invoice_id": "$record_id"}
    assert form["title"] == "Add Line"


def test_permalink_route_points_to_view_render():
    rows = _seed_rows("site_routes")
    assert len(rows) == 1
    assert rows[0]["id"] == "route_invoice_detail"
    assert rows[0]["pattern"] == "/invoices/{invoice_id}"
    assert rows[0]["object_id"] == "site_view_render"
    assert rows[0]["priority"] == "10"


def test_permissions_no_longer_reference_the_removed_object():
    payload = json.loads((APP_INVOICES_DIR / "permissions" / "rules.json").read_text())
    object_ids = {rule.get("object_id") for rule in payload["rules"]}
    assert "site_invoice_view" not in object_ids
    assert "site_invoices" in object_ids


def test_no_public_read_survives_the_retrofit():
    """Invoices stay owner-private: the retrofit must not have added a
    public read rule on either collection, and the removed object's
    public-execute rule must be gone too (already covered above)."""
    payload = json.loads((APP_INVOICES_DIR / "permissions" / "rules.json").read_text())
    policy = object_permissions.policy_from_dict(
        {"access_mode": "role_based", "rules": payload["rules"]}
    )
    for collection in ("invoices", "invoice_lines"):
        decision = object_permissions.check_permission(
            None, object_permissions.READ, policy=policy, collection=collection,
            record={"owner_id": "7"},
        )
        assert decision.allowed is False, f"anonymous read should be denied on {collection}"


def test_dry_run_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()
    plan = object_packages.dry_run_package(
        "app-invoices", root=PACKAGES_ROOT, base_dir=tmp_path / "data", object_roots=[object_root],
    )
    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []
