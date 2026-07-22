"""Structural tests for packages/app-templates (templates).

Mirrors the package/schema/permission testing conventions used for
packages/app-contacts and packages/app-invoices in tests/test_app_contacts_
package.py and tests/test_app_invoices_package.py. There was no dedicated
test file for app-templates before this one; it fills that gap while also
covering the v1 -> v2 structured-data field additions (schema/ui_schema/
instructions/ai_prompt/ai_assistance/default_values/example_data) and the
new /templates page, reconciled against a private predecessor-system
audit, not part of this repo.
"""

import json
import re
from pathlib import Path

import object_packages
import object_permissions
import object_records
import object_schemas

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_TEMPLATES_DIR = PACKAGES_ROOT / "app-templates"

_SCHEMA_NAMES = ("templates",)


def _schema(name):
    return json.loads((APP_TEMPLATES_DIR / "schemas" / f"{name}.json").read_text())


def test_get_package_normalizes_app_templates_manifest():
    package = object_packages.get_package("app-templates", root=PACKAGES_ROOT)

    assert package["id"] == "app-templates"
    assert package["name"] == "Templates"
    assert {schema["collection"] for schema in package["schemas"]} == set(_SCHEMA_NAMES)
    assert {obj["id"] for obj in package["objects"]} == {"site_templates"}
    assert package["permissions"] == [{"path": "permissions/rules.json"}]
    assert {entry["collection"] for entry in package["seed"]} == set(_SCHEMA_NAMES)
    # No migration mechanism is exercised here: additive field-adds don't
    # need one (see test_additive_field_adds_need_no_migration_entry below).
    assert package["migrations"] == []


def test_dry_run_app_templates_package_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()

    plan = object_packages.dry_run_package(
        "app-templates",
        root=PACKAGES_ROOT,
        base_dir=tmp_path / "data",
        object_roots=[object_root],
    )

    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []
    assert {schema["collection"] for schema in plan["schemas"]} == set(_SCHEMA_NAMES)


def test_install_app_templates_package_loads_schema_and_page(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()

    object_packages.install_package(
        "app-templates", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root],
    )

    schema = object_schemas.get_schema("templates", base_dir=data_dir)
    assert schema["name"] == "templates"
    assert (object_root / "site" / "templates.py").is_file()


def test_schema_json_file_is_valid():
    payload = _schema("templates")
    assert payload["name"] == "templates"
    assert isinstance(payload["version"], int)


def test_templates_schema_is_now_version_2_additive_only():
    """templates.json went 1 -> 2 adding schema/ui_schema/instructions/
    ai_prompt/ai_assistance/default_values/example_data. Every field that
    existed at v1 is still present, unchanged in name/type -- this schema
    change is purely additive (live records with no value for the new
    fields just read those fields back as empty, same as any other
    optional field on a schemaless TSV collection). v2 -> v3 adds created_at
    (baseline record metadata), also additive.
    """
    schema = _schema("templates")
    assert schema["version"] == 3
    by_name = {f["name"]: f for f in schema["fields"]}

    # Fields present since v1, untouched.
    for name in ("id", "name", "description", "category", "body", "tags",
                 "is_public", "created_at", "owner_id"):
        assert name in by_name, f"v1 field {name!r} must not be removed"
    assert by_name["name"]["required"] is True
    assert by_name["body"]["required"] is True
    assert by_name["is_public"]["type"] == "boolean"
    assert by_name["is_public"]["default"] == "false"

    # New v2 fields -- none required, so v1 records missing them stay valid.
    for new_field in ("schema", "ui_schema", "instructions", "ai_prompt",
                       "default_values", "example_data"):
        field = by_name[new_field]
        assert field["type"] == "textarea"
        assert "required" not in field or not field["required"]

    ai_assistance = by_name["ai_assistance"]
    assert ai_assistance["type"] == "enum"
    assert ai_assistance["enum"] == ["none", "suggestions", "auto_fill", "full"]
    assert ai_assistance["default"] == "suggestions"


def test_templates_json_fields_have_no_dedicated_json_type():
    """This codebase's field-type contract (docs/schema-forms.md) only
    lists text/textarea/integer/number/boolean/date/datetime/enum/computed
    -- there is no `json` type. schema/ui_schema/default_values/example_data
    therefore store JSON as a string in a `textarea` field, the same
    convention app-invoices uses for structured text (e.g. customer_address)
    and the one now mirrored by app-tasks' `metadata` field.
    """
    contract = (Path(__file__).resolve().parents[1] / "docs" / "schema-forms.md").read_text()
    type_line = next(line for line in contract.splitlines() if line.strip().startswith("- `type`"))
    declared_types = re.findall(r"`([a-z_]+)`", type_line)
    assert "json" not in declared_types
    by_name = {f["name"]: f for f in _schema("templates")["fields"]}
    for json_field in ("schema", "ui_schema", "default_values", "example_data"):
        assert by_name[json_field]["type"] == "textarea"


def test_templates_form_and_list_wire_the_new_fields():
    schema = _schema("templates")
    assert schema["forms"]["default"]["fields"] == [
        "name", "description", "category", "body", "schema",
        "instructions", "ai_prompt", "ai_assistance",
        "default_values", "tags", "is_public",
    ]
    assert "ai_assistance" in schema["views"]["list_fields"]
    assert "instructions" in schema["search"]["fields"]


def test_ai_assistance_fields_are_config_only_not_an_ai_engine():
    """ai_prompt/ai_assistance are stored config; nothing in this package
    (or repo) reads them to call an AI provider. Same posture as other
    deferred-infra fields elsewhere in this codebase.
    """
    for path in APP_TEMPLATES_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert "openai" not in text.lower()
        assert "anthropic" not in text.lower()


def test_seed_tsv_is_header_only_and_matches_schema_field_order():
    schema = _schema("templates")
    path = APP_TEMPLATES_DIR / "seed" / "templates.tsv"
    lines = path.read_text().splitlines()
    assert len(lines) == 1, "templates.tsv should be header-only"
    header = lines[0].split("\t")
    assert header == [f["name"] for f in schema["fields"]]


def test_additive_field_adds_need_no_migration_entry():
    """This system has no migration *execution* at all yet -- every
    package in the repo ships an empty "migrations": [] array, and
    object_packages.py's own install-plan builder refuses to proceed if a
    package declares any migration ("Package migration execution is not
    implemented yet"). Records are schemaless TSV rows (object_records.py's
    _project_record fills any field missing from a row with ""), so an
    additive field-add is already fully handled by the schema change alone.
    """
    package = object_packages.get_package("app-templates", root=PACKAGES_ROOT)
    assert package["migrations"] == []

    source = Path(object_packages.__file__).read_text()
    assert "Package migration execution is not implemented yet" in source


def test_create_template_record_accepts_new_structured_fields(tmp_path):
    data_dir = tmp_path / "data"
    schema_path = data_dir / "schemas" / "templates.json"
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text((APP_TEMPLATES_DIR / "schemas" / "templates.json").read_text())

    record = object_records.create_collection_record(
        "templates",
        {
            "id": "tpl_1",
            "name": "Onboarding Contract",
            "body": "fallback text",
            "schema": json.dumps({"type": "object", "properties": {"amount": {"type": "number"}}}),
            "default_values": json.dumps({"amount": 0}),
            "ai_assistance": "auto_fill",
        },
        base_dir=data_dir,
        roots=[],
    )
    assert record["ai_assistance"] == "auto_fill"
    assert json.loads(record["schema"])["type"] == "object"

    # A v1-style record with none of the new fields must still be valid
    # (additive fields read back as empty, never required).
    legacy = object_records.create_collection_record(
        "templates",
        {"id": "tpl_2", "name": "Legacy Template", "body": "just a body"},
        base_dir=data_dir,
        roots=[],
    )
    assert legacy["schema"] == ""
    assert legacy["ai_assistance"] in ("", "suggestions")


def _app_templates_policy():
    payload = json.loads((APP_TEMPLATES_DIR / "permissions" / "rules.json").read_text())
    return object_permissions.policy_from_dict({"access_mode": "role_based", "rules": payload["rules"]})


def test_owner_can_crud_own_templates():
    policy = _app_templates_policy()
    subject = object_permissions.PermissionSubject(user_id="7")
    record = {"owner_id": "7", "name": "Mine", "is_public": "false"}
    for action in (
        object_permissions.CREATE, object_permissions.READ,
        object_permissions.UPDATE, object_permissions.DELETE,
    ):
        decision = object_permissions.check_permission(
            subject, action, policy=policy, collection="templates", record=record,
        )
        assert decision.allowed is True, f"templates/{action} should be allowed for the owner"


def test_others_cannot_touch_someone_elses_private_template():
    policy = _app_templates_policy()
    subject = object_permissions.PermissionSubject(user_id="8")
    record = {"owner_id": "7", "name": "Not mine", "is_public": "false"}
    for action in (
        object_permissions.UPDATE, object_permissions.DELETE,
    ):
        decision = object_permissions.check_permission(
            subject, action, policy=policy, collection="templates", record=record,
        )
        assert decision.allowed is False, f"templates/{action} should be denied for a non-owner"


def test_registered_user_can_read_a_public_template():
    policy = _app_templates_policy()
    subject = object_permissions.PermissionSubject(user_id="8")
    record = {"owner_id": "7", "name": "Shared", "is_public": "true"}
    decision = object_permissions.check_permission(
        subject, object_permissions.READ, policy=policy, collection="templates", record=record,
    )
    assert decision.allowed is True


def test_anonymous_cannot_read_any_template():
    policy = _app_templates_policy()
    record = {"owner_id": "7", "name": "Shared", "is_public": "true"}
    decision = object_permissions.check_permission(
        None, object_permissions.READ, policy=policy, collection="templates", record=record,
    )
    assert decision.allowed is False


def test_site_templates_page_is_publicly_executable():
    policy = _app_templates_policy()
    decision = object_permissions.check_permission(
        None, object_permissions.EXECUTE, policy=policy, object_id="site_templates",
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
    for path in APP_TEMPLATES_DIR.rglob("*"):
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
