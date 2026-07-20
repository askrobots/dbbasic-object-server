"""Structural + permission tests for packages/app-rollup
(plan/vocabulary/14-rollup-spec.md).

Mirrors tests/test_app_settings_package.py's package/schema/permission
conventions. The worked end-to-end example (orders-per-day) and the
"a rollup target can be public while its source stays owner-scoped"
payoff test live here rather than in tests/test_object_rollups.py because
they exercise the real package manifest + real permission policy, not
just object_rollups.py in isolation.
"""

import json
import re
from pathlib import Path

import object_packages
import object_permissions
import object_records
import object_rollups
import object_schemas

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_ROLLUP_DIR = PACKAGES_ROOT / "app-rollup"


def _write_schema(data_dir, collection, fields):
    path = data_dir / "schemas" / f"{collection}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": collection, "fields": fields}))
    return path


# --- Package manifest / install --------------------------------------------

def test_get_package_normalizes_app_rollup_manifest():
    package = object_packages.get_package("app-rollup", root=PACKAGES_ROOT)

    assert package["id"] == "app-rollup"
    assert package["name"] == "Rollups"
    assert package["objects"] == []
    assert package["seed"] == []
    assert package["permissions"] == [{"path": "permissions/rules.json"}]
    assert {schema["collection"] for schema in package["schemas"]} == {"rollup_definitions"}


def test_dry_run_app_rollup_package_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()

    plan = object_packages.dry_run_package(
        "app-rollup", root=PACKAGES_ROOT, base_dir=tmp_path / "data", object_roots=[object_root],
    )

    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []
    assert {schema["collection"] for schema in plan["schemas"]} == {"rollup_definitions"}


def test_install_app_rollup_package_loads_schema(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()

    object_packages.install_package(
        "app-rollup", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root],
    )

    schema = object_schemas.get_schema("rollup_definitions", base_dir=data_dir)
    assert schema["name"] == "rollup_definitions"
    field_names = [f["name"] for f in schema["fields"]]
    assert field_names == [
        "id", "name", "source_collection", "filter", "group_by", "time_bucket",
        "metrics", "target_collection", "min_group_size", "refresh_mode",
        "refresh_interval_seconds", "last_computed_at", "enabled",
    ]
    by_name = {f["name"]: f for f in schema["fields"]}
    assert by_name["last_computed_at"].get("read_only") is True
    assert schema["views"]["list_mode"] == "table"


def test_schema_json_file_is_valid_and_versioned():
    payload = json.loads((APP_ROLLUP_DIR / "schemas" / "rollup_definitions.json").read_text())
    assert payload["name"] == "rollup_definitions"
    assert payload["version"] == 1
    assert payload["views"]["list_mode"] == "table"


def test_no_disallowed_org_names_leak_into_the_package():
    """Public repo hygiene: no internal org/codename references anywhere
    in this package's source (same guard as tests/test_app_orders_package.py).
    """
    banned = re.compile(
        "|".join([r"\b" + "q" + "9" + r"\b", "ask" + "robots", r"\b" + "wo" + "ld" + r"\b"]),
        re.IGNORECASE,
    )
    for path in APP_ROLLUP_DIR.rglob("*"):
        if path.is_dir():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not banned.search(text), f"disallowed reference found in {path}"


# --- Permissions: rollup_definitions is admin-owned, full stop -------------

def _app_rollup_policy(extra_rules=()):
    payload = json.loads((APP_ROLLUP_DIR / "permissions" / "rules.json").read_text())
    rules = list(payload["rules"]) + list(extra_rules)
    return object_permissions.policy_from_dict({"access_mode": "role_based", "rules": rules})


def test_rollup_definitions_denied_to_registered_non_admin_for_every_action():
    policy = _app_rollup_policy()
    subject = object_permissions.PermissionSubject(user_id="7")
    record = {"id": "r1", "name": "Orders per day", "source_collection": "orders"}

    for action in (
        object_permissions.CREATE, object_permissions.READ,
        object_permissions.UPDATE, object_permissions.DELETE,
    ):
        decision = object_permissions.check_permission(
            subject, action, policy=policy, collection="rollup_definitions", record=record,
        )
        assert decision.allowed is False, action


