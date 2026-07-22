"""Structural tests for packages/app-tasks (tasks).

Mirrors the package/schema/permission testing conventions used for
packages/app-contacts and packages/app-invoices in tests/test_app_contacts_
package.py and tests/test_app_invoices_package.py. There was no dedicated
test file for app-tasks before this one; it fills that gap while also
covering the v5 -> v6 "Work / CreateWork" field additions (task_type/
template_id/instructions/metadata), reconciled against a private
predecessor-system audit, not part of this repo. The guarded-transition
machinery on `status` (schema v5's headline feature) is asserted intact
and untouched throughout.
"""

import json
import re
from pathlib import Path

import object_packages
import object_permissions
import object_records
import object_schemas

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_TASKS_DIR = PACKAGES_ROOT / "app-tasks"

_SCHEMA_NAMES = ("tasks",)


def _schema(name):
    return json.loads((APP_TASKS_DIR / "schemas" / f"{name}.json").read_text())


def test_get_package_normalizes_app_tasks_manifest():
    package = object_packages.get_package("app-tasks", root=PACKAGES_ROOT)

    assert package["id"] == "app-tasks"
    assert package["name"] == "Tasks"
    assert {schema["collection"] for schema in package["schemas"]} == set(_SCHEMA_NAMES)
    assert {obj["id"] for obj in package["objects"]} == {"site_tasks"}
    assert package["permissions"] == [{"path": "permissions/rules.json"}]
    # Plus "views"/"site_routes": one 59 detail view + route (/tasks/{id})
    # composing task_comments and files as related children.
    assert {entry["collection"] for entry in package["seed"]} == set(_SCHEMA_NAMES) | {
        "views", "site_routes",
    }
    assert package["migrations"] == []
    # template_id is a soft/optional relation (see dbbasic-package.json's
    # description) -- no install-order dependency on app-templates is
    # declared, the same choice app-invoices made for customer_id -> contacts.
    assert package["dependencies"] == [{"id": "app-projects", "version": None}]


def test_dry_run_app_tasks_package_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()

    plan = object_packages.dry_run_package(
        "app-tasks",
        root=PACKAGES_ROOT,
        base_dir=tmp_path / "data",
        object_roots=[object_root],
    )

    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []
    assert {schema["collection"] for schema in plan["schemas"]} == set(_SCHEMA_NAMES)


def test_install_app_tasks_package_loads_schema_and_page(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()

    object_packages.install_package(
        "app-tasks", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root],
    )

    schema = object_schemas.get_schema("tasks", base_dir=data_dir)
    assert schema["name"] == "tasks"
    assert (object_root / "site" / "tasks.py").is_file()


def test_schema_json_file_is_valid():
    payload = _schema("tasks")
    assert payload["name"] == "tasks"
    assert isinstance(payload["version"], int)


def test_tasks_schema_is_now_version_6_additive_only():
    """tasks.json went 5 -> 6 adding task_type/template_id/instructions/
    metadata, then 7 -> 8 adding views.filter_fields (status/urgency) for the
    generative filter bar, then 8 -> 9 adding capabilities.comments (the
    generic comment thread on the task detail). Every field that existed at v5
    is still present, unchanged in name/type -- these schema changes are
    purely additive.
    """
    schema = _schema("tasks")
    assert schema["version"] == 9
    assert schema["views"]["filter_fields"] == ["status", "urgency"]
    assert schema["capabilities"]["comments"] is True
    by_name = {f["name"]: f for f in schema["fields"]}

    for name in ("id", "title", "description", "project_id", "status",
                 "urgency", "due_date", "assigned_to", "created_at", "owner_id"):
        assert name in by_name, f"v5 field {name!r} must not be removed"
    assert by_name["title"]["required"] is True
    assert by_name["created_at"]["read_only"] is True

    for new_field in ("instructions", "metadata"):
        field = by_name[new_field]
        assert field["type"] == "textarea"
        assert "required" not in field or not field["required"]

    task_type = by_name["task_type"]
    assert task_type["type"] == "enum"
    assert task_type["enum"] == ["BASIC", "STANDARD", "CONTRACT"]
    assert task_type["default"] == "STANDARD"

    template_id = by_name["template_id"]
    assert template_id["relation"]["collection"] == "templates"
    assert "required" not in template_id or not template_id["required"]


def test_metadata_field_has_no_dedicated_json_type():
    """Same convention as app-templates' schema/default_values fields --
    this codebase's field-type contract (docs/schema-forms.md) has no
    dedicated json type, so metadata stores JSON as a string in a
    `textarea` field.
    """
    by_name = {f["name"]: f for f in _schema("tasks")["fields"]}
    assert by_name["metadata"]["type"] == "textarea"


def test_urgency_is_exact_source_parity_and_untouched():
    """The parity doc calls urgency an EXACT MATCH with the audited
    predecessor and says not to alter it.
    """
    urgency = {f["name"]: f for f in _schema("tasks")["fields"]}["urgency"]
    assert urgency["enum"] == ["low", "normal", "high", "critical"]
    assert urgency["default"] == "normal"
    assert "transitions" not in urgency


