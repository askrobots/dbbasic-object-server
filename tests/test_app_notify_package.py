"""Structural tests for packages/app-notify (12 notify): the notify_rules
config collection (admin-owned) + the seeded task-assigned example rule."""

import json
from pathlib import Path

import object_packages
import object_permissions

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_NOTIFY_DIR = PACKAGES_ROOT / "app-notify"


def test_manifest_and_schema():
    package = object_packages.get_package("app-notify", root=PACKAGES_ROOT)
    assert package["id"] == "app-notify"
    assert {s["collection"] for s in package["schemas"]} == {"notify_rules"}
    assert package["objects"] == []  # engine is object_notify + the daemon pass, not a page
    assert {e["collection"] for e in package["seed"]} == {"notify_rules"}
    schema = json.loads((APP_NOTIFY_DIR / "schemas" / "notify_rules.json").read_text())
    names = {f["name"] for f in schema["fields"]}
    assert {"event_pattern", "match", "recipients", "suppress_self", "channels"} <= names


def test_notify_rules_is_admin_owned():
    payload = json.loads((APP_NOTIFY_DIR / "permissions" / "rules.json").read_text())
    policy = object_permissions.policy_from_dict({"access_mode": "role_based", "rules": payload["rules"]})
    admin = object_permissions.PermissionSubject(user_id="1", roles=("admin",))
    plain = object_permissions.PermissionSubject(user_id="7")
    for action in (object_permissions.CREATE, object_permissions.READ,
                   object_permissions.UPDATE, object_permissions.DELETE):
        assert object_permissions.check_permission(admin, action, policy=policy, collection="notify_rules").allowed is True
        assert object_permissions.check_permission(plain, action, policy=policy, collection="notify_rules").allowed is False


def test_seed_ships_the_task_assigned_example_rule():
    import csv
    rows = list(csv.DictReader(open(APP_NOTIFY_DIR / "seed" / "notify_rules.tsv"), delimiter="\t"))
    assert len(rows) == 1
    rule = rows[0]
    assert rule["event_pattern"] == "tasks.record.updated"
    assert json.loads(rule["match"]) == {"status": "assigned"}
    assert json.loads(rule["recipients"]) == {"mode": "field", "field": "assigned_to"}
    channels = json.loads(rule["channels"])
    assert any(c["channel"] == "in_app" for c in channels)


def test_dry_run_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()
    plan = object_packages.dry_run_package(
        "app-notify", root=PACKAGES_ROOT, base_dir=tmp_path / "data", object_roots=[object_root])
    assert plan["safe_to_install"] is True and plan["warnings"] == []
