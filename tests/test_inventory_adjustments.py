"""Inventory adjustments (plan/inventory-adjustments-spec.md): the loss
taxonomy, the cost-stamping/gating hook, the shrinkage composer, and count
reconciliation. Losses are MOVEMENTS with a financial shadow -- never edits
to an on-hand number (docs/logic-decisions.md #3)."""

import pathlib
from decimal import Decimal

import object_execution
import object_records
import object_schemas
import object_stock
import python_object_runtime
from conftest import stage_collection

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
RUNTIME = python_object_runtime.PythonObjectRuntime()
CATALOG_OBJECTS = PACKAGES / "app-catalog" / "objects"

INV, SHRINK, THEFT = "acct-inventory", "acct-shrinkage", "acct-theft"


def setup_env(tmp_path, monkeypatch, *, settings=None):
    data_dir = tmp_path / "data"
    wanted = [("app-catalog", "products"), ("app-catalog", "locations"),
              ("app-catalog", "stock_moves"),
              ("app-finance", "fin_journals"), ("app-finance", "fin_journal_lines"),
              ("app-finance", "fin_accounts")]
    for pkg, name in wanted:
        stage_collection(data_dir, pkg, name)

    if settings is None:
        settings = (("inventory.journal.inventory_account", INV),
                    ("inventory.journal.shrinkage_account", SHRINK))
    rows = ""
    for i, (k, v) in enumerate(settings):
        rows += f"s{i}\t{k}\t{v}\t\n"
    stage_collection(data_dir, "app-settings", "app_settings", rows=rows)

    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    for acct in (INV, SHRINK, THEFT):
        object_records.create_collection_record(
            "fin_accounts",
            {"id": acct, "name": acct, "account_type": "asset", "owner_id": "dan"},
            base_dir=data_dir)
    return data_dir


def make_product(data_dir, pid="prod1", cost="250"):
    return object_records.create_collection_record(
        "products",
        {"id": pid, "name": f"Widget {pid}", "product_type": "physical",
         "cost_cents": cost, "owner_id": "dan"},
        base_dir=data_dir)


def make_location(data_dir, lid="loc1"):
    return object_records.create_collection_record(
        "locations",
        {"id": lid, "name": f"Shelf {lid}", "owner_id": "dan"},
        base_dir=data_dir)


def make_move(data_dir, mid, *, reason, qty="1", cost="250",
              from_loc="", to_loc="", product="prod1"):
    return object_records.create_collection_record(
        "stock_moves",
        {"id": mid, "product_id": product, "from_location_id": from_loc,
         "to_location_id": to_loc, "quantity": qty, "unit_cost_cents": cost,
         "reason": reason, "occurred_at": "2026-07-24", "owner_id": "dan"},
        base_dir=data_dir)


def run_hook(record, action="create"):
    result = object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "hook_stock_moves", method="BEFORE_WRITE",
            payload={"action": action, "collection": "stock_moves", "record": record}),
        roots=[CATALOG_OBJECTS])
    return result.result


def fire_stock_books(record_id):
    # Mirror the REAL dispatcher payload: event name participle, action RAW.
    result = object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "system_stock_books", method="EVENT",
            payload={"event": "stock_moves.record.created",
                     "collection": "stock_moves", "record_id": record_id,
                     "action": "create"}),
        roots=[CATALOG_OBJECTS])
    return result.result


def apply_count(payload):
    result = object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "action_apply_count", method="POST",
            payload={"_identity": {"user_id": "dan"}, **payload}),
        roots=[CATALOG_OBJECTS])
    return result.result


def journals(data_dir):
    return object_records.read_collection_records("fin_journals", base_dir=data_dir)


def lines_for(data_dir, journal_id):
    return [l for l in object_records.read_collection_records(
                "fin_journal_lines", base_dir=data_dir)
            if l["journal_id"] == journal_id]


# --- 1. taxonomy -----------------------------------------------------------

