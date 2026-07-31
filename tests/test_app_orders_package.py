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
    # 0.3.0 deletes the bespoke /orders list page object: it held no
    # business logic and is now a seeded VIEW record over the generic
    # `list` block.
    assert {obj["id"] for obj in package["objects"]} == {
        "system_order_totals",
        # 0.4.0: committed orders with goods still owed -- the pick
        # queue, counted for the home page's attention band.
        "system_order_attention",
        # 0.5.0, the store-voice slice: the customer's own door and the
        # shop's own voice. Behavior lives in tests/test_store_voice.py.
        "system_order_portal_link",
        "system_order_email",
        "site_order_status",
        # 0.6.0, the drop-ship slice: the action that raises the vendor's
        # purchase order from a sale order, and the two-row read that
        # proves margin needed no stored field. Behavior lives in
        # tests/test_dropship.py.
        "action_dropship_order",
        "site_dropship_margin",
    }
    assert package["permissions"] == [{"path": "permissions/rules.json"}]
    assert {entry["collection"] for entry in package["seed"]} == {
        "orders",
        "order_lines",
        "views",
        "site_routes",
    }
    assert {dep["id"] for dep in package["dependencies"]} == {"app-views"}


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
    # orders.py was deleted (0.3.0): the /orders list page is now a seeded
    # VIEW record over the generic `list` block.
    assert not (object_root / "site" / "orders.py").exists()
    assert (object_root / "system" / "order_totals.py").is_file()


def test_schema_json_files_are_valid_and_versioned():
    # orders is at v3: the fulfillment slice added the machine-derived
    # `partial` status and the receiving slice added `received`, the
    # purchase side's terminal state (see the transitions test below).
    # orders v4: customer_note + gift_message, so the packing slip prints
    # what the shopper typed at checkout (v3 added `received` for the PO
    # side, v2 added `partial`).
    # orders v5: portal_token, the customer's capability URL. A guest
    # checkout leaves no account to sign into, so without it a buyer could
    # neither track an order nor start a return.
    # orders v6: drop-shipping -- fulfillment_source, linked_order_id,
    # vendor_id and the ship_to pair. No new collection, because the v1
    # shared SO/PO schema had already modelled "the vendor ships straight
    # to my customer" as two rows pointing at each other; see
    # tests/test_dropship.py.
    # orders v7: TIME -- fulfillment_method plus the four datetimes
    # (requested_at/promised_at/ready_at/collected_at) and the pickup half
    # of the status ladder. Five fields and three enum values rather than a
    # new collection, because everything about the money was already here
    # and only the time was missing; see tests/test_pickup_slots.py.
    # order_lines v3: `backordered`, so the shipment builder can tell a
    # line the shop agreed to owe from one it can actually pack.
    # v2 added line_note and modifier_cents, the per-line half of
    # the same slice -- "no onions" is an instruction on one line of one
    # order and has no SKU, and "oat milk +60c" is that instruction with a
    # price delta, which is why the delta lives on the line rather than in
    # the price book. See tests/test_pickup_service.py.
    expected_versions = {"orders": 7, "order_lines": 3}
    for name in ("orders", "order_lines"):
        payload = json.loads((APP_ORDERS_DIR / "schemas" / f"{name}.json").read_text())
        assert payload["name"] == name
        assert payload["version"] == expected_versions[name]
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
        "draft", "confirmed", "processing", "partial", "shipped", "delivered",
        "received", "preparing", "ready", "collected", "cancelled",
    ]
    assert status_field["default"] == "draft"

    transitions = status_field["transitions"]
    owner_guard = {"owner_id": "$user_id"}

    draft_targets = {entry["to"]: entry["when"] for entry in transitions["draft"]}
    assert draft_targets == {"confirmed": owner_guard, "cancelled": owner_guard}

    # partial/shipped are reachable straight from confirmed because they are
    # DERIVED by system_order_fulfillment from shipment lines, and the
    # zero-touch shop (app-shop's auto_fulfill) ships a paid order without
    # ever passing through processing.
    # `received` is reachable from the same three states for the mirror
    # reason on the PURCHASE side: system_receipt_posting derives it from
    # receipt lines, and a PO that was received in one delivery never passed
    # through partial.
    # v7: `preparing` and `ready` join the same list, and `ready` is
    # reachable straight from confirmed for the third time the same
    # argument has been made here -- a counter that makes the coffee while
    # the customer stands there never passes through a `preparing` anybody
    # observed, and a forced bookkeeping hop nobody performed is fake
    # precision, exactly as it was for the zero-touch shop's shipped.
    confirmed_targets = {entry["to"]: entry["when"] for entry in transitions["confirmed"]}
    assert confirmed_targets == {"processing": owner_guard, "partial": owner_guard,
                                 "shipped": owner_guard, "received": owner_guard,
                                 "preparing": owner_guard, "ready": owner_guard,
                                 "cancelled": owner_guard}

    processing_targets = {entry["to"]: entry["when"] for entry in transitions["processing"]}
    assert processing_targets == {"partial": owner_guard, "shipped": owner_guard,
                                  "received": owner_guard, "cancelled": owner_guard}

    partial_targets = {entry["to"]: entry["when"] for entry in transitions["partial"]}
    assert partial_targets == {"shipped": owner_guard, "received": owner_guard,
                               "cancelled": owner_guard}

    shipped_targets = {entry["to"]: entry["when"] for entry in transitions["shipped"]}
    assert shipped_targets == {"delivered": owner_guard}

    # The PICKUP ladder (v7): confirmed -> preparing -> ready -> collected.
    # cancelled hangs off both middle rungs, for the customer who calls
    # back while it is being made and the one who never turns up.
    preparing_targets = {entry["to"]: entry["when"] for entry in transitions["preparing"]}
    assert preparing_targets == {"ready": owner_guard, "cancelled": owner_guard}

    ready_targets = {entry["to"]: entry["when"] for entry in transitions["ready"]}
    assert ready_targets == {"collected": owner_guard, "cancelled": owner_guard}

    # delivered, received, collected and cancelled are terminal: no entries
    # in the transitions map at all. received is terminal for the same
    # reason delivered is -- goods that arrived cannot un-arrive, and a
    # miscount is an adjustment move, never an edit. `collected` is
    # terminal for the third version of that sentence.
    assert "delivered" not in transitions
    assert "received" not in transitions
    assert "collected" not in transitions
    assert "cancelled" not in transitions


