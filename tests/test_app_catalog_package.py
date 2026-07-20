"""Structural tests for packages/app-catalog (products, v1).

Mirrors the package/schema/permission testing conventions used for
packages/app-invoices in tests/test_app_invoices_package.py. v1 is
products only -- orders, order lines, stock, and locations are a later
slice (see dbbasic-package.json's description; reconciled against a
private predecessor-system catalog audit, not part of this repo).
"""

import json
import re
from pathlib import Path

import object_packages
import object_permissions
import object_schemas

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_CATALOG_DIR = PACKAGES_ROOT / "app-catalog"

# Field types that store money as a float/decimal rather than integer cents
# -- the doctrine this package must never violate (00-doctrine-and-contract.md).
_FLOAT_MONEY_TYPES = {"float", "number", "currency"}


def _products_schema():
    return json.loads((APP_CATALOG_DIR / "schemas" / "products.json").read_text())


def test_get_package_normalizes_app_catalog_manifest():
    package = object_packages.get_package("app-catalog", root=PACKAGES_ROOT)

    assert package["id"] == "app-catalog"
    assert package["name"] == "Catalog"
    assert {schema["collection"] for schema in package["schemas"]} == {"products"}
    assert {obj["id"] for obj in package["objects"]} == {
        "site_products",
        "site_product_view",
    }
    assert package["permissions"] == [{"path": "permissions/rules.json"}]
    assert {entry["collection"] for entry in package["seed"]} == {"products"}


def test_dry_run_app_catalog_package_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()

    plan = object_packages.dry_run_package(
        "app-catalog",
        root=PACKAGES_ROOT,
        base_dir=tmp_path / "data",
        object_roots=[object_root],
    )

    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []
    assert {schema["collection"] for schema in plan["schemas"]} == {"products"}


