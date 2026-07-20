"""Structural tests for packages/app-orders (sales and purchase orders).

Mirrors the package/schema/permission testing conventions used for
packages/app-invoices in tests/test_app_invoices_package.py. Behavior
tests for the order_totals HANDLES handler live in
tests/test_app_orders_totals.py.
"""

import json
import re
from pathlib import Path

import object_packages
import object_permissions
import object_schemas

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_ORDERS_DIR = PACKAGES_ROOT / "app-orders"

# Field types that store money as a float/decimal rather than integer cents
# -- the doctrine this package must never violate (00-doctrine-and-contract.md).
_FLOAT_MONEY_TYPES = {"float", "number", "currency"}

# quantity is the one deliberate exception: a count/measure (e.g. 2.5
# hours), not currency.
_ALLOWED_FLOAT_FIELDS = {"quantity"}


def _orders_schema():
    return json.loads((APP_ORDERS_DIR / "schemas" / "orders.json").read_text())


def _order_lines_schema():
    return json.loads((APP_ORDERS_DIR / "schemas" / "order_lines.json").read_text())


def test_get_package_normalizes_app_orders_manifest():
    package = object_packages.get_package("app-orders", root=PACKAGES_ROOT)

    assert package["id"] == "app-orders"
    assert package["name"] == "Orders"
    assert {schema["collection"] for schema in package["schemas"]} == {
        "orders",
        "order_lines",
    }
    assert {obj["id"] for obj in package["objects"]} == {
        "site_orders",
        "site_order_view",
        "system_order_totals",
    }
    assert package["permissions"] == [{"path": "permissions/rules.json"}]
    assert {entry["collection"] for entry in package["seed"]} == {
        "orders",
        "order_lines",
    }


def test_dry_run_app_orders_package_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()

    plan = object_packages.dry_run_package(
        "app-orders",
        root=PACKAGES_ROOT,
        base_dir=tmp_path / "data",
        object_roots=[object_root],
    )

    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []
    assert {schema["collection"] for schema in plan["schemas"]} == {
        "orders",
        "order_lines",
    }


def test_install_app_orders_package_loads_schemas(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()

    object_packages.install_package(
        "app-orders", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root]
    )

    orders_schema = object_schemas.get_schema("orders", base_dir=data_dir)
    lines_schema = object_schemas.get_schema("order_lines", base_dir=data_dir)

    assert orders_schema["name"] == "orders"
    assert lines_schema["name"] == "order_lines"
    assert (object_root / "site" / "orders.py").is_file()
    assert (object_root / "site" / "order_view.py").is_file()
    assert (object_root / "system" / "order_totals.py").is_file()


def test_schema_json_files_are_valid_and_versioned():
    for name in ("orders", "order_lines"):
        payload = json.loads((APP_ORDERS_DIR / "schemas" / f"{name}.json").read_text())
        assert payload["name"] == name
        assert payload["version"] == 1
        assert payload["views"]["list_mode"] == "table"


def test_no_money_field_uses_a_float_or_currency_type():
    """00-doctrine-and-contract.md's hard rule: money is *_cents integers,
    never object_records.py's _FLOAT_TYPES = {"float", "number", "currency"}.
    quantity is the one documented, deliberate exception -- it is a count,
    not currency.
    """
    for schema in (_orders_schema(), _order_lines_schema()):
        for field in schema["fields"]:
            name = field["name"]
            field_type = field.get("type")
            if name in _ALLOWED_FLOAT_FIELDS:
                continue
            if "_cents" in name:
                assert field_type == "integer", f"{schema['name']}.{name} must be type integer, got {field_type!r}"
            assert field_type not in _FLOAT_MONEY_TYPES, (
                f"{schema['name']}.{name} uses a float-shaped type {field_type!r}"
            )


def test_every_cents_field_is_present_and_integer():
    orders_by_name = {f["name"]: f for f in _orders_schema()["fields"]}
    for name in ("subtotal_cents", "tax_cents", "total_cents"):
        assert orders_by_name[name]["type"] == "integer"
        assert orders_by_name[name]["default"] == "0"

    lines_by_name = {f["name"]: f for f in _order_lines_schema()["fields"]}
    for name in ("unit_price_cents", "line_total_cents", "line_tax_cents"):
        assert lines_by_name[name]["type"] == "integer"


