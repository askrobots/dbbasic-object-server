"""Structural tests for packages/app-worker (profiles, follows,
profile_comments).

Mirrors the package/schema/permission testing conventions used for
packages/app-forum in tests/test_app_forum_package.py. app-worker has no
HANDLES handler and no behavior module, so this is the whole suite.
"""

import json
import re
from pathlib import Path

import object_packages
import object_permissions
import object_schemas

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_WORKER_DIR = PACKAGES_ROOT / "app-worker"


def _profiles_schema():
    return json.loads((APP_WORKER_DIR / "schemas" / "profiles.json").read_text())


def _follows_schema():
    return json.loads((APP_WORKER_DIR / "schemas" / "follows.json").read_text())


def _comments_schema():
    return json.loads((APP_WORKER_DIR / "schemas" / "profile_comments.json").read_text())


def test_get_package_normalizes_app_worker_manifest():
    package = object_packages.get_package("app-worker", root=PACKAGES_ROOT)

    assert package["id"] == "app-worker"
    assert package["name"] == "Worker"
    assert {schema["collection"] for schema in package["schemas"]} == {
        "profiles",
        "follows",
        "profile_comments",
    }
    assert {obj["id"] for obj in package["objects"]} == {
        "site_profile",
        "site_profile_edit",
    }
    assert package["permissions"] == [{"path": "permissions/rules.json"}]
    assert {entry["collection"] for entry in package["seed"]} == {
        "profiles",
        "follows",
        "profile_comments",
    }
    assert package["dependencies"] == []


def test_dry_run_app_worker_package_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()

    plan = object_packages.dry_run_package(
        "app-worker",
        root=PACKAGES_ROOT,
        base_dir=tmp_path / "data",
        object_roots=[object_root],
    )

    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []
    assert {schema["collection"] for schema in plan["schemas"]} == {
        "profiles",
        "follows",
        "profile_comments",
    }


