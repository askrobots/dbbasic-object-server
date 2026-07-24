"""Structural tests for packages/app-settings (Phase 6: user_prefs + feature_flags).

Mirrors the package/schema/permission testing conventions used for
packages/app-notes in tests/test_object_packages.py and
tests/test_object_permissions.py.
"""

import json
import re
from pathlib import Path

import object_packages
import object_permissions
import object_schemas

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_SETTINGS_DIR = PACKAGES_ROOT / "app-settings"


def test_get_package_normalizes_app_settings_manifest():
    package = object_packages.get_package("app-settings", root=PACKAGES_ROOT)

    assert package["id"] == "app-settings"
    assert package["name"] == "Settings"
    assert package["version"] == "0.2.0"
    assert package["objects"] == []
    assert package["seed"] == [{"collection": "ai_prices", "path": "seed/ai_prices.tsv"}]
    assert package["permissions"] == [{"path": "permissions/rules.json"}]
    assert {schema["collection"] for schema in package["schemas"]} == {
        "user_prefs",
        "feature_flags",
        "ai_prices",
        "app_settings",  # server-wide per-app config (payments.* first)
    }


def test_dry_run_app_settings_package_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()

    plan = object_packages.dry_run_package(
        "app-settings",
        root=PACKAGES_ROOT,
        base_dir=tmp_path / "data",
        object_roots=[object_root],
    )

    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []
    assert {schema["collection"] for schema in plan["schemas"]} == {
        "user_prefs",
        "feature_flags",
        "ai_prices",
        "app_settings",  # server-wide per-app config (payments.* first)
    }