def test_stamped_totals_fields_are_not_schema_read_only():
    """Deliberate deviation from the task brief, documented in
    dbbasic-package.json and objects/system/order_totals.py's module
    docstring: update_collection_record has no read_only write exception,
    so a genuinely read_only field could never be re-stamped by the
    totals handler after the first write. These fields stay ordinary
    owner-writable fields and are protected by omission from
    forms.default instead (same posture as app-invoices).
    """
    orders_by_name = {f["name"]: f for f in _orders_schema()["fields"]}
    for name in ("subtotal_cents", "tax_cents", "total_cents"):
        assert not orders_by_name[name].get("read_only")
        assert name not in _orders_schema()["forms"]["default"]["fields"]

    lines_by_name = {f["name"]: f for f in _order_lines_schema()["fields"]}
    for name in ("line_total_cents", "line_tax_cents"):
        assert not lines_by_name[name].get("read_only")
        assert name not in _order_lines_schema()["forms"]["default"]["fields"]

    # order_lines.order_id: required at creation, so it also cannot be
    # schema read_only (the server rejects any client submission of a
    # read_only field, including the first one) -- see the field's own
    # "help" text and the schema test below.
    assert lines_by_name["order_id"]["required"] is True
    assert not lines_by_name["order_id"].get("read_only")

    # created_at is the one field that IS read_only in both schemas: it is
    # special-cased server-side (_apply_auto_created_at), unlike every
    # other read_only field.
    assert orders_by_name["created_at"]["read_only"] is True
    assert lines_by_name["created_at"]["read_only"] is True


def test_orders_guarded_status_transitions_match_the_real_lifecycle():
    status_field = next(f for f in _orders_schema()["fields"] if f["name"] == "status")
    assert status_field["enum"] == [
        "draft", "confirmed", "processing", "shipped", "delivered", "cancelled",
    ]
    assert status_field["default"] == "draft"

    transitions = status_field["transitions"]
    owner_guard = {"owner_id": "$user_id"}

    draft_targets = {entry["to"]: entry["when"] for entry in transitions["draft"]}
    assert draft_targets == {"confirmed": owner_guard, "cancelled": owner_guard}

    confirmed_targets = {entry["to"]: entry["when"] for entry in transitions["confirmed"]}
    assert confirmed_targets == {"processing": owner_guard, "cancelled": owner_guard}

    processing_targets = {entry["to"]: entry["when"] for entry in transitions["processing"]}
    assert processing_targets == {"shipped": owner_guard, "cancelled": owner_guard}

    shipped_targets = {entry["to"]: entry["when"] for entry in transitions["shipped"]}
    assert shipped_targets == {"delivered": owner_guard}

    # delivered and cancelled are terminal: no entries in the transitions map at all.
    assert "delivered" not in transitions
    assert "cancelled" not in transitions


def test_orders_forms_and_views_match_the_brief():
    schema = _orders_schema()
    assert schema["forms"]["default"]["fields"] == [
        "doc_type", "number", "customer_id", "customer_name", "customer_email",
        "currency", "order_date", "expected_date", "status", "notes",
    ]
    assert schema["views"]["list_fields"] == [
        "number", "customer_name", "status", "total_cents", "expected_date",
    ]
    assert schema["search"]["fields"] == ["number", "customer_name"]


def test_orders_parity_fields_present():
    """Parity fields carried over from the predecessor system's Order model
    (reconciled against a private predecessor-system audit, not part of
    this repo: "Order+Lines (SO/PO; convert_to_invoice)"): doc_type is the real SO/PO split, and
    invoice_id is the order-to-invoice conversion provenance. Neither is
    fully built here (no counter, no convert action) but both fields
    exist so no future migration has to backfill.
    """
    by_name = {f["name"]: f for f in _orders_schema()["fields"]}
    assert by_name["doc_type"]["type"] == "enum"
    assert by_name["doc_type"]["enum"] == ["sale", "purchase"]
    assert by_name["doc_type"]["default"] == "sale"
    assert "invoice_id" in by_name


def test_order_lines_schema_matches_the_brief():
    schema = _order_lines_schema()
    field_names = [f["name"] for f in schema["fields"]]
    assert field_names == [
        "id", "order_id", "product_id", "description", "quantity", "unit_price_cents",
        "line_total_cents", "tax_rate_bps", "line_tax_cents", "owner_id", "created_at",
    ]
    by_name = {f["name"]: f for f in schema["fields"]}
    assert by_name["order_id"]["relation"]["collection"] == "orders"
    assert by_name["description"]["required"] is True
    assert by_name["quantity"]["type"] == "number"
    assert by_name["quantity"]["default"] == "1"
    assert by_name["unit_price_cents"]["required"] is True