def test_install_app_worker_package_loads_schemas(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()

    object_packages.install_package(
        "app-worker",
        root=PACKAGES_ROOT,
        base_dir=data_dir,
        object_roots=[object_root],
    )

    profiles_schema = object_schemas.get_schema("profiles", base_dir=data_dir)
    follows_schema = object_schemas.get_schema("follows", base_dir=data_dir)
    comments_schema = object_schemas.get_schema("profile_comments", base_dir=data_dir)

    assert profiles_schema["name"] == "profiles"
    assert follows_schema["name"] == "follows"
    assert comments_schema["name"] == "profile_comments"
    assert (object_root / "site" / "profile.py").is_file()
    assert (object_root / "site" / "profile_edit.py").is_file()


def test_schema_json_files_are_valid_and_versioned():
    for name in ("profiles", "follows", "profile_comments"):
        payload = json.loads((APP_WORKER_DIR / "schemas" / f"{name}.json").read_text())
        assert payload["name"] == name
        assert payload["version"] == 1


def test_three_collections_present_with_expected_field_names():
    profile_fields = [f["name"] for f in _profiles_schema()["fields"]]
    assert profile_fields == [
        "id", "display_name", "bio", "skills", "experience", "location",
        "education", "website", "social_links", "is_active", "owner_id",
        "created_at",
    ]

    follow_fields = [f["name"] for f in _follows_schema()["fields"]]
    assert follow_fields == [
        "id", "follower_id", "following_id", "created_at", "owner_id",
    ]

    comment_fields = [f["name"] for f in _comments_schema()["fields"]]
    assert comment_fields == [
        "id", "profile_id", "author_name", "author_email", "author_url",
        "content", "status", "ip_address", "created_at", "owner_id",
    ]


def test_skills_is_free_text_not_a_relation():
    """SCOPE RULE: the source model's skills field is free text, not a
    skills-matching taxonomy or relation. No skills collection, no
    availability field, exists anywhere in this package.
    """
    by_name = {f["name"]: f for f in _profiles_schema()["fields"]}
    assert by_name["skills"]["type"] == "text"
    assert "relation" not in by_name["skills"]

    field_names = {f["name"] for f in _profiles_schema()["fields"]}
    assert "availability" not in field_names
    assert "rating" not in field_names
    assert "reviews" not in field_names


def test_profiles_forms_and_views_match_the_brief():
    schema = _profiles_schema()
    assert schema["forms"]["default"]["fields"] == [
        "display_name", "bio", "skills", "experience", "location",
        "education", "website", "social_links", "is_active",
    ]
    assert schema["search"]["fields"] == ["display_name", "skills", "bio"]


def test_profiles_is_active_defaults_true():
    by_name = {f["name"]: f for f in _profiles_schema()["fields"]}
    assert by_name["is_active"]["type"] == "boolean"
    assert by_name["is_active"]["default"] == "true"


def test_follows_storage_is_append_and_edge_is_directed():
    schema = _follows_schema()
    assert schema["storage"] == "append"

    by_name = {f["name"]: f for f in schema["fields"]}
    assert by_name["follower_id"]["type"] == "text"
    assert by_name["follower_id"]["required"] is True
    assert by_name["following_id"]["type"] == "text"
    assert by_name["following_id"]["required"] is True


def test_follows_has_no_search_block():
    assert "search" not in _follows_schema()


def test_profile_comments_status_enum_and_defaults():
    by_name = {f["name"]: f for f in _comments_schema()["fields"]}
    status_field = by_name["status"]
    assert status_field["enum"] == ["pending", "approved", "rejected"]
    assert status_field["default"] == "pending"


def test_profile_comments_guarded_transitions_match_the_brief():
    by_name = {f["name"]: f for f in _comments_schema()["fields"]}
    transitions = by_name["status"]["transitions"]
    owner_guard = {"owner_id": "$user_id"}

    pending_targets = {entry["to"]: entry["when"] for entry in transitions["pending"]}
    assert pending_targets == {"approved": owner_guard, "rejected": owner_guard}

    approved_targets = {entry["to"]: entry["when"] for entry in transitions["approved"]}
    assert approved_targets == {"rejected": owner_guard}

    # rejected is terminal: no entry in the transitions map at all.
    assert "rejected" not in transitions


def test_profile_comments_hidden_fields():
    by_name = {f["name"]: f for f in _comments_schema()["fields"]}
    assert by_name["author_email"]["permissions"] == {"public": "hidden"}
    assert by_name["ip_address"]["permissions"] == {"public": "hidden"}
    assert by_name["status"]["permissions"] == {"public": "hidden"}


def test_profile_comments_content_and_profile_id_required():
    by_name = {f["name"]: f for f in _comments_schema()["fields"]}
    assert by_name["profile_id"]["required"] is True
    assert by_name["profile_id"]["relation"]["collection"] == "profiles"
    assert by_name["content"]["required"] is True


def test_profile_comments_has_no_search_block():
    assert "search" not in _comments_schema()


def _app_worker_policy():
    payload = json.loads((APP_WORKER_DIR / "permissions" / "rules.json").read_text())
    return object_permissions.policy_from_dict({"access_mode": "role_based", "rules": payload["rules"]})


def test_anonymous_can_read_any_profile_and_follow_edge():
    policy = _app_worker_policy()
    someone_elses_profile = {"owner_id": "7", "display_name": "Ada"}
    someone_elses_follow = {"owner_id": "7", "follower_id": "7", "following_id": "8"}

    for collection, record in (
        ("profiles", someone_elses_profile),
        ("follows", someone_elses_follow),
    ):
        decision = object_permissions.check_permission(
            None, object_permissions.READ, policy=policy, collection=collection, record=record
        )
        assert decision.allowed is True, f"anonymous read should be allowed on {collection}"


def test_anonymous_can_read_only_approved_profile_comments():
    policy = _app_worker_policy()
    approved = {"owner_id": "9", "profile_id": "p1", "content": "hi", "status": "approved"}
    pending = {"owner_id": "9", "profile_id": "p1", "content": "hi", "status": "pending"}
    rejected = {"owner_id": "9", "profile_id": "p1", "content": "spam", "status": "rejected"}

    approved_decision = object_permissions.check_permission(
        None, object_permissions.READ, policy=policy, collection="profile_comments", record=approved
    )
    assert approved_decision.allowed is True

    for record in (pending, rejected):
        decision = object_permissions.check_permission(
            None, object_permissions.READ, policy=policy, collection="profile_comments", record=record
        )
        assert decision.allowed is False


def test_owner_can_crud_own_profile_follow_and_comment():
    policy = _app_worker_policy()
    subject = object_permissions.PermissionSubject(user_id="7")
    records = {
        "profiles": {"owner_id": "7", "display_name": "Ada"},
        "follows": {"owner_id": "7", "follower_id": "7", "following_id": "8"},
        "profile_comments": {"owner_id": "7", "profile_id": "p1", "content": "hi", "status": "pending"},
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


def test_others_cannot_write_someone_elses_profile_follow_or_comment():
    policy = _app_worker_policy()
    subject = object_permissions.PermissionSubject(user_id="8")
    records = {
        "profiles": {"owner_id": "7", "display_name": "Ada"},
        "follows": {"owner_id": "7", "follower_id": "7", "following_id": "9"},
        "profile_comments": {"owner_id": "7", "profile_id": "p1", "content": "hi", "status": "pending"},
    }

    for collection, record in records.items():
        for action in (object_permissions.UPDATE, object_permissions.DELETE):
            decision = object_permissions.check_permission(
                subject, action, policy=policy, collection=collection, record=record
            )
            assert decision.allowed is False, f"non-owner {action} should be denied on {collection}"


def test_anonymous_cannot_create_a_profile_comment():
    """v1 guestbook posting is signed-in only -- no public create rule is
    granted on profile_comments (see schemas/profile_comments.json and
    dbbasic-package.json's deferred list for the anonymous path).
    """
    policy = _app_worker_policy()
    record = {"profile_id": "p1", "content": "hi", "status": "pending"}

    decision = object_permissions.check_permission(
        None, object_permissions.CREATE, policy=policy, collection="profile_comments", record=record
    )
    assert decision.allowed is False


def test_worker_pages_are_publicly_executable():
    policy = _app_worker_policy()

    for object_id in ("site_profile", "site_profile_edit"):
        decision = object_permissions.check_permission(
            None, object_permissions.EXECUTE, policy=policy, object_id=object_id
        )
        assert decision.allowed is True


def test_seed_tsvs_have_no_data_rows_and_match_schema_field_order():
    """Header-only seeds, matching the established precedent (app-tasks,
    app-notes, app-invoices, app-forum all ship header-only seeds).
    """
    for name, schema in (
        ("profiles", _profiles_schema()),
        ("follows", _follows_schema()),
        ("profile_comments", _comments_schema()),
    ):
        path = APP_WORKER_DIR / "seed" / f"{name}.tsv"
        lines = path.read_text().splitlines()
        assert len(lines) == 1, f"{name}.tsv should be header-only"
        header = lines[0].split("\t")
        assert header == [f["name"] for f in schema["fields"]]


def test_no_disallowed_org_names_leak_into_the_package():
    """Public repo hygiene: no internal org/codename references anywhere
    in this package's source, including in any source-doc path reference
    (this package describes its source only as "a private
    predecessor-system audit, not part of this repo").
    """
    # Built from fragments so this guard file itself stays clean of the
    # very internal names it forbids.
    banned = re.compile(
        "|".join([r"\b" + "q" + "9" + r"\b", "ask" + "robots", r"\b" + "wo" + "ld" + r"\b"]),
        re.IGNORECASE,
    )
    for path in APP_WORKER_DIR.rglob("*"):
        if path.is_dir():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not banned.search(text), f"disallowed reference found in {path}"


def test_package_does_not_touch_app_shell_or_app_theme():
    """This package's brief explicitly forbids editing packages/app-shell
    or packages/app-theme (nav/home wiring is the main loop's job). This
    test only asserts app-worker's own manifest declares no dependency on
    them; it cannot detect a stray edit elsewhere in the repo, but keeps
    the intent on record.
    """
    manifest = json.loads((APP_WORKER_DIR / "dbbasic-package.json").read_text())
    assert "app-shell" not in manifest.get("dependencies", [])
    assert "app-theme" not in manifest.get("dependencies", [])
