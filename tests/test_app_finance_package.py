"""Structural tests for packages/app-finance (double-entry accounting v1).

Mirrors the package/schema/permission testing conventions used for
packages/app-invoices in tests/test_app_invoices_package.py. Behavior
tests for the computed helpers (journal_totals/trial_balance) live in
tests/test_object_finance.py -- there is no HANDLES totals handler in
this package to test here (deliberate; see object_finance.py's module
docstring and dbbasic-package.json's description).
"""

import json
import re
from pathlib import Path

import object_packages
import object_permissions
import object_schemas

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_FINANCE_DIR = PACKAGES_ROOT / "app-finance"

# Field types that store money as a float/decimal rather than integer cents
# -- the doctrine this package must never violate (00-doctrine-and-contract.md).
_FLOAT_MONEY_TYPES = {"float", "number", "currency"}

_SCHEMA_NAMES = ("fin_accounts", "fin_journals", "fin_journal_lines", "fin_recurring")


def _schema(name):
    return json.loads((APP_FINANCE_DIR / "schemas" / f"{name}.json").read_text())


def test_get_package_normalizes_app_finance_manifest():
    package = object_packages.get_package("app-finance", root=PACKAGES_ROOT)

    assert package["id"] == "app-finance"
    assert package["name"] == "Finance"
    assert {schema["collection"] for schema in package["schemas"]} == set(_SCHEMA_NAMES)
    # site_setup_accounts (65 slice 4) added the Setup Accounts action object.
    assert {obj["id"] for obj in package["objects"]} == {
        "site_accounts", "site_journals", "site_journal_view", "site_trial_balance",
        "site_setup_accounts",
    }
    assert package["permissions"] == [{"path": "permissions/rules.json"}]
    # + a site_routes seed for the /finance/setup-accounts route (65 slice 4).
    assert {entry["collection"] for entry in package["seed"]} == set(_SCHEMA_NAMES) | {"site_routes"}


def test_dry_run_app_finance_package_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()

    plan = object_packages.dry_run_package(
        "app-finance",
        root=PACKAGES_ROOT,
        base_dir=tmp_path / "data",
        object_roots=[object_root],
    )

    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []
    assert {schema["collection"] for schema in plan["schemas"]} == set(_SCHEMA_NAMES)


def test_install_app_finance_package_loads_schemas(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()

    object_packages.install_package(
        "app-finance", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root],
    )

    for name in _SCHEMA_NAMES:
        schema = object_schemas.get_schema(name, base_dir=data_dir)
        assert schema["name"] == name

    assert (object_root / "site" / "accounts.py").is_file()
    assert (object_root / "site" / "journals.py").is_file()
    assert (object_root / "site" / "journal_view.py").is_file()
    assert (object_root / "site" / "trial_balance.py").is_file()


def test_schema_json_files_are_valid_and_versioned():
    # Every finance collection gained an entity_id scoping FK (65 multi-entity)
    # -- an additive relation field -- so each version bumped by one from its
    # prior value: fin_accounts 2->3, fin_journals 2->3, the rest 1->2.
    for name in _SCHEMA_NAMES:
        payload = _schema(name)
        assert payload["name"] == name
        # entity_id is present on every finance collection, a relation into
        # the entities collection (scoping FK, not composition).
        by_name = {f["name"]: f for f in payload["fields"]}
        assert by_name["entity_id"]["relation"]["collection"] == "entities", name
        if name == "fin_accounts":
            # hierarchy tree view (60) + generated_from (61) history, now + entity_id
            assert payload["version"] == 3
            assert payload["views"]["list_mode"] == "tree"
        elif name == "fin_journals":
            # generated_from (61) + entity_id (65)
            assert payload["version"] == 3
            assert payload["views"]["list_mode"] == "table"
        else:
            # fin_journal_lines / fin_recurring: 1 -> 2 (entity_id)
            assert payload["version"] == 2
            assert payload["views"]["list_mode"] == "table"