def test_status_guarded_transitions_are_completely_untouched():
    """The headline feature of schema v5 -- who-per-verb transition guards
    on `status` -- must survive the v6 field-add byte-for-byte in shape.
    """
    status = {f["name"]: f for f in _schema("tasks")["fields"]}["status"]
    assert status["enum"] == [
        "draft", "open", "assigned", "waiting_on_client",
        "approved", "disputed", "cancelled",
    ]
    assert status["default"] == "open"
    transitions = status["transitions"]
    assert set(transitions.keys()) == {"draft", "open", "assigned", "waiting_on_client", "disputed"}

    draft_targets = {entry["to"] for entry in transitions["draft"]}
    assert draft_targets == {"open", "assigned", "cancelled"}
    assert all(entry["when"] == {"owner_id": "$user_id"} for entry in transitions["draft"])

    open_targets = {
        entry if isinstance(entry, str) else entry["to"] for entry in transitions["open"]
    }
    assert open_targets == {"assigned", "cancelled"}

    assigned_targets = {entry["to"] for entry in transitions["assigned"]}
    assert assigned_targets == {"waiting_on_client", "open", "cancelled"}

    waiting_targets = {
        entry if isinstance(entry, str) else entry["to"]
        for entry in transitions["waiting_on_client"]
    }
    assert waiting_targets == {"approved", "disputed", "assigned"}

    disputed_targets = {entry["to"] for entry in transitions["disputed"]}
    assert disputed_targets == {"assigned", "cancelled"}
    # approved and cancelled remain terminal (absent from the map).
    assert "approved" not in transitions
    assert "cancelled" not in transitions


def test_guarded_transition_enforcement_still_works_end_to_end(tmp_path):
    """Exercises the guard machinery against the live v6 schema file (not
    a stand-in), the same shape as test_object_import.py's schema-delta
    check, to prove the v6 field-add didn't disturb enforcement.
    """
    data_dir = tmp_path / "data"
    schema_path = data_dir / "schemas" / "tasks.json"
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text((APP_TASKS_DIR / "schemas" / "tasks.json").read_text())

    task = object_records.create_collection_record(
        "tasks",
        {"id": "t1", "title": "Draft task", "status": "draft", "task_type": "CONTRACT"},
        base_dir=data_dir,
        roots=[],
    )
    assert task["status"] == "draft"
    assert task["task_type"] == "CONTRACT"

    updated = object_records.update_collection_record(
        "tasks", "t1", {"status": "assigned"}, base_dir=data_dir, roots=[],
    )
    assert updated["status"] == "assigned"

    task2 = object_records.create_collection_record(
        "tasks", {"id": "t2", "title": "Another", "status": "assigned"}, base_dir=data_dir, roots=[],
    )
    with __import__("pytest").raises(object_records.InvalidRecordPayloadError):
        object_records.update_collection_record(
            "tasks", "t2", {"status": "draft"}, base_dir=data_dir, roots=[],
        )


def test_tasks_form_and_list_wire_the_new_fields():
    schema = _schema("tasks")
    assert schema["forms"]["default"]["fields"] == [
        "title", "description", "instructions", "project_id",
        "task_type", "template_id", "status", "urgency", "due_date", "assigned_to",
    ]
    assert "task_type" in schema["views"]["list_fields"]
    assert "instructions" in schema["search"]["fields"]


def test_create_task_record_accepts_new_work_fields(tmp_path):
    data_dir = tmp_path / "data"
    schema_path = data_dir / "schemas" / "tasks.json"
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text((APP_TASKS_DIR / "schemas" / "tasks.json").read_text())

    templates_schema_path = data_dir / "schemas" / "templates.json"
    templates_schema_path.write_text(
        (PACKAGES_ROOT / "app-templates" / "schemas" / "templates.json").read_text()
    )
    object_records.create_collection_record(
        "templates", {"id": "tpl_1", "name": "A Template", "body": "x"},
        base_dir=data_dir, roots=[],
    )

    record = object_records.create_collection_record(
        "tasks",
        {
            "id": "t3",
            "title": "Ship the feature",
            "task_type": "BASIC",
            "template_id": "tpl_1",
            "instructions": "Follow the checklist.",
            "metadata": json.dumps({"priority_score": 8}),
        },
        base_dir=data_dir,
        roots=[],
    )
    assert record["task_type"] == "BASIC"
    assert record["template_id"] == "tpl_1"
    assert json.loads(record["metadata"])["priority_score"] == 8

    # A v5-style record with none of the new fields must still be valid
    # (additive fields read back as empty/default, never required) and
    # task_type falls back to its schema default.
    legacy = object_records.create_collection_record(
        "tasks", {"id": "t4", "title": "Old-style task"}, base_dir=data_dir, roots=[],
    )
    assert legacy["task_type"] == "STANDARD"
    assert legacy["template_id"] == ""
    assert legacy["metadata"] == ""


