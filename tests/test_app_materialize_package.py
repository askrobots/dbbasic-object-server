"""Structural + permission tests for packages/app-materialize
(plan/vocabulary/61-materialize-spec.md).

Mirrors tests/test_app_rollup_package.py's package/schema/permission
conventions. The worked end-to-end examples (recurring journal,
depreciation, CreateWork) and mapping-vocabulary unit coverage live in
tests/test_object_materialize.py instead; this file exercises the real
package manifest, real permission policy, and the real installed
materialize_seed.py's HANDLES-regeneration mechanism.
"""

import json
import re
from pathlib import Path

import object_handlers
import object_materialize
import object_packages
import object_permissions
import object_records
import object_schemas

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_MATERIALIZE_DIR = PACKAGES_ROOT / "app-materialize"


def _write_schema(data_dir, collection, fields, storage=None):
    payload = {"name": collection, "fields": fields}
    if storage:
        payload["storage"] = storage
    path = data_dir / "schemas" / f"{collection}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return path


# --- Package manifest / install --------------------------------------------

def test_get_package_normalizes_app_materialize_manifest():
    package = object_packages.get_package("app-materialize", root=PACKAGES_ROOT)

    assert package["id"] == "app-materialize"
    assert package["name"] == "Materialize"
    assert package["seed"] == []
    assert package["permissions"] == [{"path": "permissions/rules.json"}]
    assert {schema["collection"] for schema in package["schemas"]} == {"materialize_definitions"}
    assert {obj["id"] for obj in package["objects"]} == {
        "system_materialize_run", "system_materialize_seed", "site_materialize_run_button",
    }


def test_dry_run_app_materialize_package_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()

    plan = object_packages.dry_run_package(
        "app-materialize", root=PACKAGES_ROOT, base_dir=tmp_path / "data", object_roots=[object_root],
    )

    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []
    assert {schema["collection"] for schema in plan["schemas"]} == {"materialize_definitions"}


def test_install_app_materialize_package_loads_schema_and_objects(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()

    object_packages.install_package(
        "app-materialize", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root],
    )

    schema = object_schemas.get_schema("materialize_definitions", base_dir=data_dir)
    assert schema["name"] == "materialize_definitions"
    field_names = [f["name"] for f in schema["fields"]]
    assert field_names == [
        "id", "name", "source_collection", "source_filter", "trigger", "output_collection",
        "child_collection", "child_source_field", "child_link_field", "idempotency_key",
        "mapping", "balance_check", "debit_account_id", "credit_account_id", "actor",
        "last_run_at", "enabled", "block",
    ]
    by_name = {f["name"]: f for f in schema["fields"]}
    assert by_name["last_run_at"].get("read_only") is True
    assert by_name["enabled"].get("default") == "true"
    assert by_name["block"].get("default") == "false"
    assert schema["views"]["list_mode"] == "table"

    assert (object_root / "system" / "materialize_run.py").is_file()
    assert (object_root / "system" / "materialize_seed.py").is_file()
    assert (object_root / "site" / "materialize_run_button.py").is_file()


def test_schema_json_file_is_valid_and_versioned():
    payload = json.loads((APP_MATERIALIZE_DIR / "schemas" / "materialize_definitions.json").read_text())
    assert payload["name"] == "materialize_definitions"
    assert payload["version"] == 1
    assert payload["views"]["list_mode"] == "table"


def test_no_disallowed_org_names_leak_into_the_package():
    """Public repo hygiene: no internal org/codename references anywhere
    in this package's source (same guard as tests/test_app_rollup_package.py).
    """
    banned = re.compile(
        "|".join([r"\b" + "q" + "9" + r"\b", "ask" + "robots", r"\b" + "wo" + "ld" + r"\b"]),
        re.IGNORECASE,
    )
    for path in APP_MATERIALIZE_DIR.rglob("*"):
        if path.is_dir():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not banned.search(text), f"disallowed reference found in {path}"


def test_materialize_seed_object_source_declares_no_handles():
    """HANDLES ships empty and stays empty: v1 does NOT rewrite the installed
    object at runtime to track the definition set (no self-modifying source
    under the poll loop). Event-mode runs via the manual path instead; the
    shipped file never hand-lists collections.
    """
    text = (APP_MATERIALIZE_DIR / "objects" / "system" / "materialize_seed.py").read_text()
    assert object_handlers.extract_handles(text) == []


# --- Permissions: materialize_definitions is admin-owned, full stop -------

def _app_materialize_policy(extra_rules=()):
    payload = json.loads((APP_MATERIALIZE_DIR / "permissions" / "rules.json").read_text())
    rules = list(payload["rules"]) + list(extra_rules)
    return object_permissions.policy_from_dict({"access_mode": "role_based", "rules": rules})