def test_product_id_relation_present_and_optional():
    """Order lines relate to app-catalog's products collection, but the
    relation is optional -- a line may be free-text (task brief), and
    this package declares no dependency on app-catalog.
    """
    by_name = {f["name"]: f for f in _order_lines_schema()["fields"]}
    assert by_name["product_id"]["relation"]["collection"] == "products"
    assert "required" not in by_name["product_id"] or not by_name["product_id"]["required"]

    manifest = json.loads((APP_ORDERS_DIR / "dbbasic-package.json").read_text())
    assert manifest["dependencies"] == []


def test_customer_snapshot_fields_present_alongside_optional_relation():
    """Same 'standalone-with-optional-relation, not a hard app-contacts
    coupling' decision app-invoices makes: customer_id is an optional
    cross-reference; the snapshot fields are what renders.
    """
    by_name = {f["name"]: f for f in _orders_schema()["fields"]}
    assert by_name["customer_id"]["relation"]["collection"] == "contacts"
    assert "required" not in by_name["customer_id"] or not by_name["customer_id"]["required"]
    assert by_name["customer_name"]["required"] is True


def _app_orders_policy():
    payload = json.loads((APP_ORDERS_DIR / "permissions" / "rules.json").read_text())
    return object_permissions.policy_from_dict({"access_mode": "role_based", "rules": payload["rules"]})


def test_owner_can_crud_own_order():
    policy = _app_orders_policy()
    subject = object_permissions.PermissionSubject(user_id="7")
    record = {"owner_id": "7", "number": "SO-0001", "customer_name": "Example Co", "status": "draft"}

    for action in (
        object_permissions.CREATE,
        object_permissions.READ,
        object_permissions.UPDATE,
        object_permissions.DELETE,
    ):
        decision = object_permissions.check_permission(
            subject, action, policy=policy, collection="orders", record=record
        )
        assert decision.allowed is True


def test_others_cannot_touch_someone_elses_order():
    policy = _app_orders_policy()
    subject = object_permissions.PermissionSubject(user_id="8")
    record = {"owner_id": "7", "number": "SO-0001", "customer_name": "Example Co", "status": "draft"}

    for action in (
        object_permissions.READ,
        object_permissions.UPDATE,
        object_permissions.DELETE,
    ):
        decision = object_permissions.check_permission(
            subject, action, policy=policy, collection="orders", record=record
        )
        assert decision.allowed is False


def test_anonymous_cannot_read_any_order():
    """No public read rule is granted on the orders collection at all."""
    policy = _app_orders_policy()
    record = {"owner_id": "7", "number": "SO-0001", "customer_name": "Example Co", "status": "draft"}

    decision = object_permissions.check_permission(
        None, object_permissions.READ, policy=policy, collection="orders", record=record
    )
    assert decision.allowed is False


def test_owner_can_crud_own_order_lines():
    policy = _app_orders_policy()
    subject = object_permissions.PermissionSubject(user_id="7")
    record = {"owner_id": "7", "order_id": "ord_1", "description": "Widget"}

    for action in (
        object_permissions.CREATE,
        object_permissions.READ,
        object_permissions.UPDATE,
        object_permissions.DELETE,
    ):
        decision = object_permissions.check_permission(
            subject, action, policy=policy, collection="order_lines", record=record
        )
        assert decision.allowed is True


def test_others_cannot_touch_someone_elses_order_lines():
    policy = _app_orders_policy()
    subject = object_permissions.PermissionSubject(user_id="8")
    record = {"owner_id": "7", "order_id": "ord_1", "description": "Widget"}

    for action in (
        object_permissions.READ,
        object_permissions.UPDATE,
        object_permissions.DELETE,
    ):
        decision = object_permissions.check_permission(
            subject, action, policy=policy, collection="order_lines", record=record
        )
        assert decision.allowed is False


def test_orders_and_order_view_pages_are_publicly_executable():
    """Public execute on the *page objects* (they show a sign-in prompt to
    visitors), never public read on the *collection* -- same split
    app-invoices uses for site_invoices/site_invoice_view.
    """
    policy = _app_orders_policy()

    for object_id in ("site_orders", "site_order_view"):
        decision = object_permissions.check_permission(
            None, object_permissions.EXECUTE, policy=policy, object_id=object_id
        )
        assert decision.allowed is True


def test_seed_tsvs_have_no_data_rows_and_match_schema_field_order():
    """Both seed files ship header-only (no starter data), matching the
    established precedent (app-tasks, app-notes, app-contacts, app-invoices
    all ship header-only seeds). The header order matches the schema's own
    field order for readability.
    """
    for name, schema in (("orders", _orders_schema()), ("order_lines", _order_lines_schema())):
        path = APP_ORDERS_DIR / "seed" / f"{name}.tsv"
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
    for path in APP_ORDERS_DIR.rglob("*"):
        if path.is_dir():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not banned.search(text), f"disallowed reference found in {path}"
