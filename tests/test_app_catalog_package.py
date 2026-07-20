"""Structural tests for packages/app-catalog (products + STOCK: locations,
stock_moves).

Mirrors the package/schema/permission testing conventions used for
packages/app-invoices in tests/test_app_invoices_package.py. Behavior
tests for the derived-levels fold (object_stock.py) live in
tests/test_object_stock.py.
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

# quantity is the one deliberate exception: a count/measure (e.g. 2.5 kg),
# not currency -- same exception app-orders' order_lines.quantity documents.
_ALLOWED_FLOAT_FIELDS = {"quantity"}


def _products_schema():
    return json.loads((APP_CATALOG_DIR / "schemas" / "products.json").read_text())


def _locations_schema():
    return json.loads((APP_CATALOG_DIR / "schemas" / "locations.json").read_text())


def _stock_moves_schema():
    return json.loads((APP_CATALOG_DIR / "schemas" / "stock_moves.json").read_text())


def test_get_package_normalizes_app_catalog_manifest():
    package = object_packages.get_package("app-catalog", root=PACKAGES_ROOT)

    assert package["id"] == "app-catalog"
    assert package["name"] == "Catalog"
    assert {schema["collection"] for schema in package["schemas"]} == {
        "products", "locations", "stock_moves",
    }
    # site_product_view was removed in the Stage-6 retrofit: the product
    # permalink is now a seeded 59 detail view (site_view_render), not a
    # bespoke page object -- see tests/test_app_catalog_detail_retrofit.py.
    assert {obj["id"] for obj in package["objects"]} == {
        "site_products",
        "site_locations",
        "site_stock",
    }
    assert package["permissions"] == [{"path": "permissions/rules.json"}]
    assert {entry["collection"] for entry in package["seed"]} == {
        "products", "locations", "stock_moves", "views", "site_routes",
    }


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
    assert {schema["collection"] for schema in plan["schemas"]} == {
        "products", "locations", "stock_moves",
    }


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
    locations_schema = object_schemas.get_schema("locations", base_dir=data_dir)
    stock_moves_schema = object_schemas.get_schema("stock_moves", base_dir=data_dir)

    assert products_schema["name"] == "products"
    assert locations_schema["name"] == "locations"
    assert stock_moves_schema["name"] == "stock_moves"
    assert stock_moves_schema["storage"] == "append"
    assert (object_root / "site" / "products.py").is_file()
    # product_view.py was removed in the Stage-6 retrofit (replaced by a
    # seeded detail view); it must no longer be installed.
    assert not (object_root / "site" / "product_view.py").exists()
    assert (object_root / "site" / "locations.py").is_file()
    assert (object_root / "site" / "stock.py").is_file()


def test_schema_json_file_is_valid_and_versioned():
    payload = _products_schema()
    assert payload["name"] == "products"
    # v1 -> v2: the ASSET-only fields gained `visible_when` so a detail/edit
    # surface hides them for non-asset products (Stage-6 conditional-visibility
    # extraction), replacing product_view.py's bespoke show/hide CSS+JS.
    assert payload["version"] == 2
    assert payload["views"]["list_mode"] == "table"


def test_asset_fields_are_conditionally_visible_on_asset_products_only():
    payload = _products_schema()
    by_name = {f["name"]: f for f in payload["fields"]}
    for name in ("useful_life_months", "purchase_date", "salvage_value_cents",
                 "depreciation_method", "asset_status"):
        assert by_name[name]["visible_when"] == {"field": "product_type", "equals": "asset"}, name
    # Non-asset fields must NOT carry the condition (they always show).
    assert "visible_when" not in by_name["name"]
    assert "visible_when" not in by_name["price_cents"]


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


def test_locations_schema_field_order_and_types():
    schema = _locations_schema()
    field_names = [f["name"] for f in schema["fields"]]
    assert field_names == [
        "id", "name", "location_type", "parent_id", "code", "owner_id", "created_at",
    ]
    by_name = {f["name"]: f for f in schema["fields"]}
    assert by_name["name"]["type"] == "text"
    assert by_name["name"]["required"] is True
    assert by_name["code"]["type"] == "text"
    assert by_name["created_at"]["read_only"] is True


def test_locations_type_enum_matches_the_source_model():
    """The reconciled predecessor system's Location model (private source
    audit, not part of this repo): a real warehouse->bin hierarchy plus
    virtual customer/supplier locations, carried verbatim -- with a plain
    'virtual' catch-all alongside the two named virtual counterparties.
    """
    by_name = {f["name"]: f for f in _locations_schema()["fields"]}
    assert by_name["location_type"]["enum"] == [
        "warehouse", "zone", "aisle", "shelf", "bin", "customer", "supplier", "virtual",
    ]
    assert by_name["location_type"]["default"] == "warehouse"


def test_locations_parent_id_is_a_self_relation():
    """Self-parent hierarchy: warehouse -> zone -> aisle -> shelf -> bin,
    same shape app-thread's thread_comments.reply_to_id uses to relate a
    collection to itself. Optional -- a top-level location has no parent.
    """
    by_name = {f["name"]: f for f in _locations_schema()["fields"]}
    assert by_name["parent_id"]["relation"]["collection"] == "locations"
    assert "required" not in by_name["parent_id"] or not by_name["parent_id"]["required"]


def test_locations_search_covers_only_content_fields():
    schema = _locations_schema()
    assert schema["search"]["fields"] == ["name", "code"]


def test_stock_moves_schema_is_append_storage():
    """stock_moves is the textbook append-mode collection
    (docs/append-only-storage-design.md): immutable, write-heavy, never
    updated. Declared via the schema's top-level "storage" key, the only
    way a manual schema can opt in (object_schemas.py).
    """
    schema = _stock_moves_schema()
    assert schema["storage"] == "append"


def test_stock_moves_schema_field_order_and_relations():
    schema = _stock_moves_schema()
    field_names = [f["name"] for f in schema["fields"]]
    assert field_names == [
        "id", "product_id", "from_location_id", "to_location_id", "quantity",
        "unit_cost_cents", "reason", "reference", "occurred_at", "owner_id", "created_at",
    ]
    by_name = {f["name"]: f for f in schema["fields"]}

    assert by_name["product_id"]["relation"]["collection"] == "products"
    assert by_name["product_id"]["required"] is True

    for name in ("from_location_id", "to_location_id"):
        assert by_name[name]["relation"]["collection"] == "locations"
        assert "required" not in by_name[name] or not by_name[name]["required"]


def test_stock_moves_reason_enum_matches_the_source_model():
    """Matches the predecessor system's stock move reasons (private source
    audit, not part of this repo), default transfer.
    """
    by_name = {f["name"]: f for f in _stock_moves_schema()["fields"]}
    assert by_name["reason"]["enum"] == [
        "purchase", "sale", "transfer", "adjustment", "return", "count",
    ]
    assert by_name["reason"]["default"] == "transfer"


def test_stock_moves_quantity_and_cost_field_types():
    by_name = {f["name"]: f for f in _stock_moves_schema()["fields"]}
    assert by_name["quantity"]["type"] == "number"
    assert by_name["quantity"]["required"] is True
    assert by_name["unit_cost_cents"]["type"] == "integer"
    assert "required" not in by_name["unit_cost_cents"] or not by_name["unit_cost_cents"]["required"]


def test_stock_moves_has_no_search_config():
    """Moves are records, not content -- unlike products/locations, this
    collection is deliberately not globally searchable.
    """
    assert "search" not in _stock_moves_schema()


def test_no_money_field_in_locations_or_stock_moves_uses_a_float_or_currency_type():
    for schema in (_locations_schema(), _stock_moves_schema()):
        for field in schema["fields"]:
            name = field["name"]
            if name in _ALLOWED_FLOAT_FIELDS:
                continue
            field_type = field.get("type")
            if "_cents" in name:
                assert field_type == "integer", f"{schema['name']}.{name} must be type integer, got {field_type!r}"
            assert field_type not in _FLOAT_MONEY_TYPES, (
                f"{schema['name']}.{name} uses a float-shaped type {field_type!r}"
            )


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


def test_products_list_page_is_publicly_executable():
    """Public execute on the *page object* (it shows a sign-in prompt to
    visitors), never public read on the *collection* -- same split
    app-invoices and app-notes use. (The product permalink is now a seeded
    detail view via site_view_render, not a bespoke page object.)
    """
    policy = _app_catalog_policy()

    decision = object_permissions.check_permission(
        None, object_permissions.EXECUTE, policy=policy, object_id="site_products"
    )
    assert decision.allowed is True


def test_owner_can_crud_own_location():
    policy = _app_catalog_policy()
    subject = object_permissions.PermissionSubject(user_id="7")
    record = {"owner_id": "7", "name": "Main Warehouse", "location_type": "warehouse"}

    for action in (
        object_permissions.CREATE,
        object_permissions.READ,
        object_permissions.UPDATE,
        object_permissions.DELETE,
    ):
        decision = object_permissions.check_permission(
            subject, action, policy=policy, collection="locations", record=record
        )
        assert decision.allowed is True


def test_others_cannot_touch_someone_elses_location():
    policy = _app_catalog_policy()
    subject = object_permissions.PermissionSubject(user_id="8")
    record = {"owner_id": "7", "name": "Main Warehouse", "location_type": "warehouse"}

    for action in (
        object_permissions.READ,
        object_permissions.UPDATE,
        object_permissions.DELETE,
    ):
        decision = object_permissions.check_permission(
            subject, action, policy=policy, collection="locations", record=record
        )
        assert decision.allowed is False


def test_owner_can_crud_own_stock_move():
    policy = _app_catalog_policy()
    subject = object_permissions.PermissionSubject(user_id="7")
    record = {"owner_id": "7", "product_id": "p1", "quantity": "10", "reason": "purchase"}

    for action in (
        object_permissions.CREATE,
        object_permissions.READ,
        object_permissions.UPDATE,
        object_permissions.DELETE,
    ):
        decision = object_permissions.check_permission(
            subject, action, policy=policy, collection="stock_moves", record=record
        )
        assert decision.allowed is True


def test_others_cannot_touch_someone_elses_stock_move():
    policy = _app_catalog_policy()
    subject = object_permissions.PermissionSubject(user_id="8")
    record = {"owner_id": "7", "product_id": "p1", "quantity": "10", "reason": "purchase"}

    for action in (
        object_permissions.READ,
        object_permissions.UPDATE,
        object_permissions.DELETE,
    ):
        decision = object_permissions.check_permission(
            subject, action, policy=policy, collection="stock_moves", record=record
        )
        assert decision.allowed is False


def test_anonymous_cannot_read_locations_or_stock_moves():
    policy = _app_catalog_policy()

    for collection, record in (
        ("locations", {"owner_id": "7", "name": "Main Warehouse"}),
        ("stock_moves", {"owner_id": "7", "product_id": "p1", "quantity": "10"}),
    ):
        decision = object_permissions.check_permission(
            None, object_permissions.READ, policy=policy, collection=collection, record=record
        )
        assert decision.allowed is False


def test_locations_and_stock_pages_are_publicly_executable():
    """Same split as products/product_view: public execute on the page
    objects only, never public read on the underlying collections.
    """
    policy = _app_catalog_policy()

    for object_id in ("site_locations", "site_stock"):
        decision = object_permissions.check_permission(
            None, object_permissions.EXECUTE, policy=policy, object_id=object_id
        )
        assert decision.allowed is True


def test_seed_tsvs_are_header_only_and_match_schema_field_order():
    """Header-only, matching the established precedent (app-tasks,
    app-notes, app-invoices, app-contacts all ship header-only seeds).
    The header order matches each schema's own field order for
    readability.
    """
    for schema_fn, filename in (
        (_products_schema, "products.tsv"),
        (_locations_schema, "locations.tsv"),
        (_stock_moves_schema, "stock_moves.tsv"),
    ):
        schema = schema_fn()
        path = APP_CATALOG_DIR / "seed" / filename
        lines = path.read_text().splitlines()
        assert len(lines) == 1, f"{filename} should be header-only"
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


def test_object_stock_module_has_no_disallowed_org_names():
    """object_stock.py lives at the repo root, not inside packages/
    app-catalog/ (see object_stock.py's own module docstring for why --
    same placement and same reason as object_finance.py for app-finance),
    so the repo-hygiene sweep above never walks it -- covered here
    explicitly instead, mirroring
    test_app_finance_package.py::test_object_finance_module_has_no_disallowed_org_names.
    """
    banned = re.compile(
        "|".join([r"\b" + "q" + "9" + r"\b", "ask" + "robots", r"\b" + "wo" + "ld" + r"\b"]),
        re.IGNORECASE,
    )
    path = Path(__file__).resolve().parents[1] / "object_stock.py"
    text = path.read_text(encoding="utf-8", errors="ignore")
    assert not banned.search(text), f"disallowed reference found in {path}"