def test_orders_forms_and_views_match_the_brief():
    schema = _orders_schema()
    # fulfillment_method is IN the default form (v7): it is the field every
    # downstream behaviour keys off, and one only action_checkout could set
    # would leave an operator raising a counter or pickup order by hand with
    # no way to say so.
    assert schema["forms"]["default"]["fields"] == [
        "doc_type", "number", "customer_id", "customer_name", "customer_email",
        "currency", "order_date", "expected_date", "status", "fulfillment_method",
        "notes",
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
    # v2 puts line_note and modifier_cents next to the unit price they
    # qualify, and before the line total they are folded into -- reading
    # order is the argument: the note and the delta belong to the thing
    # being sold, not to the arithmetic underneath it. v3 adds
    # `backordered` last among the facts about the sale and still ahead of
    # the arithmetic, for the same reason. v4 adds discount_bps in the
    # same spirit: the rate somebody negotiated is a fact about the sale,
    # so it sits with the price it reduces, and line_discount_cents joins
    # the computed block beside the total it produced.
    assert field_names == [
        "id", "order_id", "product_id", "description", "quantity", "unit_price_cents",
        "line_note", "modifier_cents", "backordered",
        "discount_bps", "line_discount_cents",
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
    this package declares no dependency on app-catalog. It does depend on
    app-views now (the retrofit's seeded view_order_detail needs
    site_view_render), same as every other Stage-6-retrofitted package.
    """
    by_name = {f["name"]: f for f in _order_lines_schema()["fields"]}
    assert by_name["product_id"]["relation"]["collection"] == "products"
    assert "required" not in by_name["product_id"] or not by_name["product_id"]["required"]

    manifest = json.loads((APP_ORDERS_DIR / "dbbasic-package.json").read_text())
    assert "app-catalog" not in manifest.get("dependencies", [])
    assert manifest.get("dependencies", []) == ["app-views"]


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


def test_orders_page_is_publicly_executable():
    """Public execute on the *page object* (it shows a sign-in prompt to
    visitors), never public read on the *collection* -- same split
    app-invoices uses for site_invoices. The order detail permalink is now
    the seeded view_order_detail view rendered by the shared
    site_view_render object (see test_app_orders_detail_retrofit.py), not
    a bespoke page object of this package's own, so it carries no
    per-package permission rule here.

    site_orders' own public-execute rule is left in place even though the
    object was deleted in 0.3.0 (the /orders list page is now a seeded
    VIEW record) -- same precedent as app-notes' site_notes rule.
    """
    policy = _app_orders_policy()

    decision = object_permissions.check_permission(
        None, object_permissions.EXECUTE, policy=policy, object_id="site_orders"
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