def test_no_money_field_uses_a_float_or_currency_type():
    for name in _SCHEMA_NAMES:
        for field in _schema(name)["fields"]:
            field_name = field["name"]
            field_type = field.get("type")
            if "_cents" in field_name:
                assert field_type == "integer", f"{name}.{field_name} must be type integer, got {field_type!r}"
            assert field_type not in _FLOAT_MONEY_TYPES, (
                f"{name}.{field_name} uses a float-shaped type {field_type!r}"
            )


def test_fin_accounts_has_the_five_real_account_types():
    by_name = {f["name"]: f for f in _schema("fin_accounts")["fields"]}
    account_type = by_name["account_type"]
    assert account_type["type"] == "enum"
    assert account_type["enum"] == ["asset", "liability", "equity", "income", "expense"]
    assert account_type["required"] is True


def test_fin_accounts_parent_id_is_a_self_relation():
    by_name = {f["name"]: f for f in _schema("fin_accounts")["fields"]}
    parent = by_name["parent_id"]
    assert parent["relation"]["collection"] == "fin_accounts"
    assert "required" not in parent or not parent["required"]


def test_fin_accounts_search_fields_match_the_brief():
    schema = _schema("fin_accounts")
    assert schema["search"]["fields"] == ["name", "code"]


def test_fin_journals_status_transition_is_draft_to_posted_owner_gated_only():
    """No balance check anywhere in this guard -- posting is a bare status
    flip, matching the source's own gap exactly (see dbbasic-package.json).
    """
    by_name = {f["name"]: f for f in _schema("fin_journals")["fields"]}
    status = by_name["status"]
    assert status["type"] == "enum"
    assert status["enum"] == ["draft", "posted"]
    assert status["default"] == "draft"

    transitions = status["transitions"]
    assert transitions == {
        "draft": [{"to": "posted", "when": {"owner_id": "$user_id"}}],
    }
    # posted is terminal: no entry in the transitions map at all.
    assert "posted" not in transitions


def test_fin_journals_status_help_documents_no_balance_enforcement():
    by_name = {f["name"]: f for f in _schema("fin_journals")["fields"]}
    help_text = by_name["status"]["help"].lower()
    assert "not enforced" in help_text or "does not check" in help_text or "does not enforce" in help_text \
        or "does not" in help_text


def test_fin_journal_lines_uses_append_storage():
    schema = _schema("fin_journal_lines")
    assert schema["storage"] == "append"


def test_fin_journal_lines_debit_and_credit_are_integer_cents():
    by_name = {f["name"]: f for f in _schema("fin_journal_lines")["fields"]}
    for name in ("debit_cents", "credit_cents"):
        assert by_name[name]["type"] == "integer"
        assert by_name[name]["default"] == "0"


def test_fin_journal_lines_has_no_search_config():
    schema = _schema("fin_journal_lines")
    assert "search" not in schema


def test_fin_journal_lines_relations_point_at_journals_and_accounts():
    by_name = {f["name"]: f for f in _schema("fin_journal_lines")["fields"]}
    assert by_name["journal_id"]["relation"]["collection"] == "fin_journals"
    assert by_name["journal_id"]["required"] is True
    assert by_name["account_id"]["relation"]["collection"] == "fin_accounts"
    assert by_name["account_id"]["required"] is True


def test_fin_recurring_fields_match_the_brief():
    schema = _schema("fin_recurring")
    by_name = {f["name"]: f for f in schema["fields"]}
    assert by_name["frequency"]["enum"] == ["daily", "weekly", "monthly", "quarterly", "yearly"]
    assert by_name["frequency"]["default"] == "monthly"
    assert by_name["auto_post"]["type"] == "boolean"
    assert by_name["auto_post"]["default"] == "false"
    assert by_name["is_active"]["type"] == "boolean"
    assert by_name["is_active"]["default"] == "true"
    assert by_name["template_lines"]["type"] == "textarea"
    assert "next_run" in by_name
    assert "last_run" in by_name


