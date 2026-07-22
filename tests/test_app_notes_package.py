"""Structural tests for packages/app-notes after the Stage-6 retrofit: the
bespoke 152-line site_note_view.py permalink page was replaced by a seeded
59 detail view (site_view_render) whose `detail` block is owner-aware
(editable/deletable), so there is no per-note page object anymore. This also
fixes a latent gap -- the package previously shipped no site_routes record
for /notes/{note_id}, so permalinks depended on a route nothing seeded.
"""

import json
from pathlib import Path

import object_packages
import object_permissions

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_NOTES_DIR = PACKAGES_ROOT / "app-notes"


def _seed_rows(name):
    import csv

    with open(APP_NOTES_DIR / "seed" / f"{name}.tsv", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def test_manifest_drops_the_bespoke_view_object_and_seeds_the_detail_view():
    package = object_packages.get_package("app-notes", root=PACKAGES_ROOT)
    # Only the list page remains an object -- the permalink page is gone.
    assert {obj["id"] for obj in package["objects"]} == {"site_notes"}
    # The detail view + its route are seeded (into app-views'/site-routing's
    # own shared collections), alongside the notes seed.
    assert {entry["collection"] for entry in package["seed"]} == {
        "notes", "views", "site_routes",
    }


def test_note_view_object_file_is_deleted():
    assert not (APP_NOTES_DIR / "objects" / "site" / "note_view.py").exists()


def test_seeded_detail_view_uses_an_owner_aware_editable_detail_block():
    rows = _seed_rows("views")
    assert len(rows) == 1
    view = rows[0]
    assert view["id"] == "view_notes_detail"
    assert view["route"] == "/notes/{note_id}"
    blocks = json.loads(view["blocks"])
    assert len(blocks) == 1
    block = blocks[0]
    assert block["kind"] == "detail"
    assert block["collection"] == "notes"
    assert block["record_id"] == "$record_id"
    # The retrofit's whole point: read-only for everyone, owner-editable +
    # deletable in place -- preserving what the bespoke page did.
    assert block["editable"] is True
    assert block["deletable"] is True
    assert block["delete_redirect"] == "/notes"


def test_permalink_route_is_seeded_to_the_view_render_generator():
    rows = _seed_rows("site_routes")
    assert len(rows) == 1
    route = rows[0]
    assert route["pattern"] == "/notes/{note_id}"
    assert route["object_id"] == "site_view_render"


def test_permissions_no_longer_reference_the_removed_object():
    payload = json.loads((APP_NOTES_DIR / "permissions" / "rules.json").read_text())
    object_ids = {rule.get("object_id") for rule in payload["rules"]}
    assert "site_note_view" not in object_ids
    assert "site_notes" in object_ids  # the list page is still a public object


def test_dry_run_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()
    plan = object_packages.dry_run_package(
        "app-notes",
        root=PACKAGES_ROOT,
        base_dir=tmp_path / "data",
        object_roots=[object_root],
    )
    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []
