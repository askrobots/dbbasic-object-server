"""Structural tests for packages/app-articles after the Stage-6 retrofit: the
bespoke ~150-line site_article_view.py permalink page was replaced by a
seeded 59 detail view (site_view_render) whose `detail` block is
owner-aware (editable/deletable), so there is no per-article page object
anymore. The one-click Publish/Unpublish toggle collapses into the owner
editing `is_public` (and `published_on`) through the generator's ordinary
edit form -- same fields, one fewer bespoke action, matching how notes'
share-toggle became an is_public edit.
"""

import json
from pathlib import Path

import object_packages
import object_permissions

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_ARTICLES_DIR = PACKAGES_ROOT / "app-articles"


def _seed_rows(name):
    import csv

    with open(APP_ARTICLES_DIR / "seed" / f"{name}.tsv", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def test_manifest_drops_the_bespoke_view_object_and_seeds_the_detail_view():
    package = object_packages.get_package("app-articles", root=PACKAGES_ROOT)
    # Only the list page remains an object -- the permalink page is gone.
    assert {obj["id"] for obj in package["objects"]} == {"site_articles"}
    # The detail view + its route are seeded (into app-views'/site-routing's
    # own shared collections), alongside the articles seed.
    assert {entry["collection"] for entry in package["seed"]} == {
        "articles", "views", "site_routes",
    }


def test_article_view_object_file_is_deleted():
    assert not (APP_ARTICLES_DIR / "objects" / "site" / "article_view.py").exists()


def test_seeded_detail_view_uses_an_owner_aware_editable_detail_block():
    rows = _seed_rows("views")
    assert len(rows) == 1
    view = rows[0]
    assert view["id"] == "view_articles_detail"
    assert view["route"] == "/articles/{article_id:uuid}"
    blocks = json.loads(view["blocks"])
    assert len(blocks) == 1
    block = blocks[0]
    assert block["kind"] == "detail"
    assert block["collection"] == "articles"
    assert block["record_id"] == "$record_id"
    # The retrofit's whole point: read-only for everyone, owner-editable +
    # deletable in place -- preserving what the bespoke page did (minus the
    # dedicated Publish button, which becomes an is_public/published_on edit).
    assert block["editable"] is True
    assert block["deletable"] is True
    assert block["delete_redirect"] == "/articles"


def test_permalink_route_is_seeded_to_the_view_render_generator():
    rows = _seed_rows("site_routes")
    assert len(rows) == 1
    route = rows[0]
    assert route["pattern"] == "/articles/{article_id:uuid}"
    assert route["object_id"] == "site_view_render"


def test_permissions_no_longer_reference_the_removed_object():
    payload = json.loads((APP_ARTICLES_DIR / "permissions" / "rules.json").read_text())
    object_ids = {rule.get("object_id") for rule in payload["rules"]}
    assert "site_article_view" not in object_ids
    assert "site_articles" in object_ids  # the list page is still a public object


def test_dry_run_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()
    plan = object_packages.dry_run_package(
        "app-articles",
        root=PACKAGES_ROOT,
        base_dir=tmp_path / "data",
        object_roots=[object_root],
    )
    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []
