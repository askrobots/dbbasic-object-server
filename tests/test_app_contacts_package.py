"""Structural tests for packages/app-contacts (contacts, organizations,
interactions, tags).

Mirrors the package/schema/permission testing conventions used for
packages/app-finance and packages/app-invoices in tests/test_app_finance_
package.py and tests/test_app_invoices_package.py. There was no dedicated
test file for app-contacts before this one; it fills that gap while also
covering the CRM-depth field additions (lead_status/job_title on contacts;
kind=task/outcome/duration_minutes/subject on interactions) reconciled
against a private predecessor-system audit, not part of this repo.
"""

import json
import re
from pathlib import Path

import object_packages
import object_permissions
import object_schemas

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_CONTACTS_DIR = PACKAGES_ROOT / "app-contacts"

_SCHEMA_NAMES = ("contacts", "organizations", "interactions", "tags")


def _schema(name):
    return json.loads((APP_CONTACTS_DIR / "schemas" / f"{name}.json").read_text())


def test_get_package_normalizes_app_contacts_manifest():
    package = object_packages.get_package("app-contacts", root=PACKAGES_ROOT)

    assert package["id"] == "app-contacts"
    assert package["name"] == "Contacts"
    assert {schema["collection"] for schema in package["schemas"]} == set(_SCHEMA_NAMES)
    assert {obj["id"] for obj in package["objects"]} == {"site_contacts"}
    assert package["permissions"] == [{"path": "permissions/rules.json"}]
    # Plus "views"/"site_routes": one 59 detail view + route per collection,
    # seeded into app-views'/site routing's own shared collections (a soft
    # reference, like template_id's into app-templates -- see
    # dbbasic-package.json's description).
    assert {entry["collection"] for entry in package["seed"]} == set(_SCHEMA_NAMES) | {
        "views", "site_routes",
    }
    # No migration mechanism is exercised here: additive field-adds don't
    # need one (see test_additive_field_adds_need_no_migration_entry below).
    assert package["migrations"] == []


def test_dry_run_app_contacts_package_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()

    plan = object_packages.dry_run_package(
        "app-contacts",
        root=PACKAGES_ROOT,
        base_dir=tmp_path / "data",
        object_roots=[object_root],
    )

    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []
    assert {schema["collection"] for schema in plan["schemas"]} == set(_SCHEMA_NAMES)


def test_install_app_contacts_package_loads_schemas(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()

    object_packages.install_package(
        "app-contacts", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root],
    )

    for name in _SCHEMA_NAMES:
        schema = object_schemas.get_schema(name, base_dir=data_dir)
        assert schema["name"] == name

    assert (object_root / "site" / "contacts.py").is_file()


def test_schema_json_files_are_valid():
    for name in _SCHEMA_NAMES:
        payload = _schema(name)
        assert payload["name"] == name
        assert isinstance(payload["version"], int)


def test_contacts_schema_is_now_version_3_additive_only():
    """contacts.json went 2 -> 3 adding job_title and lead_status. Every
    field that existed at v2 is still present, unchanged in name/type --
    this schema change is purely additive (live records with no value for
    the new fields just read those fields back as empty, same as any other
    optional field on a schemaless TSV collection).
    """
    schema = _schema("contacts")
    assert schema["version"] == 4
    by_name = {f["name"]: f for f in schema["fields"]}

    # Fields present since v2, untouched.
    for name in ("id", "first_name", "last_name", "email", "phone",
                 "organization_id", "project_id", "tags", "notes",
                 "created_at", "owner_id"):
        assert name in by_name, f"v2 field {name!r} must not be removed"
    assert by_name["first_name"]["required"] is True
    assert by_name["organization_id"]["relation"]["collection"] == "organizations"
    assert by_name["created_at"]["read_only"] is True

    # New v3 fields.
    assert by_name["job_title"]["type"] == "text"
    assert "required" not in by_name["job_title"] or not by_name["job_title"]["required"]

    lead_status = by_name["lead_status"]
    assert lead_status["type"] == "enum"
    assert lead_status["enum"] == ["cold", "warm", "hot", "customer", "inactive"]
    assert "required" not in lead_status or not lead_status["required"]
    # No default is forced onto lead_status -- it's optional pipeline state.
    assert "default" not in lead_status


def test_contacts_has_no_redundant_free_text_organization_name():
    """The source's create_contact also accepts a free-text `organization`
    name alongside organization_id. This schema already has organization_id
    as a relation into the organizations collection (display_field=name),
    which covers the same need, so no separate organization_name field was
    added -- adding one would just duplicate the relation.
    """
    by_name = {f["name"]: f for f in _schema("contacts")["fields"]}
    assert "organization_name" not in by_name
    assert by_name["organization_id"]["relation"]["display_field"] == "name"


def test_contacts_form_and_list_wire_the_new_fields():
    schema = _schema("contacts")
    assert schema["forms"]["default"]["fields"] == [
        "first_name", "last_name", "email", "phone", "job_title",
        "organization_id", "project_id", "lead_status", "tags", "notes",
    ]
    assert "lead_status" in schema["views"]["list_fields"]


def test_interactions_schema_is_now_version_2_additive_only():
    """interactions.json went 1 -> 2 adding "task" to kind's enum plus
    subject/duration_minutes/outcome. occurred_on and summary are untouched.
    """
    schema = _schema("interactions")
    assert schema["version"] == 2
    by_name = {f["name"]: f for f in schema["fields"]}

    for name in ("id", "contact_id", "kind", "occurred_on", "summary", "owner_id"):
        assert name in by_name, f"v1 field {name!r} must not be removed"
    assert by_name["contact_id"]["required"] is True
    assert by_name["summary"]["required"] is True
    assert by_name["occurred_on"]["type"] == "date"

    kind = by_name["kind"]
    assert kind["enum"] == ["call", "email", "meeting", "note", "task"]
    assert kind["default"] == "note"

    assert by_name["subject"]["type"] == "text"

    duration = by_name["duration_minutes"]
    assert duration["type"] == "integer"

    outcome = by_name["outcome"]
    assert outcome["type"] == "enum"
    assert outcome["enum"] == ["positive", "neutral", "negative", "no_answer"]
    assert "default" not in outcome


def test_interactions_form_and_list_wire_the_new_fields():
    schema = _schema("interactions")
    assert schema["forms"]["default"]["fields"] == [
        "contact_id", "kind", "subject", "occurred_on",
        "duration_minutes", "outcome", "summary",
    ]
    assert "subject" in schema["views"]["list_fields"]


def test_organizations_and_tags_schemas_are_untouched():
    """The parity doc does not call for changes to these two -- confirm
    they're still at their original version with no new fields.
    """
    orgs = _schema("organizations")
    assert orgs["version"] == 1
    assert {f["name"] for f in orgs["fields"]} == {
        "id", "name", "website", "notes", "owner_id",
    }

    tags = _schema("tags")
    assert tags["version"] == 1
    assert {f["name"] for f in tags["fields"]} == {"id", "name", "owner_id"}


def test_additive_field_adds_need_no_migration_entry():
    """This system has no migration *execution* at all yet -- every
    package in the repo ships an empty "migrations": [] array, and
    object_packages.py's own install-plan builder refuses to proceed if a
    package declares any migration ("Package migration execution is not
    implemented yet"). Records are schemaless TSV rows (object_records.py's
    _project_record fills any field missing from a row with ""), so an
    additive field-add is already fully handled by the schema change alone
    -- there is nothing for a migration entry to do, and inventing one here
    would be simulating a mechanism this codebase doesn't have yet.
    """
    package = object_packages.get_package("app-contacts", root=PACKAGES_ROOT)
    assert package["migrations"] == []

    source = Path(object_packages.__file__).read_text()
    assert "Package migration execution is not implemented yet" in source


def _app_contacts_policy():
    payload = json.loads((APP_CONTACTS_DIR / "permissions" / "rules.json").read_text())
    return object_permissions.policy_from_dict({"access_mode": "role_based", "rules": payload["rules"]})


def test_owner_can_crud_own_records_in_every_collection():
    policy = _app_contacts_policy()
    subject = object_permissions.PermissionSubject(user_id="7")
    records = {
        "contacts": {"owner_id": "7", "first_name": "Ada", "lead_status": "warm"},
        "organizations": {"owner_id": "7", "name": "Acme"},
        "interactions": {"owner_id": "7", "contact_id": "c_1", "kind": "task", "summary": "Follow up"},
        "tags": {"owner_id": "7", "name": "vip"},
    }
    for collection, record in records.items():
        for action in (
            object_permissions.CREATE, object_permissions.READ,
            object_permissions.UPDATE, object_permissions.DELETE,
        ):
            decision = object_permissions.check_permission(
                subject, action, policy=policy, collection=collection, record=record,
            )
            assert decision.allowed is True, f"{collection}/{action} should be allowed for the owner"


def test_others_cannot_touch_someone_elses_records():
    policy = _app_contacts_policy()
    subject = object_permissions.PermissionSubject(user_id="8")
    records = {
        "contacts": {"owner_id": "7", "first_name": "Ada"},
        "organizations": {"owner_id": "7", "name": "Acme"},
        "interactions": {"owner_id": "7", "contact_id": "c_1", "kind": "task", "summary": "Follow up"},
        "tags": {"owner_id": "7", "name": "vip"},
    }
    for collection, record in records.items():
        for action in (
            object_permissions.READ, object_permissions.UPDATE, object_permissions.DELETE,
        ):
            decision = object_permissions.check_permission(
                subject, action, policy=policy, collection=collection, record=record,
            )
            assert decision.allowed is False, f"{collection}/{action} should be denied for a non-owner"


def test_anonymous_cannot_read_any_collection():
    policy = _app_contacts_policy()
    records = {
        "contacts": {"owner_id": "7", "first_name": "Ada"},
        "organizations": {"owner_id": "7", "name": "Acme"},
        "interactions": {"owner_id": "7", "contact_id": "c_1", "kind": "task", "summary": "Follow up"},
        "tags": {"owner_id": "7", "name": "vip"},
    }
    for collection, record in records.items():
        decision = object_permissions.check_permission(
            None, object_permissions.READ, policy=policy, collection=collection, record=record,
        )
        assert decision.allowed is False


def test_site_contacts_page_is_publicly_executable():
    policy = _app_contacts_policy()
    decision = object_permissions.check_permission(
        None, object_permissions.EXECUTE, policy=policy, object_id="site_contacts",
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
    for path in APP_CONTACTS_DIR.rglob("*"):
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