def test_every_schema_has_created_at_read_only_and_owner_id():
    for name in _SCHEMA_NAMES:
        by_name = {f["name"]: f for f in _schema(name)["fields"]}
        assert by_name["created_at"]["read_only"] is True
        assert by_name["owner_id"]["type"] == "text"


def test_seed_tsvs_have_no_data_rows_and_match_schema_field_order():
    """Header-only seeds, matching the established precedent (app-invoices,
    app-orders, app-catalog, app-tasks, app-notes, app-contacts all ship
    header-only seeds).
    """
    for name in _SCHEMA_NAMES:
        schema = _schema(name)
        path = APP_FINANCE_DIR / "seed" / f"{name}.tsv"
        lines = path.read_text().splitlines()
        assert len(lines) == 1, f"{name}.tsv should be header-only"
        header = lines[0].split("\t")
        assert header == [f["name"] for f in schema["fields"]]


def _app_finance_policy():
    payload = json.loads((APP_FINANCE_DIR / "permissions" / "rules.json").read_text())
    return object_permissions.policy_from_dict({"access_mode": "role_based", "rules": payload["rules"]})


def test_owner_can_crud_own_records_in_every_collection():
    policy = _app_finance_policy()
    subject = object_permissions.PermissionSubject(user_id="7")
    records = {
        "fin_accounts": {"owner_id": "7", "name": "Cash", "account_type": "asset"},
        "fin_journals": {"owner_id": "7", "date": "2026-07-01", "status": "draft"},
        "fin_journal_lines": {"owner_id": "7", "journal_id": "j_1", "account_id": "a_1"},
        "fin_recurring": {"owner_id": "7", "name": "Rent"},
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
    policy = _app_finance_policy()
    subject = object_permissions.PermissionSubject(user_id="8")
    records = {
        "fin_accounts": {"owner_id": "7", "name": "Cash", "account_type": "asset"},
        "fin_journals": {"owner_id": "7", "date": "2026-07-01", "status": "draft"},
        "fin_journal_lines": {"owner_id": "7", "journal_id": "j_1", "account_id": "a_1"},
        "fin_recurring": {"owner_id": "7", "name": "Rent"},
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
    policy = _app_finance_policy()
    records = {
        "fin_accounts": {"owner_id": "7", "name": "Cash", "account_type": "asset"},
        "fin_journals": {"owner_id": "7", "date": "2026-07-01", "status": "draft"},
        "fin_journal_lines": {"owner_id": "7", "journal_id": "j_1", "account_id": "a_1"},
        "fin_recurring": {"owner_id": "7", "name": "Rent"},
    }
    for collection, record in records.items():
        decision = object_permissions.check_permission(
            None, object_permissions.READ, policy=policy, collection=collection, record=record,
        )
        assert decision.allowed is False


def test_site_pages_are_publicly_executable():
    """Public execute on the *page objects* (they show a sign-in prompt to
    visitors), never public read on a collection -- same split app-invoices
    and app-catalog use.
    """
    policy = _app_finance_policy()
    for object_id in ("site_accounts", "site_journals", "site_journal_view", "site_trial_balance"):
        decision = object_permissions.check_permission(
            None, object_permissions.EXECUTE, policy=policy, object_id=object_id,
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
    for path in APP_FINANCE_DIR.rglob("*"):
        if path.is_dir():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not banned.search(text), f"disallowed reference found in {path}"


def test_object_finance_module_has_no_disallowed_org_names():
    """object_finance.py lives at the repo root, not inside packages/
    app-finance/ (see dbbasic-package.json's description), so the repo-
    hygiene sweep above never walks it -- covered here explicitly instead.
    """
    banned = re.compile(
        "|".join([r"\b" + "q" + "9" + r"\b", "ask" + "robots", r"\b" + "wo" + "ld" + r"\b"]),
        re.IGNORECASE,
    )
    path = Path(__file__).resolve().parents[1] / "object_finance.py"
    text = path.read_text(encoding="utf-8", errors="ignore")
    assert not banned.search(text), f"disallowed reference found in {path}"