def test_install_app_catalog_package_loads_schema(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()

    object_packages.install_package(
        "app-catalog",
        root=PACKAGES_ROOT,
        base_dir=data_dir,
        object_roots=[object_root],
    )

    products_schema = object_schemas.get_schema("products", base_dir=data_dir)

    assert products_schema["name"] == "products"
    assert (object_root / "site" / "products.py").is_file()
    assert (object_root / "site" / "product_view.py").is_file()


def test_schema_json_file_is_valid_and_versioned():
    payload = _products_schema()
    assert payload["name"] == "products"
    assert payload["version"] == 1
    assert payload["views"]["list_mode"] == "table"


def test_no_money_field_uses_a_float_or_currency_type():
    """00-doctrine-and-contract.md's hard rule: money is *_cents integers,
    never object_records.py's _FLOAT_TYPES = {"float", "number", "currency"}.
    Products has no count/measure field like invoices' quantity, so there
    is no allowed-float exception here at all.
    """
    for field in _products_schema()["fields"]:
        name = field["name"]
        field_type = field.get("type")
        if "_cents" in name:
            assert field_type == "integer", f"products.{name} must be type integer, got {field_type!r}"
        assert field_type not in _FLOAT_MONEY_TYPES, (
            f"products.{name} uses a float-shaped type {field_type!r}"
        )


def test_every_cents_field_is_present_and_integer():
    by_name = {f["name"]: f for f in _products_schema()["fields"]}
    for name in ("price_cents", "cost_cents", "salvage_value_cents"):
        assert by_name[name]["type"] == "integer"
        assert by_name[name]["default"] == "0"


def test_product_type_enum_matches_the_source_model():
    """The reconciled predecessor system's Product model (private source
    audit, not part of this repo): type choices
    physical/digital/service/subscription/ASSET, carried verbatim.
    """
    by_name = {f["name"]: f for f in _products_schema()["fields"]}
    assert by_name["product_type"]["enum"] == [
        "physical", "digital", "service", "subscription", "asset",
    ]
    assert by_name["product_type"]["default"] == "physical"


def test_no_status_transitions_map_invented():
    """Products don't have a status FSM -- is_active is a plain boolean.
    The task brief is explicit that no transitions map should be invented
    here (contrast with app-invoices/app-tasks' guarded status enums).
    """
    by_name = {f["name"]: f for f in _products_schema()["fields"]}
    assert by_name["is_active"]["type"] == "boolean"
    assert by_name["is_active"]["default"] == "true"
    assert "transitions" not in by_name["is_active"]
    for field in _products_schema()["fields"]:
        assert "transitions" not in field, f"products.{field['name']} should not have a transitions map"


def test_asset_parity_fields_present():
    """ASSET parity fields carried from the predecessor system's Product
    model (private source audit, not part of this repo: ASSET type carries
    depreciation fields + helpers), not exercised in v1 (no depreciation
    schedule is computed here) but kept so no future migration has to
    backfill.
    """
    by_name = {f["name"]: f for f in _products_schema()["fields"]}

    assert by_name["useful_life_months"]["type"] == "integer"
    assert by_name["purchase_date"]["type"] == "date"
    assert by_name["salvage_value_cents"]["type"] == "integer"
    assert by_name["salvage_value_cents"]["default"] == "0"
    assert by_name["depreciation_method"]["type"] == "enum"
    assert by_name["depreciation_method"]["enum"] == ["straight_line", "declining"]
    # nullable: no default, not required -- an asset whose method has not
    # been set yet stays blank rather than forced to a guessed default.
    assert "default" not in by_name["depreciation_method"]
    assert not by_name["depreciation_method"].get("required")
    assert by_name["asset_status"]["type"] == "text"


def test_finance_account_and_digital_file_pointers_carried_as_text():
    """The real predecessor links Product to finance Account rows and a
    downloadable file. No finance or hard file-relation package exists
    yet, so these stay plain text ids (see dbbasic-package.json) rather
    than a schema `relation` this codebase cannot yet resolve.
    """
    by_name = {f["name"]: f for f in _products_schema()["fields"]}
    for name in ("income_account", "expense_account", "digital_file_id"):
        assert by_name[name]["type"] == "text"
        assert "relation" not in by_name[name]


def test_products_forms_and_views_match_the_brief():
    schema = _products_schema()
    assert schema["forms"]["default"]["fields"] == [
        "name", "sku", "product_type", "description",
        "price_cents", "cost_cents", "currency", "unit", "is_active",
    ]
    assert schema["views"]["list_fields"] == [
        "name", "sku", "product_type", "price_cents", "is_active",
    ]


def test_search_fields_cover_only_content_fields():
    """Products is content-searchable (name/sku/description) -- this is
    the deliberate exception to the bookkeeping-field guard: no
    price/cost/account/asset field belongs in search.fields.
    """
    schema = _products_schema()
    assert schema["search"]["fields"] == ["name", "sku", "description"]
    bookkeeping_shaped = {
        "price_cents", "cost_cents", "salvage_value_cents", "currency",
        "income_account", "expense_account", "owner_id", "asset_status",
        "depreciation_method", "useful_life_months", "purchase_date",
        "digital_file_id",
    }
    assert not bookkeeping_shaped & set(schema["search"]["fields"])


def test_products_schema_field_order_matches_the_brief():
    field_names = [f["name"] for f in _products_schema()["fields"]]
    assert field_names == [
        "id", "name", "sku", "product_type", "description",
        "price_cents", "cost_cents", "currency", "unit", "is_active",
        "income_account", "expense_account", "digital_file_id",
        "useful_life_months", "purchase_date", "salvage_value_cents",
        "depreciation_method", "asset_status", "owner_id", "created_at",
    ]


def _app_catalog_policy():
    payload = json.loads((APP_CATALOG_DIR / "permissions" / "rules.json").read_text())
    return object_permissions.policy_from_dict({"access_mode": "role_based", "rules": payload["rules"]})


def test_owner_can_crud_own_product():
    policy = _app_catalog_policy()
    subject = object_permissions.PermissionSubject(user_id="7")
    record = {"owner_id": "7", "name": "Widget", "product_type": "physical"}

    for action in (
        object_permissions.CREATE,
        object_permissions.READ,
        object_permissions.UPDATE,
        object_permissions.DELETE,
    ):
        decision = object_permissions.check_permission(
            subject, action, policy=policy, collection="products", record=record
        )
        assert decision.allowed is True


def test_others_cannot_touch_someone_elses_product():
    policy = _app_catalog_policy()
    subject = object_permissions.PermissionSubject(user_id="8")
    record = {"owner_id": "7", "name": "Widget", "product_type": "physical"}

    for action in (
        object_permissions.READ,
        object_permissions.UPDATE,
        object_permissions.DELETE,
    ):
        decision = object_permissions.check_permission(
            subject, action, policy=policy, collection="products", record=record
        )
        assert decision.allowed is False


def test_anonymous_cannot_read_any_product():
    """No public read rule is granted on the products collection at all --
    v1 keeps the catalog owner-scoped like app-invoices; a public
    storefront view is deferred (see dbbasic-package.json).
    """
    policy = _app_catalog_policy()
    record = {"owner_id": "7", "name": "Widget", "product_type": "physical"}

    decision = object_permissions.check_permission(
        None, object_permissions.READ, policy=policy, collection="products", record=record
    )
    assert decision.allowed is False


def test_products_and_product_view_pages_are_publicly_executable():
    """Public execute on the *page objects* (they show a sign-in prompt to
    visitors), never public read on the *collection* -- same split
    app-invoices and app-notes use.
    """
    policy = _app_catalog_policy()

    for object_id in ("site_products", "site_product_view"):
        decision = object_permissions.check_permission(
            None, object_permissions.EXECUTE, policy=policy, object_id=object_id
        )
        assert decision.allowed is True


def test_seed_tsv_is_header_only_and_matches_schema_field_order():
    """Header-only, matching the established precedent (app-tasks,
    app-notes, app-invoices, app-contacts all ship header-only seeds).
    The header order matches the schema's own field order for
    readability.
    """
    schema = _products_schema()
    path = APP_CATALOG_DIR / "seed" / "products.tsv"
    lines = path.read_text().splitlines()
    assert len(lines) == 1, "products.tsv should be header-only"
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
    for path in APP_CATALOG_DIR.rglob("*"):
        if path.is_dir():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not banned.search(text), f"disallowed reference found in {path}"
