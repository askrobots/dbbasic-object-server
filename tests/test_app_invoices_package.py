"""Structural tests for packages/app-invoices (invoices, quotes, credit notes).

Mirrors the package/schema/permission testing conventions used for
packages/app-notes and packages/app-tasks in tests/test_app_settings_package.py
and tests/test_app_views_package.py. Behavior tests for the invoice_totals
HANDLES handler live in tests/test_app_invoices_totals.py.
"""

import json
import re
from pathlib import Path

import object_packages
import object_permissions
import object_schemas

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_INVOICES_DIR = PACKAGES_ROOT / "app-invoices"

# Field types that store money as a float/decimal rather than integer cents
# -- the doctrine this package must never violate (00-doctrine-and-contract.md,
# plan/vocabulary/20-invoice-spec.md's "Money, stated as a hard rule" section).
_FLOAT_MONEY_TYPES = {"float", "number", "currency"}

# quantity is the one deliberate exception: a count/measure (e.g. 2.5
# hours), not currency.
_ALLOWED_FLOAT_FIELDS = {"quantity"}


def _invoices_schema():
    return json.loads((APP_INVOICES_DIR / "schemas" / "invoices.json").read_text())


def _invoice_lines_schema():
    return json.loads((APP_INVOICES_DIR / "schemas" / "invoice_lines.json").read_text())


def test_get_package_normalizes_app_invoices_manifest():
    package = object_packages.get_package("app-invoices", root=PACKAGES_ROOT)

    assert package["id"] == "app-invoices"
    assert package["name"] == "Invoices"
    assert {schema["collection"] for schema in package["schemas"]} == {
        "invoices",
        "invoice_lines",
    }
    assert {obj["id"] for obj in package["objects"]} == {
        "site_invoices",
        "system_invoice_totals",
        "system_invoice_aging",  # aging + dunning runner (payments slice 2)
    }
    assert package["permissions"] == [{"path": "permissions/rules.json"}]
    assert {entry["collection"] for entry in package["seed"]} == {
        "invoices",
        "invoice_lines",
        "views",
        "site_routes",
      "notify_rules",
    }


def test_dry_run_app_invoices_package_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()

    plan = object_packages.dry_run_package(
        "app-invoices",
        root=PACKAGES_ROOT,
        base_dir=tmp_path / "data",
        object_roots=[object_root],
    )

    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []
    assert {schema["collection"] for schema in plan["schemas"]} == {
        "invoices",
        "invoice_lines",
    }


