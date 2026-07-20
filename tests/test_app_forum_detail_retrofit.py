"""Stage-6 retrofit of packages/app-forum: the 338-line bespoke
site_forum_topic.py page is replaced by a seeded 59 detail view composed of
three GENERIC block renderers -- an editable+deletable `detail` block (the
topic), a `related` block (FLAT replies by topic_id -- deliberately flat, not
nested), and a `form` block with a FK locked to the page's topic (reply
compose). Flat, because nested discussion buries new replies and orphans
subtrees on delete; a "poll" over flat replies is a rollup, not a new feature.
"""

import json
from pathlib import Path

import object_packages
import object_schemas

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_FORUM_DIR = PACKAGES_ROOT / "app-forum"


def _seed_rows(name):
    import csv

    with open(APP_FORUM_DIR / "seed" / f"{name}.tsv", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def test_manifest_drops_bespoke_topic_page_and_seeds_the_detail_view():
    package = object_packages.get_package("app-forum", root=PACKAGES_ROOT)
    assert {obj["id"] for obj in package["objects"]} == {"site_forum"}
    assert {entry["collection"] for entry in package["seed"]} == {
        "forum_categories", "forum_topics", "forum_replies", "views", "site_routes",
    }


def test_forum_topic_object_file_is_deleted():
    assert not (APP_FORUM_DIR / "objects" / "site" / "forum_topic.py").exists()


def test_topic_view_composes_detail_flat_related_and_fk_locked_form():
    rows = _seed_rows("views")
    assert len(rows) == 1
    view = rows[0]
    assert view["id"] == "view_forum_topic"
    assert view["route"] == "/forum/topics/{topic_id:uuid}"
    blocks = json.loads(view["blocks"])
    kinds = [b["kind"] for b in blocks]
    assert kinds == ["detail", "related", "form"]

    detail, related, form = blocks
    # 1. editable+deletable topic detail
    assert detail["collection"] == "forum_topics"
    assert detail["record_id"] == "$record_id"
    assert detail["editable"] is True and detail["deletable"] is True

    # 2. FLAT replies (a plain related list by topic_id -- no nesting)
    assert related["collection"] == "forum_replies"
    assert related["fk_field"] == "topic_id"
    assert related["match"] == "$record_id"

    # 3. reply compose with topic_id locked to the page's topic
    assert form["collection"] == "forum_replies"
    assert form["fixed"] == {"topic_id": "$record_id"}


def test_permalink_route_points_to_view_render():
    rows = _seed_rows("site_routes")
    assert len(rows) == 1
    assert rows[0]["pattern"] == "/forum/topics/{topic_id:uuid}"
    assert rows[0]["object_id"] == "site_view_render"


def test_ai_summary_is_conditionally_visible_via_visible_when():
    """The orphaned ai_summary field now shows on the topic detail, but only
    when is_ai_summarized -- via the field's own visible_when, not page code."""
    schema = json.loads((APP_FORUM_DIR / "schemas" / "forum_topics.json").read_text())
    ai = next(f for f in schema["fields"] if f["name"] == "ai_summary")
    assert ai["visible_when"] == {"field": "is_ai_summarized", "equals": "true"}


def test_reply_compose_form_is_flat_no_parent_id():
    """Flat replies: parent_id is dropped from the compose form's
    forms.default, so no one composes a nested reply. The field itself stays
    on the schema (existing data), it is just never offered."""
    schema = json.loads((APP_FORUM_DIR / "schemas" / "forum_replies.json").read_text())
    assert schema["forms"]["default"]["fields"] == ["topic_id", "content"]
    assert "parent_id" in {f["name"] for f in schema["fields"]}


def test_permissions_no_longer_reference_the_removed_object():
    payload = json.loads((APP_FORUM_DIR / "permissions" / "rules.json").read_text())
    object_ids = {rule.get("object_id") for rule in payload["rules"]}
    assert "site_forum_topic" not in object_ids
    assert "site_forum" in object_ids


def test_dry_run_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()
    plan = object_packages.dry_run_package(
        "app-forum", root=PACKAGES_ROOT, base_dir=tmp_path / "data", object_roots=[object_root],
    )
    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []


def test_visible_when_survives_install(tmp_path):
    """The condition must survive normalization/install -- else non-summarized
    topics would show a blank AI Summary row."""
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    object_packages.install_package(
        "app-forum", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root],
    )
    schema = object_schemas.get_schema("forum_topics", base_dir=data_dir)
    ai = next(f for f in schema["fields"] if f["name"] == "ai_summary")
    assert ai.get("visible_when") == {"field": "is_ai_summarized", "equals": "true"}