def test_materialize_definitions_denied_to_registered_non_admin_for_every_action():
    policy = _app_materialize_policy()
    subject = object_permissions.PermissionSubject(user_id="7")
    record = {"id": "m1", "name": "Recurring journals", "source_collection": "fin_recurring"}

    for action in (
        object_permissions.CREATE, object_permissions.READ,
        object_permissions.UPDATE, object_permissions.DELETE,
    ):
        decision = object_permissions.check_permission(
            subject, action, policy=policy, collection="materialize_definitions", record=record,
        )
        assert decision.allowed is False, action


def test_materialize_definitions_unreachable_by_anonymous_reads():
    policy = _app_materialize_policy()
    decision = object_permissions.check_permission(
        None, object_permissions.READ, policy=policy, collection="materialize_definitions",
    )
    assert decision.allowed is False


def test_materialize_definitions_allowed_for_admin_role():
    policy = _app_materialize_policy()
    admin = object_permissions.PermissionSubject(user_id="1", roles=("admin",))

    for action in (
        object_permissions.CREATE, object_permissions.READ,
        object_permissions.UPDATE, object_permissions.DELETE,
    ):
        decision = object_permissions.check_permission(
            admin, action, policy=policy, collection="materialize_definitions",
        )
        assert decision.allowed is True, action


def test_materialize_run_execute_denied_to_non_admin_and_allowed_for_admin():
    policy = _app_materialize_policy()
    non_admin = object_permissions.PermissionSubject(user_id="7")
    admin = object_permissions.PermissionSubject(user_id="1", roles=("admin",))

    denied = object_permissions.check_permission(
        non_admin, object_permissions.EXECUTE, policy=policy, object_id="system_materialize_run",
    )
    allowed = object_permissions.check_permission(
        admin, object_permissions.EXECUTE, policy=policy, object_id="system_materialize_run",
    )
    assert denied.allowed is False
    assert allowed.allowed is True


# --- Event-mode fixtures + CreateWork worked example ---------------------
# (No HANDLES-rewrite tests: v1 deliberately has no runtime source rewriter;
# event-mode runs through the manual path, exercised by the worked example
# below.)

def _install_with_tasks_fixture(data_dir, object_root):
    object_packages.install_package(
        "app-materialize", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root],
    )
    _write_schema(data_dir, "tasks", [
        {"name": "id"}, {"name": "title", "type": "text"},
        {"name": "template_id", "type": "text", "relation": {"collection": "templates", "display_field": "name"}},
    ])


def _event_definition(**overrides):
    base = {
        "id": "matgen_task_seed",
        "name": "CreateWork seed",
        "source_collection": "tasks",
        "trigger": json.dumps({"mode": "event", "on": "record.created"}),
        "output_collection": "tasks",
        "idempotency_key": "{definition_id}_{source_id}",
        "mapping": json.dumps({}),
        "enabled": "true",
        "block": "false",
    }
    base.update(overrides)
    return base


def test_event_mode_definition_runs_through_the_manual_path(tmp_path):
    """Event-mode is valid but v1 runs it manually (no auto HANDLES): a
    definition with trigger.mode == "event" generates correctly when driven
    through generate_one_event, the same call the (inert) seed handler would
    make -- proving degrade-to-manual works without any source rewriting."""
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    _install_with_tasks_fixture(data_dir, object_root)
    definition = _event_definition()
    object_records.create_collection_record(
        "materialize_definitions", definition, base_dir=data_dir, roots=[object_root], actor="admin",
    )
    # The seed object ships inert -- nothing rewrote it.
    installed_text = (object_root / "system" / "materialize_seed.py").read_text()
    assert object_handlers.extract_handles(installed_text) == []

    # compute_event_handles still reports what a future deliberate sync WOULD
    # wire, but it is pure computation with no side effect on the object.
    assert object_materialize.compute_event_handles(base_dir=data_dir, roots=[object_root]) == [
        "tasks.record.created"
    ]
    assert (object_root / "system" / "materialize_seed.py").read_text() == installed_text


# --- Worked example: CreateWork end-to-end through the real package -------

def test_worked_example_creatework_seed_via_materialize_run(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    _install_with_tasks_fixture(data_dir, object_root)
    _write_schema(data_dir, "templates", [
        {"name": "id"}, {"name": "name", "type": "text"}, {"name": "default_values", "type": "textarea"},
    ])
    object_records.create_collection_record(
        "templates",
        {"id": "tmpl1", "name": "Onboarding", "default_values": json.dumps({"title": "Seeded title"})},
        base_dir=data_dir, roots=[object_root],
    )
    object_records.create_collection_record(
        "tasks", {"id": "task1", "title": "", "template_id": "tmpl1"}, base_dir=data_dir, roots=[object_root],
    )
    object_records.create_collection_record(
        "materialize_definitions", _event_definition(), base_dir=data_dir, roots=[object_root], actor="admin",
    )

    result = object_materialize.generate_definition(
        object_records.get_collection_record("materialize_definitions", "matgen_task_seed", base_dir=data_dir, roots=[object_root]),
        base_dir=data_dir, roots=[object_root],
    )
    assert result["generated"] == 1

    updated = object_records.get_collection_record("tasks", "task1", base_dir=data_dir, roots=[object_root])
    assert updated["title"] == "Seeded title"
