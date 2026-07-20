"""Structural tests for packages/app-forum (forum_categories, forum_topics,
forum_replies).

Mirrors the package/schema/permission testing conventions used for
packages/app-invoices in tests/test_app_invoices_package.py. app-forum has
no HANDLES handler and no behavior module, so this is the whole suite.
"""

import json
import re
from pathlib import Path

import object_packages
import object_permissions
import object_schemas

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_FORUM_DIR = PACKAGES_ROOT / "app-forum"


def _categories_schema():
    return json.loads((APP_FORUM_DIR / "schemas" / "forum_categories.json").read_text())


def _topics_schema():
    return json.loads((APP_FORUM_DIR / "schemas" / "forum_topics.json").read_text())


def _replies_schema():
    return json.loads((APP_FORUM_DIR / "schemas" / "forum_replies.json").read_text())


def test_get_package_normalizes_app_forum_manifest():
    package = object_packages.get_package("app-forum", root=PACKAGES_ROOT)

    assert package["id"] == "app-forum"
    assert package["name"] == "Forum"
    assert {schema["collection"] for schema in package["schemas"]} == {
        "forum_categories",
        "forum_topics",
        "forum_replies",
    }
    assert {obj["id"] for obj in package["objects"]} == {
        "site_forum",
        "site_forum_topic",
    }
    assert package["permissions"] == [{"path": "permissions/rules.json"}]
    assert {entry["collection"] for entry in package["seed"]} == {
        "forum_categories",
        "forum_topics",
        "forum_replies",
    }


def test_dry_run_app_forum_package_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()

    plan = object_packages.dry_run_package(
        "app-forum",
        root=PACKAGES_ROOT,
        base_dir=tmp_path / "data",
        object_roots=[object_root],
    )

    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []
    assert {schema["collection"] for schema in plan["schemas"]} == {
        "forum_categories",
        "forum_topics",
        "forum_replies",
    }