def test_all_six_loss_reasons_are_valid_and_old_reasons_unaffected(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_product(data_dir)
    make_location(data_dir)
    for i, reason in enumerate(("waste", "breakage", "theft", "expiry",
                                "damage", "disaster")):
        make_move(data_dir, f"m{i}", reason=reason, from_loc="loc1")
    make_move(data_dir, "m-old", reason="transfer", from_loc="loc1", to_loc="loc1")
    rows = object_records.read_collection_records("stock_moves", base_dir=data_dir)
    assert len(rows) == 7


def test_schema_declares_the_hook_and_reason_filter(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    schema = object_schemas.get_schema("stock_moves", base_dir=data_dir)
    assert schema["hooks"]["before_write"] == "hook_stock_moves"
    assert "reason" in schema["views"]["filter_fields"]


# --- 2/3. hook: stamps and gates -------------------------------------------

def test_hook_stamps_product_cost_when_absent_and_keeps_explicit_cost(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_product(data_dir, cost="250")
    stamped = run_hook({"product_id": "prod1", "quantity": "3",
                        "reason": "waste", "from_location_id": "loc1"})
    assert stamped["record"]["unit_cost_cents"] == "250"
    explicit = run_hook({"product_id": "prod1", "quantity": "3", "reason": "purchase",
                         "to_location_id": "loc1", "unit_cost_cents": "199"})
    assert explicit is None  # no transform: the client's invoice cost is kept


def test_later_product_cost_edit_never_touches_the_stamped_move(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_product(data_dir, cost="250")
    make_location(data_dir)
    stamped = run_hook({"id": "mv-stamp", "product_id": "prod1", "quantity": "2",
                        "reason": "waste", "from_location_id": "loc1",
                        "occurred_at": "2026-07-24", "owner_id": "dan"})
    object_records.create_collection_record(
        "stock_moves", stamped["record"], base_dir=data_dir)
    object_records.update_collection_record(
        "products", "prod1", {"cost_cents": "999"}, base_dir=data_dir)
    move = object_records.get_collection_record("stock_moves", "mv-stamp", base_dir=data_dir)
    assert move["unit_cost_cents"] == "250"


def test_hook_gates_quantity_and_loss_shape(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    zero = run_hook({"product_id": "p", "quantity": "0", "reason": "waste",
                     "from_location_id": "loc1"})
    assert zero["status"] == 400 and "positive" in zero["error"]
    negative = run_hook({"product_id": "p", "quantity": "-2", "reason": "sale"})
    assert negative["status"] == 400
    no_from = run_hook({"product_id": "p", "quantity": "1", "reason": "theft"})
    assert no_from["status"] == 400 and "from_location_id" in no_from["error"]
    with_to = run_hook({"product_id": "p", "quantity": "1", "reason": "waste",
                        "from_location_id": "loc1", "to_location_id": "loc2"})
    assert with_to["status"] == 400 and "destination" in with_to["error"]
    update = run_hook({"quantity": "0"}, action="update")
    assert update is None  # create-only


# --- 4. shrinkage composer ---------------------------------------------------

def test_waste_composes_posted_shrinkage_journal_idempotently(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_product(data_dir)
    make_location(data_dir)
    make_move(data_dir, "mv1", reason="waste", qty="3", cost="250", from_loc="loc1")
    result = fire_stock_books("mv1")
    assert result["posted"] is True
    journal = journals(data_dir)[0]
    assert journal["status"] == "posted"
    assert journal["generated_from"] == "stock_moves/mv1"
    by_account = {l["account_id"]: l for l in lines_for(data_dir, result["journal_id"])}
    assert by_account[SHRINK]["debit_cents"] == "750"
    assert by_account[INV]["credit_cents"] == "750"
    replay = fire_stock_books("mv1")
    assert "already composed" in replay["skipped"]
    assert len(journals(data_dir)) == 1


def test_per_reason_account_override_routes_theft(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch, settings=(
        ("inventory.journal.inventory_account", INV),
        ("inventory.journal.shrinkage_account", SHRINK),
        ("inventory.journal.theft_account", THEFT),
    ))
    make_product(data_dir)
    make_location(data_dir)
    make_move(data_dir, "mv-theft", reason="theft", qty="1", cost="1000", from_loc="loc1")
    result = fire_stock_books("mv-theft")
    by_account = {l["account_id"]: l for l in lines_for(data_dir, result["journal_id"])}
    assert by_account[THEFT]["debit_cents"] == "1000"
    assert by_account[INV]["credit_cents"] == "1000"


def test_unconfigured_accounts_skip_and_move_is_unaffected(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch, settings=())
    make_product(data_dir)
    make_location(data_dir)
    make_move(data_dir, "mv1", reason="waste", qty="3", from_loc="loc1")
    result = fire_stock_books("mv1")
    assert result["skipped"] == "accounts unconfigured"
    assert journals(data_dir) == []
    assert object_records.get_collection_record("stock_moves", "mv1", base_dir=data_dir)


def test_a_transfer_shaped_adjustment_composes_nothing(tmp_path, monkeypatch):
    """Both locations set: goods moved between shelves, no value changed."""
    data_dir = setup_env(tmp_path, monkeypatch)
    make_product(data_dir)
    make_location(data_dir)
    make_move(data_dir, "mv-tr", reason="adjustment", qty="2",
              from_loc="loc1", to_loc="loc1")
    assert fire_stock_books("mv-tr")["skipped"] == "not a loss event"
    assert journals(data_dir) == []


def test_a_sale_needs_its_own_account_and_never_borrows_shrinkage(
        tmp_path, monkeypatch):
    """A sale IS a composable direction now (COGS-on-sale, see
    test_cogs_on_sale.py) -- but this env configures only inventory and
    shrinkage, and cost of sales must never fall back to the shrinkage
    account the loss reasons default to. Goods sold are not goods lost,
    and routing one into the other misstates the P&L while still
    balancing, which is the failure mode nobody notices."""
    data_dir = setup_env(tmp_path, monkeypatch)
    make_product(data_dir)
    make_location(data_dir)
    make_move(data_dir, "mv-sale", reason="sale", qty="2", from_loc="loc1")
    assert fire_stock_books("mv-sale")["skipped"] == "accounts unconfigured"
    assert journals(data_dir) == []


# --- 5. count reconciliation -------------------------------------------------

def test_count_shortage_writes_move_out_and_shrinkage_journal(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_product(data_dir, cost="250")
    make_location(data_dir)
    make_move(data_dir, "mv-in", reason="purchase", qty="10", to_loc="loc1")
    result = apply_count({"product_id": "prod1", "location_id": "loc1",
                          "counted_qty": "7"})
    assert result["status"] == 200 and result["direction"] == "shortage"
    assert result["variance"] == "-3"
    move = object_records.get_collection_record("stock_moves", result["move_id"],
                                                base_dir=data_dir)
    assert move["from_location_id"] == "loc1" and move["to_location_id"] == ""
    assert move["quantity"] == "3" and move["reference"] == "count variance"
    assert move["unit_cost_cents"] == "250"
    by_account = {l["account_id"]: l
                  for l in lines_for(data_dir, result["journal"]["journal_id"])}
    assert by_account[SHRINK]["debit_cents"] == "750"
    assert object_stock.quantity_at_location(
        "prod1", "loc1", base_dir=data_dir) == Decimal("7")


def test_count_overage_writes_move_in_and_mirror_journal(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_product(data_dir, cost="100")
    make_location(data_dir)
    make_move(data_dir, "mv-in", reason="purchase", qty="10", to_loc="loc1")
    result = apply_count({"product_id": "prod1", "location_id": "loc1",
                          "counted_qty": "12"})
    assert result["direction"] == "overage" and result["variance"] == "2"
    by_account = {l["account_id"]: l
                  for l in lines_for(data_dir, result["journal"]["journal_id"])}
    assert by_account[INV]["debit_cents"] == "200"
    assert by_account[SHRINK]["credit_cents"] == "200"
    assert object_stock.quantity_at_location(
        "prod1", "loc1", base_dir=data_dir) == Decimal("12")


def test_count_match_is_a_no_op(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_product(data_dir)
    make_location(data_dir)
    make_move(data_dir, "mv-in", reason="purchase", qty="5", to_loc="loc1")
    result = apply_count({"product_id": "prod1", "location_id": "loc1",
                          "counted_qty": "5"})
    assert result["variance"] == "0"
    moves = object_records.read_collection_records("stock_moves", base_dir=data_dir)
    assert len(moves) == 1  # only the purchase
    assert journals(data_dir) == []


# --- 6. levels reflect losses -----------------------------------------------

def test_stock_levels_reflect_loss_moves(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_product(data_dir)
    make_location(data_dir)
    make_move(data_dir, "mv-in", reason="purchase", qty="10", to_loc="loc1")
    make_move(data_dir, "mv-waste", reason="waste", qty="3", from_loc="loc1")
    make_move(data_dir, "mv-theft", reason="theft", qty="1", from_loc="loc1")
    assert object_stock.quantity_at_location(
        "prod1", "loc1", base_dir=data_dir) == Decimal("6")