def test_install_app_invoices_package_loads_schemas(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()

    object_packages.install_package(
        "app-invoices",
        root=PACKAGES_ROOT,
        base_dir=data_dir,
        object_roots=[object_root],
    )

    invoices_schema = object_schemas.get_schema("invoices", base_dir=data_dir)
    lines_schema = object_schemas.get_schema("invoice_lines", base_dir=data_dir)

    assert invoices_schema["name"] == "invoices"
    assert lines_schema["name"] == "invoice_lines"
    assert (object_root / "site" / "invoices.py").is_file()
    assert (object_root / "system" / "invoice_totals.py").is_file()


def test_schema_json_files_are_valid_and_versioned():
    for name in ("invoices", "invoice_lines"):
        payload = json.loads((APP_INVOICES_DIR / "schemas" / f"{name}.json").read_text())
        assert payload["name"] == name
        # invoices v2: paid/balance became derived (app-payments rollups/formulas)
        assert payload["version"] == (3 if name == "invoices" else 1)
        assert payload["views"]["list_mode"] == "table"


def test_no_money_field_uses_a_float_or_currency_type():
    """00-doctrine-and-contract.md's hard rule: money is *_cents integers,
    never object_records.py's _FLOAT_TYPES = {"float", "number", "currency"}.
    quantity is the one documented, deliberate exception -- it is a count,
    not currency.
    """
    for schema in (_invoices_schema(), _invoice_lines_schema()):
        for field in schema["fields"]:
            name = field["name"]
            field_type = field.get("type")
            if name in _ALLOWED_FLOAT_FIELDS:
                continue
            if "_cents" in name or name in {"balance_due_cents"}:
                assert field_type in ("integer", "computed"), f"{schema['name']}.{name} must be integer or computed-over-cents, got {field_type!r}"
            assert field_type not in _FLOAT_MONEY_TYPES, (
                f"{schema['name']}.{name} uses a float-shaped type {field_type!r}"
            )


def test_every_cents_field_is_present_and_integer():
    invoices_by_name = {f["name"]: f for f in _invoices_schema()["fields"]}
    for name in (
        "subtotal_cents", "tax_cents", "total_cents",
        "amount_paid_cents", "balance_due_cents",
    ):
        expected = "computed" if name in ("amount_paid_cents", "balance_due_cents", "payments_received_cents", "refunded_cents") else "integer"
        assert invoices_by_name[name]["type"] == expected
        if expected == "integer":
            assert invoices_by_name[name]["default"] == "0"

    lines_by_name = {f["name"]: f for f in _invoice_lines_schema()["fields"]}
    for name in ("unit_price_cents", "line_total_cents", "line_tax_cents"):
        assert lines_by_name[name]["type"] == "integer"


def test_stamped_totals_fields_are_not_schema_read_only():
    """Deliberate deviation from the task brief, documented in
    dbbasic-package.json and objects/system/invoice_totals.py's module
    docstring: update_collection_record has no read_only write exception,
    so a genuinely read_only field could never be re-stamped by the
    totals handler after the first write. These fields stay ordinary
    owner-writable fields and are protected by omission from
    forms.default instead (UI-only, matches
    plan/vocabulary/20-invoice-spec.md's own already-reasoned posture).
    """
    invoices_by_name = {f["name"]: f for f in _invoices_schema()["fields"]}
    for name in ("subtotal_cents", "tax_cents", "total_cents", "balance_due_cents"):
        assert not invoices_by_name[name].get("read_only")
        assert name not in _invoices_schema()["forms"]["default"]["fields"]

    lines_by_name = {f["name"]: f for f in _invoice_lines_schema()["fields"]}
    for name in ("line_total_cents", "line_tax_cents"):
        assert not lines_by_name[name].get("read_only")
        assert name not in _invoice_lines_schema()["forms"]["default"]["fields"]

    # invoice_lines.invoice_id: required at creation, so it also cannot be
    # schema read_only (the server rejects any client submission of a
    # read_only field, including the first one) -- see the field's own
    # "help" text and the schema test below.
    assert lines_by_name["invoice_id"]["required"] is True
    assert not lines_by_name["invoice_id"].get("read_only")

    # created_at is the one field that IS read_only in both schemas: it is
    # special-cased server-side (_apply_auto_created_at), unlike every
    # other read_only field.
    assert invoices_by_name["created_at"]["read_only"] is True
    assert lines_by_name["created_at"]["read_only"] is True


def test_invoices_guarded_status_transitions_match_the_brief():
    status_field = next(f for f in _invoices_schema()["fields"] if f["name"] == "status")
    assert status_field["enum"] == ["draft", "sent", "paid", "partial", "overdue", "void"]
    assert status_field["default"] == "draft"

    transitions = status_field["transitions"]
    owner_guard = {"owner_id": "$user_id"}

    draft_targets = {entry["to"]: entry["when"] for entry in transitions["draft"]}
    assert draft_targets == {"sent": owner_guard, "void": owner_guard}

    sent_targets = {entry["to"]: entry["when"] for entry in transitions["sent"]}
    assert sent_targets == {
        "paid": owner_guard, "partial": owner_guard,
        "overdue": owner_guard, "void": owner_guard,
    }

    partial_targets = {entry["to"]: entry["when"] for entry in transitions["partial"]}
    # + overdue (v3): a partially-paid invoice can age past due (slice 2)
    assert partial_targets == {"paid": owner_guard, "void": owner_guard,
                               "overdue": owner_guard}
    # v3: overdue is no longer terminal (clears to paid/partial, voidable),
    # and a refund can reopen a paid invoice -- the lifecycle arcs the
    # aging/status machines revealed.
    overdue_targets = {entry["to"]: entry["when"] for entry in transitions["overdue"]}
    assert overdue_targets == {"paid": owner_guard, "partial": owner_guard,
                               "void": owner_guard}
    paid_targets = {entry["to"]: entry["when"] for entry in transitions["paid"]}
    assert paid_targets == {"partial": owner_guard}

    # void is terminal; paid can reopen to partial when a refund lands
    # (v3 -- the arc the refund machinery revealed).
    assert "void" not in transitions


def test_invoices_forms_and_views_match_the_brief():
    schema = _invoices_schema()
    assert schema["forms"]["default"]["fields"] == [
        "doc_type", "number", "customer_id", "customer_name", "customer_email",
        "customer_address", "currency", "issue_date", "due_date", "status",
        "notes", "payment_link",
    ]
    assert schema["views"]["list_fields"] == [
        "number", "customer_name", "status", "total_cents", "due_date",
    ]
    assert schema["search"]["fields"] == ["number", "customer_name"]


def test_invoices_parity_fields_present():
    """Parity fields carried over from the predecessor system's invoice
    model, not present in the first-principles 20-invoice-spec.md: doc_type
    (invoice/credit_note/quote share one schema + numbering scheme there)
    and source_order_id (order -> invoice conversion provenance). Neither is
    fully built here (no counter, no orders package) but both fields exist
    so no future migration has to backfill.
    """
    by_name = {f["name"]: f for f in _invoices_schema()["fields"]}
    assert by_name["doc_type"]["type"] == "enum"
    assert by_name["doc_type"]["enum"] == ["invoice", "credit_note", "quote"]
    assert by_name["doc_type"]["default"] == "invoice"
    assert "source_order_id" in by_name


def test_invoice_lines_schema_matches_the_brief():
    schema = _invoice_lines_schema()
    field_names = [f["name"] for f in schema["fields"]]
    assert field_names == [
        "id", "invoice_id", "description", "quantity", "unit_price_cents",
        "line_total_cents", "tax_rate_bps", "line_tax_cents", "owner_id", "created_at",
    ]
    by_name = {f["name"]: f for f in schema["fields"]}
    assert by_name["invoice_id"]["relation"]["collection"] == "invoices"
    assert by_name["description"]["required"] is True
    assert by_name["quantity"]["type"] == "number"
    assert by_name["quantity"]["default"] == "1"
    assert by_name["unit_price_cents"]["required"] is True


def test_customer_snapshot_fields_present_alongside_optional_relation():
    """plan/vocabulary/20-invoice-spec.md's 'standalone-with-optional-
    relation, not a hard app-contacts coupling' decision: customer_id is
    an optional cross-reference; the snapshot fields are what renders.
    """
    by_name = {f["name"]: f for f in _invoices_schema()["fields"]}
    assert by_name["customer_id"]["relation"]["collection"] == "contacts"
    assert "required" not in by_name["customer_id"] or not by_name["customer_id"]["required"]
    assert by_name["customer_name"]["required"] is True


def _app_invoices_policy():
    payload = json.loads((APP_INVOICES_DIR / "permissions" / "rules.json").read_text())
    return object_permissions.policy_from_dict({"access_mode": "role_based", "rules": payload["rules"]})


def test_owner_can_crud_own_invoice():
    policy = _app_invoices_policy()
    subject = object_permissions.PermissionSubject(user_id="7")
    record = {"owner_id": "7", "number": "INV-0001", "customer_name": "Example Co", "status": "draft"}

    for action in (
        object_permissions.CREATE,
        object_permissions.READ,
        object_permissions.UPDATE,
        object_permissions.DELETE,
    ):
        decision = object_permissions.check_permission(
            subject, action, policy=policy, collection="invoices", record=record
        )
        assert decision.allowed is True


def test_others_cannot_touch_someone_elses_invoice():
    policy = _app_invoices_policy()
    subject = object_permissions.PermissionSubject(user_id="8")
    record = {"owner_id": "7", "number": "INV-0001", "customer_name": "Example Co", "status": "draft"}

    for action in (
        object_permissions.READ,
        object_permissions.UPDATE,
        object_permissions.DELETE,
    ):
        decision = object_permissions.check_permission(
            subject, action, policy=policy, collection="invoices", record=record
        )
        assert decision.allowed is False


def test_anonymous_cannot_read_any_invoice():
    """No public read rule is granted on the invoices collection at all --
    the tokenized public view is a later slice (see dbbasic-package.json).
    """
    policy = _app_invoices_policy()
    record = {"owner_id": "7", "number": "INV-0001", "customer_name": "Example Co", "status": "draft"}

    decision = object_permissions.check_permission(
        None, object_permissions.READ, policy=policy, collection="invoices", record=record
    )
    assert decision.allowed is False


def test_owner_can_crud_own_invoice_lines():
    policy = _app_invoices_policy()
    subject = object_permissions.PermissionSubject(user_id="7")
    record = {"owner_id": "7", "invoice_id": "inv_1", "description": "Consulting"}

    for action in (
        object_permissions.CREATE,
        object_permissions.READ,
        object_permissions.UPDATE,
        object_permissions.DELETE,
    ):
        decision = object_permissions.check_permission(
            subject, action, policy=policy, collection="invoice_lines", record=record
        )
        assert decision.allowed is True


def test_others_cannot_touch_someone_elses_invoice_lines():
    policy = _app_invoices_policy()
    subject = object_permissions.PermissionSubject(user_id="8")
    record = {"owner_id": "7", "invoice_id": "inv_1", "description": "Consulting"}

    for action in (
        object_permissions.READ,
        object_permissions.UPDATE,
        object_permissions.DELETE,
    ):
        decision = object_permissions.check_permission(
            subject, action, policy=policy, collection="invoice_lines", record=record
        )
        assert decision.allowed is False


def test_invoices_page_is_publicly_executable():
    """Public execute on the *page object* (it shows a sign-in prompt to
    visitors), never public read on the *collection* -- same split
    app-notes uses for site_notes. The invoice detail permalink is now a
    seeded 59 view rendered by site_view_render (app-views' own package),
    not a per-package object, so it has no rule here anymore -- see
    tests/test_app_invoices_detail_retrofit.py.
    """
    policy = _app_invoices_policy()

    decision = object_permissions.check_permission(
        None, object_permissions.EXECUTE, policy=policy, object_id="site_invoices"
    )
    assert decision.allowed is True


def test_seed_tsvs_have_no_data_rows_and_match_schema_field_order():
    """Both seed files ship header-only (no starter data), matching the
    established precedent (app-tasks, app-notes, app-contacts all ship
    header-only seeds; only app-settings/ai_prices.tsv, live operational
    config, ships rows). The header order matches the schema's own field
    order for readability.
    """
    for name, schema in (("invoices", _invoices_schema()), ("invoice_lines", _invoice_lines_schema())):
        path = APP_INVOICES_DIR / "seed" / f"{name}.tsv"
        lines = path.read_text().splitlines()
        assert len(lines) == 1, f"{name}.tsv should be header-only"
        header = lines[0].split("\t")
        assert header == [f["name"] for f in schema["fields"]]


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
    for path in APP_INVOICES_DIR.rglob("*"):
        if path.is_dir():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not banned.search(text), f"disallowed reference found in {path}"