def test_invalid_task_type_is_rejected(tmp_path):
    data_dir = tmp_path / "data"
    schema_path = data_dir / "schemas" / "tasks.json"
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text((APP_TASKS_DIR / "schemas" / "tasks.json").read_text())

    with __import__("pytest").raises(object_records.InvalidRecordPayloadError):
        object_records.create_collection_record(
            "tasks", {"id": "t5", "title": "Bad", "task_type": "URGENT"},
            base_dir=data_dir, roots=[],
        )


def test_seed_tsv_is_header_only_and_omits_only_the_stamped_created_at_field():
    """tasks.tsv ships header-only (established precedent: app-notes,
    app-contacts, app-invoices all ship header-only seeds too). The header
    matches the schema's own field order for readability, the same as
    app-invoices' seed -- except `created_at`, which was already omitted
    from this seed file before v6 (it's the server-stamped read_only
    timestamp; nothing to seed) and stays omitted, unrelated to this
    change.
    """
    schema = _schema("tasks")
    path = APP_TASKS_DIR / "seed" / "tasks.tsv"
    lines = path.read_text().splitlines()
    assert len(lines) == 1, "tasks.tsv should be header-only"
    header = lines[0].split("\t")
    expected = [f["name"] for f in schema["fields"] if f["name"] != "created_at"]
    assert header == expected


def test_additive_field_adds_need_no_migration_entry():
    """This system has no migration *execution* at all yet -- every
    package in the repo ships an empty "migrations": [] array, and
    object_packages.py's own install-plan builder refuses to proceed if a
    package declares any migration ("Package migration execution is not
    implemented yet"). Records are schemaless TSV rows (object_records.py's
    _project_record fills any field missing from a row with ""), so an
    additive field-add is already fully handled by the schema change alone.
    """
    package = object_packages.get_package("app-tasks", root=PACKAGES_ROOT)
    assert package["migrations"] == []

    source = Path(object_packages.__file__).read_text()
    assert "Package migration execution is not implemented yet" in source


def _app_tasks_policy():
    payload = json.loads((APP_TASKS_DIR / "permissions" / "rules.json").read_text())
    return object_permissions.policy_from_dict({"access_mode": "role_based", "rules": payload["rules"]})


def test_owner_can_crud_own_task_with_new_fields():
    policy = _app_tasks_policy()
    subject = object_permissions.PermissionSubject(user_id="7")
    record = {"owner_id": "7", "title": "Mine", "task_type": "CONTRACT", "template_id": "tpl_1"}
    for action in (
        object_permissions.CREATE, object_permissions.READ,
        object_permissions.UPDATE, object_permissions.DELETE,
    ):
        decision = object_permissions.check_permission(
            subject, action, policy=policy, collection="tasks", record=record,
        )
        assert decision.allowed is True, f"tasks/{action} should be allowed for the owner"


def test_assignee_can_read_and_update_but_not_delete():
    policy = _app_tasks_policy()
    subject = object_permissions.PermissionSubject(user_id="9")
    record = {"owner_id": "7", "assigned_to": "9", "title": "Assigned to me"}

    for action in (object_permissions.READ, object_permissions.UPDATE):
        decision = object_permissions.check_permission(
            subject, action, policy=policy, collection="tasks", record=record,
        )
        assert decision.allowed is True

    decision = object_permissions.check_permission(
        subject, object_permissions.DELETE, policy=policy, collection="tasks", record=record,
    )
    assert decision.allowed is False


def test_anonymous_cannot_read_any_task():
    policy = _app_tasks_policy()
    record = {"owner_id": "7", "title": "Private"}
    decision = object_permissions.check_permission(
        None, object_permissions.READ, policy=policy, collection="tasks", record=record,
    )
    assert decision.allowed is False


def test_site_tasks_page_is_publicly_executable():
    policy = _app_tasks_policy()
    decision = object_permissions.check_permission(
        None, object_permissions.EXECUTE, policy=policy, object_id="site_tasks",
    )
    assert decision.allowed is True


def test_no_disallowed_org_names_leak_into_the_package():
    """Public repo hygiene: no internal org/codename references anywhere
    in this package's source.
    """
    # Built from fragments so this guard file itself stays clean of the very
    # internal names it forbids (otherwise the test would flag its own source).
    banned = re.compile(
        "|".join([r"\b" + "q" + "9" + r"\b", "ask" + "robots", r"\b" + "wo" + "ld" + r"\b"]),
        re.IGNORECASE,
    )
    for path in APP_TASKS_DIR.rglob("*"):
        if path.is_dir():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not banned.search(text), f"disallowed reference found in {path}"


def test_this_test_file_has_no_disallowed_org_names():
    banned = re.compile(
        "|".join([r"\b" + "q" + "9" + r"\b", "ask" + "robots", r"\b" + "wo" + "ld" + r"\b"]),
        re.IGNORECASE,
    )
    text = Path(__file__).read_text(encoding="utf-8", errors="ignore")
    assert not banned.search(text), "disallowed reference found in this test file itself"
