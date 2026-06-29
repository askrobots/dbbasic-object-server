import json

import object_permission_store
import object_permissions as permissions


def test_load_policy_returns_conservative_default_when_file_is_missing(tmp_path):
    policy = object_permission_store.load_policy(tmp_path / "data")

    assert policy.access_mode == "role_based"
    assert policy.rules == ()


def test_save_and_load_policy_round_trips_rules(tmp_path):
    data_dir = tmp_path / "data"
    policy = permissions.PermissionPolicy(
        access_mode="role_based",
        roles={"sales": {"label": "Sales"}},
        user_roles={"42": ("sales",)},
        rules=(
            permissions.PermissionRule.allow(
                "role:sales",
                [permissions.READ],
                collection="contacts",
                row_filter={"owner_id": "$user_id"},
                fields=["name", "email"],
                denied_fields=["internal_notes"],
                reason="sales reps only see own contacts",
            ),
        ),
    )

    path = object_permission_store.save_policy(policy, data_dir)
    loaded = object_permission_store.load_policy(data_dir)

    assert path == data_dir / "permissions" / "policy.json"
    assert loaded.access_mode == "role_based"
    assert loaded.roles == {"sales": {"label": "Sales"}}
    assert loaded.user_roles == {"42": ("sales",)}
    assert len(loaded.rules) == 1
    assert loaded.rules[0].principal == "role:sales"
    assert loaded.rules[0].row_filter == {"owner_id": "$user_id"}
    assert loaded.rules[0].fields == frozenset({"name", "email"})
    assert loaded.rules[0].denied_fields == frozenset({"internal_notes"})


def test_replace_policy_validates_and_writes_payload(tmp_path):
    data_dir = tmp_path / "data"
    payload = {
        "access_mode": "subscription",
        "roles": {},
        "user_roles": {},
        "rules": [
            {
                "effect": "allow",
                "principal": "subscription:pro",
                "actions": ["read"],
                "collection": "reports",
                "reason": "active pro subscription",
            }
        ],
        "admin_roles": ["admin"],
    }

    policy = object_permission_store.replace_policy(payload, data_dir)
    saved_payload = json.loads((data_dir / "permissions" / "policy.json").read_text())

    assert policy.access_mode == "subscription"
    assert saved_payload["access_mode"] == "subscription"
    assert saved_payload["rules"][0]["principal"] == "subscription:pro"


def test_load_policy_rejects_invalid_json(tmp_path):
    policy_file = tmp_path / "data" / "permissions" / "policy.json"
    policy_file.parent.mkdir(parents=True)
    policy_file.write_text("{")

    try:
        object_permission_store.load_policy(tmp_path / "data")
    except ValueError as exc:
        assert str(exc) == "Permission policy file contains invalid JSON"
    else:
        raise AssertionError("Expected invalid policy JSON to fail")