def test_install_app_settings_package_loads_schemas(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()

    object_packages.install_package(
        "app-settings",
        root=PACKAGES_ROOT,
        base_dir=data_dir,
        object_roots=[object_root],
    )

    prefs_schema = object_schemas.get_schema("user_prefs", base_dir=data_dir)
    flags_schema = object_schemas.get_schema("feature_flags", base_dir=data_dir)

    assert prefs_schema["name"] == "user_prefs"
    assert [field["name"] for field in prefs_schema["fields"]] == [
        "id",
        "owner_id",
        "key",
        "value",
    ]
    assert prefs_schema["forms"]["default"]["fields"] == ["key", "value"]

    assert flags_schema["name"] == "feature_flags"
    assert [field["name"] for field in flags_schema["fields"]] == [
        "id",
        "flag",
        "value",
        "description",
    ]

    prices_schema = object_schemas.get_schema("ai_prices", base_dir=data_dir)
    assert prices_schema["name"] == "ai_prices"
    assert [field["name"] for field in prices_schema["fields"]] == [
        "id",
        "provider",
        "model",
        "input_per_million_cents",
        "output_per_million_cents",
        "effective_date",
    ]


def test_schema_json_files_are_valid_and_versioned():
    for name in ("user_prefs", "feature_flags", "ai_prices"):
        payload = json.loads((APP_SETTINGS_DIR / "schemas" / f"{name}.json").read_text())
        assert payload["name"] == name
        assert payload["version"] == 1
        assert payload["views"]["list_mode"] == "table"


def test_install_app_settings_package_seeds_ai_prices(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()

    object_packages.install_package(
        "app-settings",
        root=PACKAGES_ROOT,
        base_dir=data_dir,
        object_roots=[object_root],
    )

    import object_records

    rows = object_records.read_collection_records("ai_prices", base_dir=data_dir)
    assert rows  # seeded with at least one current entry
    for row in rows:
        # Integer cents only -- never a float for money.
        assert re.fullmatch(r"-?\d+", row["input_per_million_cents"])
        assert re.fullmatch(r"-?\d+", row["output_per_million_cents"])


def _app_settings_policy():
    payload = json.loads((APP_SETTINGS_DIR / "permissions" / "rules.json").read_text())
    return object_permissions.policy_from_dict({"access_mode": "role_based", "rules": payload["rules"]})


def test_user_prefs_row_filter_scopes_reads_to_owner():
    policy = _app_settings_policy()
    subject = object_permissions.PermissionSubject(user_id="7")

    own_row = object_permissions.check_permission(
        subject,
        object_permissions.READ,
        policy=policy,
        collection="user_prefs",
        record={"owner_id": "7", "key": "theme", "value": "dark"},
    )
    other_row = object_permissions.check_permission(
        subject,
        object_permissions.READ,
        policy=policy,
        collection="user_prefs",
        record={"owner_id": "8", "key": "theme", "value": "light"},
    )

    assert own_row.allowed is True
    assert other_row.allowed is False


def test_user_prefs_row_filter_scopes_writes_to_owner():
    policy = _app_settings_policy()
    subject = object_permissions.PermissionSubject(user_id="7")

    own_update = object_permissions.check_permission(
        subject,
        object_permissions.UPDATE,
        policy=policy,
        collection="user_prefs",
        record={"owner_id": "7", "key": "theme", "value": "dark"},
    )
    other_delete = object_permissions.check_permission(
        subject,
        object_permissions.DELETE,
        policy=policy,
        collection="user_prefs",
        record={"owner_id": "8", "key": "theme", "value": "light"},
    )

    assert own_update.allowed is True
    assert other_delete.allowed is False


def test_feature_flags_readable_by_registered_users():
    policy = _app_settings_policy()
    subject = object_permissions.PermissionSubject(user_id="7")

    decision = object_permissions.check_permission(
        subject, object_permissions.READ, policy=policy, collection="feature_flags"
    )

    assert decision.allowed is True


def test_feature_flags_not_writable_by_registered_users():
    policy = _app_settings_policy()
    subject = object_permissions.PermissionSubject(user_id="7")
    record = {"flag": "kanban_view", "value": "on", "description": "Kanban board"}

    create = object_permissions.check_permission(
        subject, object_permissions.CREATE, policy=policy, collection="feature_flags", record=record
    )
    update = object_permissions.check_permission(
        subject, object_permissions.UPDATE, policy=policy, collection="feature_flags", record=record
    )
    delete = object_permissions.check_permission(
        subject, object_permissions.DELETE, policy=policy, collection="feature_flags", record=record
    )

    assert create.allowed is False
    assert update.allowed is False
    assert delete.allowed is False


def test_feature_flags_unreachable_by_anonymous_reads():
    policy = _app_settings_policy()

    decision = object_permissions.check_permission(
        None, object_permissions.READ, policy=policy, collection="feature_flags"
    )

    assert decision.allowed is False


def test_ai_prices_readable_by_registered_users():
    policy = _app_settings_policy()
    subject = object_permissions.PermissionSubject(user_id="7")

    decision = object_permissions.check_permission(
        subject, object_permissions.READ, policy=policy, collection="ai_prices"
    )

    assert decision.allowed is True


def test_ai_prices_not_writable_by_registered_users():
    """Prices are live-editable data, but only for admins (who bypass the
    policy engine entirely) -- an ordinary registered user must not be able
    to create, update, or delete a price row, or cost recording could be
    corrupted the same way the predecessor's hardcoded table went stale.
    """
    policy = _app_settings_policy()
    subject = object_permissions.PermissionSubject(user_id="7")
    record = {
        "provider": "anthropic",
        "model": "claude-haiku-4-5",
        "input_per_million_cents": "1",
        "output_per_million_cents": "1",
        "effective_date": "2026-01-01",
    }

    create = object_permissions.check_permission(
        subject, object_permissions.CREATE, policy=policy, collection="ai_prices", record=record
    )
    update = object_permissions.check_permission(
        subject, object_permissions.UPDATE, policy=policy, collection="ai_prices", record=record
    )
    delete = object_permissions.check_permission(
        subject, object_permissions.DELETE, policy=policy, collection="ai_prices", record=record
    )

    assert create.allowed is False
    assert update.allowed is False
    assert delete.allowed is False


def test_ai_prices_unreachable_by_anonymous_reads():
    policy = _app_settings_policy()

    decision = object_permissions.check_permission(
        None, object_permissions.READ, policy=policy, collection="ai_prices"
    )

    assert decision.allowed is False