def test_rollup_definitions_unreachable_by_anonymous_reads():
    policy = _app_rollup_policy()

    decision = object_permissions.check_permission(
        None, object_permissions.READ, policy=policy, collection="rollup_definitions",
    )

    assert decision.allowed is False


def test_rollup_definitions_allowed_for_admin_role():
    policy = _app_rollup_policy()
    admin = object_permissions.PermissionSubject(user_id="1", roles=("admin",))

    for action in (
        object_permissions.CREATE, object_permissions.READ,
        object_permissions.UPDATE, object_permissions.DELETE,
    ):
        decision = object_permissions.check_permission(
            admin, action, policy=policy, collection="rollup_definitions",
        )
        assert decision.allowed is True, action


# --- Worked example: orders-per-day, end-to-end ----------------------------

ORDERS_FIELDS = [
    {"name": "id"},
    {"name": "owner_id", "type": "text"},
    {"name": "channel", "type": "text"},
    {"name": "status", "type": "text"},
    {"name": "total_cents", "type": "integer"},
    {"name": "created_at", "type": "datetime"},
]


def _install_worked_example(data_dir, object_root):
    object_packages.install_package(
        "app-rollup", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root],
    )
    _write_schema(data_dir, "orders", ORDERS_FIELDS)

    # Two customers' paid orders (owner-scoped source data) plus one draft
    # order that the rollup's filter must exclude.
    for row in (
        {"id": "o1", "owner_id": "alice", "channel": "web", "status": "paid",
         "total_cents": "1000", "created_at": "2026-07-01T09:00:00Z"},
        {"id": "o2", "owner_id": "bob", "channel": "web", "status": "paid",
         "total_cents": "3000", "created_at": "2026-07-01T15:00:00Z"},
        {"id": "o3", "owner_id": "alice", "channel": "retail", "status": "paid",
         "total_cents": "500", "created_at": "2026-07-01T10:00:00Z"},
        {"id": "o4", "owner_id": "bob", "channel": "web", "status": "draft",
         "total_cents": "999999", "created_at": "2026-07-01T09:00:00Z"},
    ):
        object_records.create_collection_record("orders", row, base_dir=data_dir, roots=[object_root])

    definition = {
        "id": "rollup_orders_by_day",
        "name": "Orders per day",
        "source_collection": "orders",
        "filter": json.dumps({"status": "paid"}),
        "group_by": json.dumps(["channel"]),
        "time_bucket": json.dumps({"field": "created_at", "granularity": "day"}),
        "metrics": json.dumps([
            {"op": "count", "as": "order_count"},
            {"op": "sum", "field": "total_cents", "as": "revenue_cents"},
            {"op": "avg", "field": "total_cents", "as": "avg_order_cents"},
        ]),
        "target_collection": "rollup_orders_by_day",
        "min_group_size": "",
        "refresh_mode": "scheduled",
        "refresh_interval_seconds": "3600",
        "enabled": "true",
    }
    object_records.create_collection_record(
        "rollup_definitions", definition, base_dir=data_dir, roots=[object_root], actor="admin",
    )
    return definition