def test_install_app_forum_package_loads_schemas(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()

    object_packages.install_package(
        "app-forum",
        root=PACKAGES_ROOT,
        base_dir=data_dir,
        object_roots=[object_root],
    )

    categories_schema = object_schemas.get_schema("forum_categories", base_dir=data_dir)
    topics_schema = object_schemas.get_schema("forum_topics", base_dir=data_dir)
    replies_schema = object_schemas.get_schema("forum_replies", base_dir=data_dir)

    assert categories_schema["name"] == "forum_categories"
    assert topics_schema["name"] == "forum_topics"
    assert replies_schema["name"] == "forum_replies"
    assert (object_root / "site" / "forum.py").is_file()
    assert (object_root / "site" / "forum_topic.py").is_file()


def test_schema_json_files_are_valid_and_versioned():
    for name in ("forum_categories", "forum_topics", "forum_replies"):
        payload = json.loads((APP_FORUM_DIR / "schemas" / f"{name}.json").read_text())
        assert payload["name"] == name
        assert payload["version"] == 1


def test_three_collections_present_with_expected_field_names():
    cat_fields = [f["name"] for f in _categories_schema()["fields"]]
    assert cat_fields == [
        "id", "name", "slug", "description", "icon",
        "display_order", "is_active", "owner_id", "created_at",
    ]

    topic_fields = [f["name"] for f in _topics_schema()["fields"]]
    assert topic_fields == [
        "id", "category_id", "project_id", "title", "content", "views",
        "is_pinned", "is_locked", "is_solved", "ai_summary",
        "is_ai_summarized", "owner_id", "created_at",
    ]

    reply_fields = [f["name"] for f in _replies_schema()["fields"]]
    assert reply_fields == [
        "id", "topic_id", "parent_id", "content", "is_solution",
        "owner_id", "created_at",
    ]


def test_topic_category_relation_is_required():
    by_name = {f["name"]: f for f in _topics_schema()["fields"]}
    assert by_name["category_id"]["relation"]["collection"] == "forum_categories"
    assert by_name["category_id"]["required"] is True


def test_topic_project_id_is_a_plain_optional_text_field():
    """The source's optional project link, carried but not wired to
    app-projects as a relation -- app-forum has no dependency on it.
    """
    by_name = {f["name"]: f for f in _topics_schema()["fields"]}
    assert by_name["project_id"]["type"] == "text"
    assert "relation" not in by_name["project_id"]
    assert not by_name["project_id"].get("required")


def test_reply_parent_is_a_self_referencing_relation():
    by_name = {f["name"]: f for f in _replies_schema()["fields"]}
    assert by_name["parent_id"]["relation"]["collection"] == "forum_replies"
    assert not by_name["parent_id"].get("required")

    by_name_topic = {f["name"]: f for f in _replies_schema()["fields"]}
    assert by_name_topic["topic_id"]["relation"]["collection"] == "forum_topics"
    assert by_name_topic["topic_id"]["required"] is True


def test_reply_storage_is_explicitly_classic():
    assert _replies_schema()["storage"] == "classic"


def test_moderation_and_carried_flags_are_booleans_with_expected_defaults():
    by_name = {f["name"]: f for f in _topics_schema()["fields"]}
    for name in ("is_pinned", "is_locked", "is_solved", "is_ai_summarized"):
        assert by_name[name]["type"] == "boolean"
        assert by_name[name]["default"] == "false"

    cat_by_name = {f["name"]: f for f in _categories_schema()["fields"]}
    assert cat_by_name["is_active"]["type"] == "boolean"
    assert cat_by_name["is_active"]["default"] == "true"

    reply_by_name = {f["name"]: f for f in _replies_schema()["fields"]}
    assert reply_by_name["is_solution"]["type"] == "boolean"
    assert reply_by_name["is_solution"]["default"] == "false"


def test_carried_but_undriven_fields_are_present():
    """ai_summary/ai_summarized and views are carried from the source model
    with no generation or increment logic behind them -- see
    dbbasic-package.json's deferred list.
    """
    by_name = {f["name"]: f for f in _topics_schema()["fields"]}
    assert by_name["ai_summary"]["type"] == "textarea"
    assert by_name["views"]["type"] == "number"
    assert by_name["views"]["default"] == "0"


def test_display_order_and_default_field_types():
    cat_by_name = {f["name"]: f for f in _categories_schema()["fields"]}
    assert cat_by_name["display_order"]["type"] == "number"
    assert cat_by_name["display_order"]["default"] == "0"


def test_search_fields_cover_the_content_fields():
    assert _categories_schema()["search"]["fields"] == ["name", "description"]
    assert _topics_schema()["search"]["fields"] == ["title", "content"]
    assert _replies_schema()["search"]["fields"] == ["content"]


def _app_forum_policy():
    payload = json.loads((APP_FORUM_DIR / "permissions" / "rules.json").read_text())
    return object_permissions.policy_from_dict({"access_mode": "role_based", "rules": payload["rules"]})


def test_anonymous_can_read_any_category_topic_or_reply():
    """Public read on all three collections, no row_filter -- the whole
    forum is visible to anonymous visitors.
    """
    policy = _app_forum_policy()
    someone_elses_category = {"owner_id": "7", "name": "General"}
    someone_elses_topic = {"owner_id": "7", "category_id": "cat_1", "title": "Hello"}
    someone_elses_reply = {"owner_id": "7", "topic_id": "topic_1", "content": "Hi"}

    for collection, record in (
        ("forum_categories", someone_elses_category),
        ("forum_topics", someone_elses_topic),
        ("forum_replies", someone_elses_reply),
    ):
        decision = object_permissions.check_permission(
            None, object_permissions.READ, policy=policy, collection=collection, record=record
        )
        assert decision.allowed is True, f"anonymous read should be allowed on {collection}"


def test_owner_can_crud_own_category_topic_and_reply():
    policy = _app_forum_policy()
    subject = object_permissions.PermissionSubject(user_id="7")
    records = {
        "forum_categories": {"owner_id": "7", "name": "General"},
        "forum_topics": {"owner_id": "7", "category_id": "cat_1", "title": "Hello"},
        "forum_replies": {"owner_id": "7", "topic_id": "topic_1", "content": "Hi"},
    }

    for collection, record in records.items():
        for action in (
            object_permissions.CREATE,
            object_permissions.READ,
            object_permissions.UPDATE,
            object_permissions.DELETE,
        ):
            decision = object_permissions.check_permission(
                subject, action, policy=policy, collection=collection, record=record
            )
            assert decision.allowed is True, f"owner {action} should be allowed on {collection}"


def test_others_cannot_write_someone_elses_category_topic_or_reply():
    """Public read still applies (tested above); this checks the
    owner-only write side: a different signed-in user cannot update or
    delete a record they do not own -- the v1 "no moderator role" posture
    documented in dbbasic-package.json and permissions/rules.json.
    """
    policy = _app_forum_policy()
    subject = object_permissions.PermissionSubject(user_id="8")
    records = {
        "forum_categories": {"owner_id": "7", "name": "General"},
        "forum_topics": {"owner_id": "7", "category_id": "cat_1", "title": "Hello"},
        "forum_replies": {"owner_id": "7", "topic_id": "topic_1", "content": "Hi"},
    }

    for collection, record in records.items():
        for action in (object_permissions.UPDATE, object_permissions.DELETE):
            decision = object_permissions.check_permission(
                subject, action, policy=policy, collection=collection, record=record
            )
            assert decision.allowed is False, f"non-owner {action} should be denied on {collection}"


def test_forum_pages_are_publicly_executable():
    policy = _app_forum_policy()

    for object_id in ("site_forum", "site_forum_topic"):
        decision = object_permissions.check_permission(
            None, object_permissions.EXECUTE, policy=policy, object_id=object_id
        )
        assert decision.allowed is True


def test_seed_tsvs_have_no_data_rows_and_match_schema_field_order():
    """Header-only seeds, matching the established precedent (app-tasks,
    app-notes, app-invoices all ship header-only seeds).
    """
    for name, schema in (
        ("forum_categories", _categories_schema()),
        ("forum_topics", _topics_schema()),
        ("forum_replies", _replies_schema()),
    ):
        path = APP_FORUM_DIR / "seed" / f"{name}.tsv"
        lines = path.read_text().splitlines()
        assert len(lines) == 1, f"{name}.tsv should be header-only"
        header = lines[0].split("\t")
        assert header == [f["name"] for f in schema["fields"]]


def test_no_disallowed_org_names_leak_into_the_package():
    """Public repo hygiene: no internal org/codename references anywhere
    in this package's source. Ported from a private predecessor-system
    audit, not part of this repo.
    """
    # Built from fragments so this guard file itself stays clean of the
    # very internal names it forbids.
    banned = re.compile(
        "|".join([r"\b" + "q" + "9" + r"\b", "ask" + "robots", r"\b" + "wo" + "ld" + r"\b"]),
        re.IGNORECASE,
    )
    for path in APP_FORUM_DIR.rglob("*"):
        if path.is_dir():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not banned.search(text), f"disallowed reference found in {path}"