def test_worked_example_orders_per_day_computes_expected_rows(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    definition = _install_worked_example(data_dir, object_root)

    result = object_rollups.compute_rollup(definition, base_dir=data_dir, roots=[object_root])
    assert result["groups"] == 2  # web, retail -- the draft order never counts

    rows = object_records.read_collection_records("rollup_orders_by_day", base_dir=data_dir, roots=[object_root])
    by_channel = {r["channel"]: r for r in rows}

    assert by_channel["web"]["bucket_start"] == "2026-07-01"
    assert by_channel["web"]["order_count"] == "2"
    assert by_channel["web"]["revenue_cents"] == "4000"  # alice's 1000 + bob's 3000
    assert by_channel["web"]["avg_order_cents"] == "2000.0"
    assert by_channel["retail"]["order_count"] == "1"
    assert by_channel["retail"]["revenue_cents"] == "500"

    # No customer identity anywhere in the target -- owner_id was never a
    # group_by field or a metric, so it never made it into the derived
    # schema at all.
    schema = object_schemas.get_schema("rollup_orders_by_day", base_dir=data_dir)
    assert "owner_id" not in [f["name"] for f in schema["fields"]]


def test_worked_example_rollup_target_can_be_public_while_source_stays_owner_scoped(tmp_path):
    """The payoff named in 14's Permissions Posture: orders is owner-
    scoped, but rollup_orders_by_day (aggregate counts/revenue, no
    customer identity in any row) can carry a public-read rule of its
    own, granted independently of the source's rule.
    """
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    definition = _install_worked_example(data_dir, object_root)
    object_rollups.compute_rollup(definition, base_dir=data_dir, roots=[object_root])

    target_row = object_records.read_collection_records(
        "rollup_orders_by_day", base_dir=data_dir, roots=[object_root]
    )[0]

    policy = _app_rollup_policy([
        {
            "effect": "allow",
            "principal": "registered",
            "actions": ["create", "read", "update", "delete"],
            "collection": "orders",
            "row_filter": {"owner_id": "$user_id"},
            "reason": "signed-in users manage their own orders",
        },
        {
            "effect": "allow",
            "principal": "public",
            "actions": ["read"],
            "collection": "rollup_orders_by_day",
            "reason": "aggregate revenue by day and channel; no individual order or customer identity",
        },
    ])

    order_row = {"id": "o1", "owner_id": "alice", "channel": "web", "status": "paid", "total_cents": "1000"}

    # The source stays exactly as private as it always was: an anonymous
    # visitor, and even a signed-in user who isn't the owner, are denied.
    anon_on_orders = object_permissions.check_permission(
        None, object_permissions.READ, policy=policy, collection="orders", record=order_row,
    )
    other_user_on_orders = object_permissions.check_permission(
        object_permissions.PermissionSubject(user_id="bob"),
        object_permissions.READ, policy=policy, collection="orders", record=order_row,
    )
    assert anon_on_orders.allowed is False
    assert other_user_on_orders.allowed is False

    # The rollup target, carrying its OWN rule, is readable by anyone --
    # including someone with no account at all -- despite resting entirely
    # on data drawn from the owner-scoped orders collection above.
    anon_on_rollup = object_permissions.check_permission(
        None, object_permissions.READ, policy=policy, collection="rollup_orders_by_day", record=target_row,
    )
    assert anon_on_rollup.allowed is True

    # And the rollup target still isn't writable by that same public reader
    # -- only the daemon pass (via allow_computed_submission, never wired
    # to a request) writes its rows.
    anon_write_on_rollup = object_permissions.check_permission(
        None, object_permissions.UPDATE, policy=policy, collection="rollup_orders_by_day", record=target_row,
    )
    assert anon_write_on_rollup.allowed is False


def test_worked_example_min_group_size_suppresses_a_single_customer_day(tmp_path):
    """14's Permissions Posture disclosure-threat worked case: a channel
    with exactly one paid order on a given day, grouped finely enough,
    would otherwise disclose that one customer's presence (and revenue)
    to anyone who can read a public target. min_group_size=2 drops it.
    """
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    definition = _install_worked_example(data_dir, object_root)
    definition["min_group_size"] = "2"

    result = object_rollups.compute_rollup(definition, base_dir=data_dir, roots=[object_root])

    # "retail" has exactly one paid order (alice's) -- suppressed. "web"
    # has two (alice's and bob's) -- kept.
    assert result["suppressed"] == 1
    rows = object_records.read_collection_records("rollup_orders_by_day", base_dir=data_dir, roots=[object_root])
    assert {r["channel"] for r in rows} == {"web"}
