"""Unit tests for object_materialize.py -- plan/vocabulary/61-materialize-spec.md.

Mirrors tests/test_object_rollups.py's direct schema/record-fixture style:
write a schema straight to data/schemas, create rows through object_records
with base_dir=tmp_path and roots=[] (never touching the real packages/
tree), then exercise object_materialize directly.

End-to-end package/permission tests (admin-only materialize_definitions,
the real dbbasic-package.json/manifest, HANDLES regeneration against the
real installed materialize_seed.py) live in
tests/test_app_materialize_package.py instead.
"""
import json

import pytest

import object_records
import object_materialize
import object_schemas


def write_schema(data_dir, collection, fields, storage=None):
    payload = {"name": collection, "fields": fields}
    if storage:
        payload["storage"] = storage
    path = data_dir / "schemas" / f"{collection}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return path


FIN_RECURRING_FIELDS = [
    {"name": "id"},
    {"name": "name", "type": "text"},
    {"name": "template_lines", "type": "textarea"},
    {"name": "frequency", "type": "text"},
    {"name": "next_run", "type": "date"},
    {"name": "auto_post", "type": "boolean"},
    {"name": "is_active", "type": "boolean"},
    {"name": "created_at", "type": "datetime"},
]

FIN_JOURNALS_FIELDS = [
    {"name": "id"},
    {"name": "date", "type": "date"},
    {"name": "description", "type": "text"},
    {"name": "status", "type": "text"},
    {"name": "reference", "type": "text"},
    {"name": "currency", "type": "text"},
    {"name": "generated_from", "type": "text"},
]

FIN_JOURNAL_LINES_FIELDS = [
    {"name": "id"},
    {"name": "journal_id", "type": "text"},
    {"name": "account_id", "type": "text"},
    {"name": "debit_cents", "type": "integer"},
    {"name": "credit_cents", "type": "integer"},
    {"name": "memo", "type": "text"},
]

PRODUCTS_FIELDS = [
    {"name": "id"},
    {"name": "name", "type": "text"},
    {"name": "product_type", "type": "text"},
    {"name": "purchase_date", "type": "date"},
    {"name": "purchase_cost_cents", "type": "integer"},
    {"name": "salvage_value_cents", "type": "integer"},
    {"name": "useful_life_months", "type": "integer"},
    {"name": "depreciation_method", "type": "text"},
]

TASKS_FIELDS = [
    {"name": "id"},
    {"name": "title", "type": "text"},
    {"name": "description", "type": "textarea"},
    {"name": "template_id", "type": "text", "relation": {"collection": "templates", "display_field": "name"}},
    {"name": "generated_from", "type": "text"},
]

TEMPLATES_FIELDS = [
    {"name": "id"},
    {"name": "name", "type": "text"},
    {"name": "default_values", "type": "textarea"},
]


def _make_fin_recurring(data_dir, rows):
    write_schema(data_dir, "fin_recurring", FIN_RECURRING_FIELDS)
    for row in rows:
        object_records.create_collection_record("fin_recurring", row, base_dir=data_dir, roots=[])


def _make_fin_journals(data_dir):
    write_schema(data_dir, "fin_journals", FIN_JOURNALS_FIELDS)


def _make_fin_journal_lines(data_dir):
    write_schema(data_dir, "fin_journal_lines", FIN_JOURNAL_LINES_FIELDS, storage="append")


def _make_products(data_dir, rows):
    write_schema(data_dir, "products", PRODUCTS_FIELDS)
    for row in rows:
        object_records.create_collection_record("products", row, base_dir=data_dir, roots=[])


def _make_tasks(data_dir, rows):
    write_schema(data_dir, "tasks", TASKS_FIELDS)
    for row in rows:
        object_records.create_collection_record("tasks", row, base_dir=data_dir, roots=[])


def _make_templates(data_dir, rows):
    write_schema(data_dir, "templates", TEMPLATES_FIELDS)
    for row in rows:
        object_records.create_collection_record("templates", row, base_dir=data_dir, roots=[])


def _recurring_definition(**overrides):
    base = {
        "id": "matgen_fin_recurring",
        "name": "Recurring journal generation",
        "source_collection": "fin_recurring",
        "source_filter": json.dumps({"is_active": "true"}),
        "trigger": json.dumps({
            "mode": "scheduled",
            "interval_seconds": 3600,
            "period_field": "frequency",
            "anchor_field": "next_run",
        }),
        "output_collection": "fin_journals",
        "child_collection": "fin_journal_lines",
        "child_source_field": "template_lines",
        "child_link_field": "journal_id",
        "idempotency_key": "matgen_{definition_id}_{source_id}_{period_start}",
        "mapping": json.dumps({
            "date": {"from_period": "period_start"},
            "description": {"template": "Recurring: {source.name} ({period_label})"},
            "status": {"if": {"source_field": "auto_post", "equals": True}, "then": "posted", "else": "draft"},
            "reference": {"literal": ""},
            "currency": {"literal": "USD"},
        }),
        "balance_check": json.dumps({"debit_field": "debit_cents", "credit_field": "credit_cents"}),
        "last_run_at": "",
        "actor": "daemon:materialize",
        "enabled": "true",
        "block": "false",
    }
    base.update(overrides)
    return base


def _depreciation_definition(**overrides):
    base = {
        "id": "matgen_depreciation",
        "name": "Monthly depreciation",
        "source_collection": "products",
        "source_filter": json.dumps({"product_type": "asset"}),
        "trigger": json.dumps({
            "mode": "scheduled_fixed",
            "interval_seconds": 3600,
            "period_field": "monthly",
            "start_field": "purchase_date",
            "end_condition": {"periods_field": "useful_life_months"},
        }),
        "output_collection": "fin_journals",
        "child_collection": "fin_journal_lines",
        "child_link_field": "journal_id",
        "idempotency_key": "matgen_{definition_id}_{source_id}_{period_start}",
        "mapping": json.dumps({
            "date": {"from_period": "period_start"},
            "description": {"template": "Depreciation: {source.name} ({period_label})"},
            "status": {"literal": "posted"},
            "currency": {"literal": "USD"},
            "amount": {"depreciation_amount": {
                "method": "straight_line",
                "cost_field": "purchase_cost_cents",
                "salvage_field": "salvage_value_cents",
                "life_field": "useful_life_months",
            }},
        }),
        "balance_check": json.dumps({"debit_field": "debit_cents", "credit_field": "credit_cents"}),
        "debit_account_id": "acct_expense",
        "credit_account_id": "acct_accum_depr",
        "last_run_at": "",
        "actor": "daemon:materialize",
        "enabled": "true",
        "block": "false",
    }
    base.update(overrides)
    return base


def _creatework_definition(**overrides):
    base = {
        "id": "matgen_task_seed",
        "name": "CreateWork seed",
        "source_collection": "tasks",
        "trigger": json.dumps({"mode": "event", "on": "record.created"}),
        "output_collection": "tasks",
        "idempotency_key": "{definition_id}_{source_id}",
        "mapping": json.dumps({}),
        "last_run_at": "",
        "actor": "daemon:materialize",
        "enabled": "true",
        "block": "false",
    }
    base.update(overrides)
    return base


# --- parse_definition: happy paths --------------------------------------------------

def test_parse_definition_scheduled_happy_path(tmp_path):
    data_dir = tmp_path / "data"
    _make_fin_recurring(data_dir, [])
    _make_fin_journals(data_dir)
    _make_fin_journal_lines(data_dir)

    config = object_materialize.parse_definition(_recurring_definition(), base_dir=data_dir, roots=[])

    assert config.definition_id == "matgen_fin_recurring"
    assert config.trigger_mode == "scheduled"
    assert config.anchor_field == "next_run"
    assert config.frequency_field == "frequency"
    assert config.output_collection == "fin_journals"
    assert config.child_collection == "fin_journal_lines"
    assert config.balance_check == {"debit_field": "debit_cents", "credit_field": "credit_cents"}
    assert config.stamp_generated_from is True  # fin_journals fixture declares generated_from


def test_parse_definition_scheduled_fixed_happy_path(tmp_path):
    data_dir = tmp_path / "data"
    _make_products(data_dir, [])
    _make_fin_journals(data_dir)
    _make_fin_journal_lines(data_dir)

    config = object_materialize.parse_definition(_depreciation_definition(), base_dir=data_dir, roots=[])

    assert config.trigger_mode == "scheduled_fixed"
    assert config.start_field == "purchase_date"
    assert config.granularity == "monthly"
    assert config.periods_field == "useful_life_months"
    assert config.synthesized_amount_entry == {"depreciation_amount": {
        "method": "straight_line",
        "cost_field": "purchase_cost_cents",
        "salvage_field": "salvage_value_cents",
        "life_field": "useful_life_months",
    }}
    assert "amount" not in config.mapping  # popped out for the synthesized shape
    assert config.debit_account_id == "acct_expense"
    assert config.credit_account_id == "acct_accum_depr"


def test_parse_definition_event_happy_path(tmp_path):
    data_dir = tmp_path / "data"
    _make_tasks(data_dir, [])

    config = object_materialize.parse_definition(_creatework_definition(), base_dir=data_dir, roots=[])

    assert config.trigger_mode == "event"
    assert config.output_collection == config.source_collection == "tasks"


# --- parse_definition: validation --------------------------------------------------

def test_parse_definition_rejects_missing_id(tmp_path):
    data_dir = tmp_path / "data"
    _make_fin_recurring(data_dir, [])
    _make_fin_journals(data_dir)
    _make_fin_journal_lines(data_dir)
    with pytest.raises(object_materialize.DefinitionError, match="missing an id"):
        object_materialize.parse_definition(_recurring_definition(id=""), base_dir=data_dir, roots=[])


def test_parse_definition_rejects_missing_source_collection(tmp_path):
    data_dir = tmp_path / "data"
    with pytest.raises(object_materialize.DefinitionError, match="source_collection"):
        object_materialize.parse_definition(_recurring_definition(source_collection=""), base_dir=data_dir, roots=[])


def test_parse_definition_raises_missing_collection_error_for_unknown_source(tmp_path):
    data_dir = tmp_path / "data"
    with pytest.raises(object_materialize.MissingCollectionError) as excinfo:
        object_materialize.parse_definition(
            _recurring_definition(source_collection="no_such_collection"), base_dir=data_dir, roots=[],
        )
    assert excinfo.value.collection == "no_such_collection"
    assert excinfo.value.role == "source"


def test_parse_definition_raises_missing_collection_error_for_unknown_output(tmp_path):
    data_dir = tmp_path / "data"
    _make_fin_recurring(data_dir, [])
    with pytest.raises(object_materialize.MissingCollectionError) as excinfo:
        object_materialize.parse_definition(
            _recurring_definition(output_collection="no_such_output"), base_dir=data_dir, roots=[],
        )
    assert excinfo.value.role == "output"


def test_parse_definition_rejects_declining_depreciation_method(tmp_path):
    data_dir = tmp_path / "data"
    _make_products(data_dir, [])
    _make_fin_journals(data_dir)
    _make_fin_journal_lines(data_dir)
    bad = _depreciation_definition(mapping=json.dumps({
        "amount": {"depreciation_amount": {
            "method": "declining", "cost_field": "purchase_cost_cents",
            "salvage_field": "salvage_value_cents", "life_field": "useful_life_months",
        }},
    }))
    with pytest.raises(object_materialize.DefinitionError, match="declining"):
        object_materialize.parse_definition(bad, base_dir=data_dir, roots=[])


def test_parse_definition_rejects_unrecognized_mapping_op(tmp_path):
    data_dir = tmp_path / "data"
    _make_fin_recurring(data_dir, [])
    _make_fin_journals(data_dir)
    _make_fin_journal_lines(data_dir)
    bad = _recurring_definition(mapping=json.dumps({"status": {"whatever": "x"}}))
    with pytest.raises(object_materialize.DefinitionError, match="unrecognized|must use one of"):
        object_materialize.parse_definition(bad, base_dir=data_dir, roots=[])


def test_parse_definition_requires_child_link_field_when_child_collection_set(tmp_path):
    data_dir = tmp_path / "data"
    _make_fin_recurring(data_dir, [])
    _make_fin_journals(data_dir)
    _make_fin_journal_lines(data_dir)
    bad = _recurring_definition(child_link_field="")
    with pytest.raises(object_materialize.DefinitionError, match="child_link_field"):
        object_materialize.parse_definition(bad, base_dir=data_dir, roots=[])


def test_parse_definition_synthesized_shape_requires_accounts(tmp_path):
    data_dir = tmp_path / "data"
    _make_products(data_dir, [])
    _make_fin_journals(data_dir)
    _make_fin_journal_lines(data_dir)
    bad = _depreciation_definition(debit_account_id="", credit_account_id="")
    with pytest.raises(object_materialize.DefinitionError, match="debit_account_id and credit_account_id"):
        object_materialize.parse_definition(bad, base_dir=data_dir, roots=[])


def test_parse_definition_stamp_generated_from_false_when_output_schema_lacks_field(tmp_path):
    data_dir = tmp_path / "data"
    _make_tasks(data_dir, [])
    config = object_materialize.parse_definition(_creatework_definition(), base_dir=data_dir, roots=[])
    # tasks fixture DOES declare generated_from in this test module -- use a
    # bare schema without it to prove the opt-in behavior.
    write_schema(data_dir, "tasks_bare", [{"name": "id"}, {"name": "title", "type": "text"}])
    bare = _creatework_definition(id="matgen_bare", source_collection="tasks_bare", output_collection="tasks_bare")
    config_bare = object_materialize.parse_definition(bare, base_dir=data_dir, roots=[])
    assert config.stamp_generated_from is True
    assert config_bare.stamp_generated_from is False


# --- mapping vocabulary --------------------------------------------------

def _period(start="2026-07-01", end="2026-08-01", label="2026-07", index=None, total=None):
    from datetime import date
    return object_materialize.Period(
        start=date.fromisoformat(start), end=date.fromisoformat(end), label=label, index=index, total=total,
    )


def test_eval_entry_from_copies_source_field():
    value = object_materialize._eval_entry(
        "name", {"from": "name"}, source_row={"name": "Acme"}, period=None, definition_id="d1",
    )
    assert value == "Acme"


def test_eval_entry_from_period_start_end_label():
    period = _period()
    assert object_materialize._eval_entry(
        "date", {"from_period": "period_start"}, source_row={}, period=period, definition_id="d1",
    ) == "2026-07-01"
    assert object_materialize._eval_entry(
        "date", {"from_period": "period_end"}, source_row={}, period=period, definition_id="d1",
    ) == "2026-08-01"
    assert object_materialize._eval_entry(
        "date", {"from_period": "period_label"}, source_row={}, period=period, definition_id="d1",
    ) == "2026-07"


def test_eval_entry_from_period_requires_a_period():
    with pytest.raises(object_materialize.DefinitionError, match="from_period"):
        object_materialize._eval_entry(
            "date", {"from_period": "period_start"}, source_row={}, period=None, definition_id="d1",
        )


def test_eval_entry_literal():
    assert object_materialize._eval_entry(
        "currency", {"literal": "USD"}, source_row={}, period=None, definition_id="d1",
    ) == "USD"


def test_eval_entry_template_interpolates_source_and_period_label():
    period = _period()
    value = object_materialize._eval_entry(
        "description", {"template": "Recurring: {source.name} ({period_label})"},
        source_row={"name": "Acme"}, period=period, definition_id="d1",
    )
    assert value == "Recurring: Acme (2026-07)"


def test_eval_entry_template_rejects_unknown_placeholder():
    with pytest.raises(object_materialize.DefinitionError, match="unknown placeholder"):
        object_materialize._eval_entry(
            "description", {"template": "{nonsense}"}, source_row={}, period=None, definition_id="d1",
        )


def test_eval_entry_if_resolves_true_and_false_branches():
    entry = {"if": {"source_field": "auto_post", "equals": True}, "then": "posted", "else": "draft"}
    assert object_materialize._eval_entry(
        "status", entry, source_row={"auto_post": "true"}, period=None, definition_id="d1",
    ) == "posted"
    assert object_materialize._eval_entry(
        "status", entry, source_row={"auto_post": "false"}, period=None, definition_id="d1",
    ) == "draft"
    assert object_materialize._eval_entry(
        "status", entry, source_row={}, period=None, definition_id="d1",
    ) == "draft"


def test_validate_mapping_entry_if_requires_then_and_else():
    with pytest.raises(object_materialize.DefinitionError, match="then.*else|'then' and 'else'"):
        object_materialize._validate_mapping_entry(
            "status", {"if": {"source_field": "x", "equals": "y"}, "then": "a"}, definition_id="d1",
        )


# --- straight-line depreciation, integer cents, remainder on final period ------

def test_straight_line_split_computes_monthly_and_remainder():
    op = {"cost_field": "cost", "salvage_field": "salvage", "life_field": "life"}
    row = {"cost": "10000", "salvage": "1000", "life": "3"}  # 9000 / 3 = 3000 exact
    monthly, remainder = object_materialize._straight_line_split(op, row, definition_id="d1", field_name="amount")
    assert monthly == 3000
    assert remainder == 0


def test_straight_line_split_remainder_lands_exactly_on_final_period():
    op = {"cost_field": "cost", "salvage_field": "salvage", "life_field": "life"}
    # depreciable = 10000 cents, life = 3 -> monthly = 3333, remainder = 1
    row = {"cost": "10000", "salvage": "0", "life": "3"}
    monthly, remainder = object_materialize._straight_line_split(op, row, definition_id="d1", field_name="amount")
    assert monthly == 3333
    assert remainder == 1
    assert monthly * 3 + remainder == 10000  # exact, no truncation drift


def test_eval_depreciation_books_monthly_except_final_period():
    op = {"cost_field": "cost", "salvage_field": "salvage", "life_field": "life"}
    row = {"cost": "10000", "salvage": "0", "life": "3"}
    p1 = _period(index=1, total=3)
    p2 = _period(index=2, total=3)
    p3 = _period(index=3, total=3)
    assert object_materialize._eval_depreciation(op, row, p1, definition_id="d1", field_name="amount") == "3333"
    assert object_materialize._eval_depreciation(op, row, p2, definition_id="d1", field_name="amount") == "3333"
    assert object_materialize._eval_depreciation(op, row, p3, definition_id="d1", field_name="amount") == "3334"


def test_eval_depreciation_requires_a_capped_scheduled_fixed_period():
    op = {"cost_field": "cost", "salvage_field": "salvage", "life_field": "life"}
    with pytest.raises(object_materialize.DefinitionError, match="scheduled_fixed"):
        object_materialize._eval_depreciation(op, {}, None, definition_id="d1", field_name="amount")
    uncapped = _period(index=None, total=None)
    with pytest.raises(object_materialize.DefinitionError, match="scheduled_fixed"):
        object_materialize._eval_depreciation(op, {}, uncapped, definition_id="d1", field_name="amount")


# --- period computation --------------------------------------------------

def test_compute_scheduled_periods_monthly_generates_one_period_per_month():
    from datetime import date
    config = object_materialize.parse_definition  # not used directly; build a minimal config via dataclass
    cfg = object_materialize.MaterializeConfig(
        definition_id="d1", name="d1", source_collection="fin_recurring", source_filter={},
        trigger_mode="scheduled", trigger_interval_seconds=3600,
        anchor_field="next_run", frequency_field="frequency",
        start_field=None, granularity=None, periods_field=None,
        output_collection="fin_journals", child_collection=None, child_source_field=None,
        child_link_field=None, idempotency_key="{definition_id}_{source_id}_{period_start}",
        mapping={}, balance_check=None, debit_account_id=None, credit_account_id=None,
        synthesized_amount_entry=None, actor="daemon:materialize", enabled=True, block=False,
        stamp_generated_from=False,
    )
    row = {"frequency": "monthly", "next_run": "2026-01-15"}
    periods = object_materialize._compute_scheduled_periods(row, cfg, now=date(2026, 4, 20))
    starts = [p.start.isoformat() for p in periods]
    assert starts == ["2026-01-15", "2026-02-15", "2026-03-15", "2026-04-15"]
    assert periods[0].label == "2026-01"
    assert periods[0].index is None and periods[0].total is None


def test_compute_scheduled_periods_daily_and_weekly_granularity():
    from datetime import date
    cfg = object_materialize.MaterializeConfig(
        definition_id="d1", name="d1", source_collection="s", source_filter={},
        trigger_mode="scheduled", trigger_interval_seconds=3600,
        anchor_field="next_run", frequency_field="frequency",
        start_field=None, granularity=None, periods_field=None,
        output_collection="o", child_collection=None, child_source_field=None,
        child_link_field=None, idempotency_key="k", mapping={}, balance_check=None,
        debit_account_id=None, credit_account_id=None, synthesized_amount_entry=None,
        actor="a", enabled=True, block=False, stamp_generated_from=False,
    )
    daily_row = {"frequency": "daily", "next_run": "2026-07-01"}
    daily_periods = object_materialize._compute_scheduled_periods(daily_row, cfg, now=date(2026, 7, 3))
    assert [p.start.isoformat() for p in daily_periods] == ["2026-07-01", "2026-07-02", "2026-07-03"]

    weekly_row = {"frequency": "weekly", "next_run": "2026-07-01"}
    weekly_periods = object_materialize._compute_scheduled_periods(weekly_row, cfg, now=date(2026, 7, 20))
    assert [p.start.isoformat() for p in weekly_periods] == ["2026-07-01", "2026-07-08", "2026-07-15"]


def test_compute_scheduled_fixed_periods_caps_at_useful_life_and_stops_at_now():
    from datetime import date
    cfg = object_materialize.MaterializeConfig(
        definition_id="d1", name="d1", source_collection="products", source_filter={},
        trigger_mode="scheduled_fixed", trigger_interval_seconds=3600,
        anchor_field=None, frequency_field=None,
        start_field="purchase_date", granularity="monthly", periods_field="useful_life_months",
        output_collection="fin_journals", child_collection="fin_journal_lines", child_source_field=None,
        child_link_field="journal_id", idempotency_key="k", mapping={}, balance_check=None,
        debit_account_id="a1", credit_account_id="a2", synthesized_amount_entry={"depreciation_amount": {}},
        actor="a", enabled=True, block=False, stamp_generated_from=False,
    )
    row = {"purchase_date": "2026-01-01", "useful_life_months": "3"}
    periods = object_materialize._compute_scheduled_fixed_periods(row, cfg, now=date(2026, 12, 31))
    assert [p.start.isoformat() for p in periods] == ["2026-01-01", "2026-02-01", "2026-03-01"]
    assert [p.index for p in periods] == [1, 2, 3]
    assert all(p.total == 3 for p in periods)

    # now before the 3rd period -- only 2 periods due yet
    partial = object_materialize._compute_scheduled_fixed_periods(row, cfg, now=date(2026, 2, 15))
    assert [p.index for p in partial] == [1, 2]


def test_month_advance_handles_day_overflow_correctly():
    from datetime import date
    assert object_materialize._add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert object_materialize._add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)  # leap year


# --- generate_definition / generate_one: full worked examples ------------------

def test_recurring_journal_generation_creates_header_and_lines(tmp_path):
    data_dir = tmp_path / "data"
    _make_fin_journals(data_dir)
    _make_fin_journal_lines(data_dir)
    template_lines = json.dumps([
        {"account_id": "acct_cash", "debit_cents": 1000, "credit_cents": 0, "memo": "cash"},
        {"account_id": "acct_rev", "debit_cents": 0, "credit_cents": 1000, "memo": "rev"},
    ])
    _make_fin_recurring(data_dir, [{
        "id": "rec1", "name": "Rent", "template_lines": template_lines,
        "frequency": "monthly", "next_run": "2026-06-01", "auto_post": "true", "is_active": "true",
    }])

    definition = _recurring_definition()
    result = object_materialize.generate_definition(definition, base_dir=data_dir, roots=[], now=None)

    # now defaults to current real time, far past 2026-06-01 -- at least one period generated
    assert result["generated"] >= 1
    assert result["errors"] == []

    journals = object_records.read_collection_records("fin_journals", base_dir=data_dir, roots=[])
    assert len(journals) >= 1
    header = journals[0]
    assert header["status"] == "posted"  # auto_post true
    assert header["currency"] == "USD"
    assert header["description"].startswith("Recurring: Rent (")
    assert json.loads(header["generated_from"])["definition_id"] == "matgen_fin_recurring"
    assert json.loads(header["generated_from"])["source_id"] == "rec1"

    lines = object_records.read_collection_records("fin_journal_lines", base_dir=data_dir, roots=[])
    assert len(lines) == 2 * len(journals)
    for line in lines:
        assert line["journal_id"] == header["id"] or line["journal_id"] in {j["id"] for j in journals}


def test_double_run_generates_no_duplicate(tmp_path):
    data_dir = tmp_path / "data"
    _make_fin_journals(data_dir)
    _make_fin_journal_lines(data_dir)
    template_lines = json.dumps([
        {"account_id": "acct_cash", "debit_cents": 500, "credit_cents": 0},
        {"account_id": "acct_rev", "debit_cents": 0, "credit_cents": 500},
    ])
    _make_fin_recurring(data_dir, [{
        "id": "rec1", "name": "Rent", "template_lines": template_lines,
        "frequency": "monthly", "next_run": "2020-01-01", "auto_post": "false", "is_active": "true",
    }])
    definition = _recurring_definition()

    first = object_materialize.generate_definition(definition, base_dir=data_dir, roots=[])
    assert first["generated"] > 0
    journal_count_after_first = len(object_records.read_collection_records("fin_journals", base_dir=data_dir, roots=[]))
    line_count_after_first = len(object_records.read_collection_records("fin_journal_lines", base_dir=data_dir, roots=[]))

    second = object_materialize.generate_definition(definition, base_dir=data_dir, roots=[])
    assert second["generated"] == 0
    assert second["skipped_already_generated"] == first["generated"]

    journal_count_after_second = len(object_records.read_collection_records("fin_journals", base_dir=data_dir, roots=[]))
    line_count_after_second = len(object_records.read_collection_records("fin_journal_lines", base_dir=data_dir, roots=[]))
    assert journal_count_after_second == journal_count_after_first
    assert line_count_after_second == line_count_after_first


def test_unbalanced_generation_writes_zero_rows(tmp_path):
    data_dir = tmp_path / "data"
    _make_fin_journals(data_dir)
    _make_fin_journal_lines(data_dir)
    unbalanced_lines = json.dumps([
        {"account_id": "acct_cash", "debit_cents": 1000, "credit_cents": 0},
        {"account_id": "acct_rev", "debit_cents": 0, "credit_cents": 999},  # off by one cent
    ])
    _make_fin_recurring(data_dir, [{
        "id": "rec1", "name": "Bad", "template_lines": unbalanced_lines,
        "frequency": "monthly", "next_run": "2020-01-01", "auto_post": "true", "is_active": "true",
    }])
    definition = _recurring_definition()

    result = object_materialize.generate_definition(definition, base_dir=data_dir, roots=[])

    assert result["generated"] == 0
    assert len(result["errors"]) >= 1
    assert "unbalanced" in result["errors"][0]["error"]

    assert object_records.read_collection_records("fin_journals", base_dir=data_dir, roots=[]) == []
    assert object_records.read_collection_records("fin_journal_lines", base_dir=data_dir, roots=[]) == []


def test_child_first_header_last_ordering_and_crash_recovery(tmp_path):
    """Simulate a crash between the last child write and the header write:
    manually write only the child rows (as generate_one would, mid-flight),
    confirm no header exists yet, then re-run the SAME generation and
    confirm it re-writes the (idempotent, already-there) children without
    error and completes by writing the header -- 61's Storage section's
    exact recovery story.
    """
    data_dir = tmp_path / "data"
    _make_fin_journals(data_dir)
    _make_fin_journal_lines(data_dir)
    template_lines = json.dumps([
        {"account_id": "acct_cash", "debit_cents": 200, "credit_cents": 0},
        {"account_id": "acct_rev", "debit_cents": 0, "credit_cents": 200},
    ])
    _make_fin_recurring(data_dir, [{
        "id": "rec1", "name": "Crash test", "template_lines": template_lines,
        "frequency": "monthly", "next_run": "2020-01-01", "auto_post": "true", "is_active": "true",
    }])
    definition = _recurring_definition()
    config = object_materialize.parse_definition(definition, base_dir=data_dir, roots=[])

    row = object_records.get_collection_record("fin_recurring", "rec1", base_dir=data_dir, roots=[])
    from datetime import date
    period = object_materialize.Period(
        start=date(2020, 1, 1), end=date(2020, 2, 1), label="2020-01", index=None, total=None,
    )
    header_id = object_materialize._render_idempotency_key(config, source_row=row, period=period)

    # Simulate the crash: write ONLY the children (as generate_one's first
    # phase would), never the header.
    child_rows = object_materialize._build_child_rows(config, source_row=row, header_id=header_id, period=period)
    for child in child_rows:
        object_records.create_collection_record(
            "fin_journal_lines", child, base_dir=data_dir, roots=[], actor=config.actor,
        )

    assert object_records.read_collection_records("fin_journals", base_dir=data_dir, roots=[]) == []
    orphans_before = object_records.read_collection_records("fin_journal_lines", base_dir=data_dir, roots=[])
    assert len(orphans_before) == 2  # orphaned, headerless -- exactly 61's "accepted, bounded" failure mode

    # Re-run the full generation -- must re-write the (now-duplicate-id)
    # children harmlessly and complete by writing the header.
    result = object_materialize.generate_one(config, row, period, base_dir=data_dir, roots=[])
    assert result["status"] == "generated"
    assert result["header_id"] == header_id

    journals = object_records.read_collection_records("fin_journals", base_dir=data_dir, roots=[])
    assert len(journals) == 1
    assert journals[0]["id"] == header_id

    lines = object_records.read_collection_records("fin_journal_lines", base_dir=data_dir, roots=[])
    assert len(lines) == 2  # no duplicate lines despite the pre-existing orphans


def test_depreciation_generation_books_straight_line_with_remainder_on_final_period(tmp_path):
    data_dir = tmp_path / "data"
    _make_fin_journals(data_dir)
    _make_fin_journal_lines(data_dir)
    _make_products(data_dir, [{
        "id": "asset1", "name": "Laptop", "product_type": "asset",
        "purchase_date": "2026-01-01", "purchase_cost_cents": "100000",
        "salvage_value_cents": "0", "useful_life_months": "3",
        "depreciation_method": "straight_line",
    }])
    definition = _depreciation_definition()

    from datetime import date, datetime, timezone
    now = datetime(2026, 4, 1, tzinfo=timezone.utc)  # past all 3 periods
    result = object_materialize.generate_definition(definition, base_dir=data_dir, roots=[], now=now)

    assert result["generated"] == 3
    assert result["errors"] == []

    journals = object_records.read_collection_records("fin_journals", base_dir=data_dir, roots=[])
    assert len(journals) == 3
    lines = object_records.read_collection_records("fin_journal_lines", base_dir=data_dir, roots=[])
    assert len(lines) == 6

    # 100000 // 3 = 33333, remainder 1 -- final period books 33334.
    amounts_by_journal = {}
    for journal in journals:
        journal_lines = [line for line in lines if line["journal_id"] == journal["id"]]
        debit_line = next(line for line in journal_lines if int(line["debit_cents"]) > 0)
        amounts_by_journal[journal["date"]] = int(debit_line["debit_cents"])

    ordered = [amounts_by_journal[d] for d in sorted(amounts_by_journal)]
    assert ordered == [33333, 33333, 33334]
    assert sum(ordered) == 100000  # exact, no truncation drift

    # Both lines of every journal carry the declared accounts.
    for journal in journals:
        journal_lines = [line for line in lines if line["journal_id"] == journal["id"]]
        accounts = {line["account_id"] for line in journal_lines}
        assert accounts == {"acct_expense", "acct_accum_depr"}


def test_creatework_fills_only_empty_fields_via_mapping(tmp_path):
    data_dir = tmp_path / "data"
    _make_tasks(data_dir, [
        {"id": "task1", "title": "Existing title", "description": "", "template_id": ""},
    ])
    definition = _creatework_definition(mapping=json.dumps({
        "description": {"literal": "seeded description"},
        "title": {"literal": "should never appear"},
    }))
    row = object_records.get_collection_record("tasks", "task1", base_dir=data_dir, roots=[])

    config = object_materialize.parse_definition(definition, base_dir=data_dir, roots=[])
    result = object_materialize.generate_one(config, row, None, base_dir=data_dir, roots=[])
    assert result["status"] == "generated"

    updated = object_records.get_collection_record("tasks", "task1", base_dir=data_dir, roots=[])
    assert updated["title"] == "Existing title"  # never clobbered -- already non-empty
    assert updated["description"] == "seeded description"  # filled -- was empty


def test_creatework_applies_template_default_values_fill_only_if_empty(tmp_path):
    data_dir = tmp_path / "data"
    _make_templates(data_dir, [{
        "id": "tmpl1", "name": "Onboarding", "default_values": json.dumps({
            "description": "from template", "title": "should not overwrite",
        }),
    }])
    _make_tasks(data_dir, [
        {"id": "task1", "title": "My own title", "description": "", "template_id": "tmpl1"},
    ])
    definition = _creatework_definition(mapping=json.dumps({}))
    row = object_records.get_collection_record("tasks", "task1", base_dir=data_dir, roots=[])

    config = object_materialize.parse_definition(definition, base_dir=data_dir, roots=[])
    result = object_materialize.generate_one(config, row, None, base_dir=data_dir, roots=[])
    assert result["status"] == "generated"

    updated = object_records.get_collection_record("tasks", "task1", base_dir=data_dir, roots=[])
    assert updated["title"] == "My own title"  # fill-only-if-empty -- never clobbered
    assert updated["description"] == "from template"  # filled from the template


def test_creatework_second_run_is_a_noop(tmp_path):
    data_dir = tmp_path / "data"
    _make_tasks(data_dir, [{"id": "task1", "title": "T", "description": "", "template_id": ""}])
    definition = _creatework_definition(mapping=json.dumps({"description": {"literal": "x"}}))
    config = object_materialize.parse_definition(definition, base_dir=data_dir, roots=[])

    row = object_records.get_collection_record("tasks", "task1", base_dir=data_dir, roots=[])
    first = object_materialize.generate_one(config, row, None, base_dir=data_dir, roots=[])
    assert first["status"] == "generated"

    row = object_records.get_collection_record("tasks", "task1", base_dir=data_dir, roots=[])
    second = object_materialize.generate_one(config, row, None, base_dir=data_dir, roots=[])
    assert second["status"] == "skipped_already_generated"


# --- per-row isolation --------------------------------------------------

def test_generate_config_isolates_one_bad_row_from_others(tmp_path):
    data_dir = tmp_path / "data"
    _make_fin_journals(data_dir)
    _make_fin_journal_lines(data_dir)
    good_lines = json.dumps([
        {"account_id": "acct_cash", "debit_cents": 100, "credit_cents": 0},
        {"account_id": "acct_rev", "debit_cents": 0, "credit_cents": 100},
    ])
    _make_fin_recurring(data_dir, [
        {"id": "bad", "name": "Bad row", "template_lines": "{not valid json",
         "frequency": "monthly", "next_run": "2020-01-01", "auto_post": "true", "is_active": "true"},
        {"id": "good", "name": "Good row", "template_lines": good_lines,
         "frequency": "monthly", "next_run": "2020-01-01", "auto_post": "true", "is_active": "true"},
    ])
    definition = _recurring_definition()

    result = object_materialize.generate_definition(definition, base_dir=data_dir, roots=[])

    assert result["generated"] > 0  # the good row still generated
    assert any(e["source_id"] == "bad" for e in result["errors"])  # the bad row is reported, isolated
    journals = object_records.read_collection_records("fin_journals", base_dir=data_dir, roots=[])
    assert all(json.loads(j["generated_from"])["source_id"] == "good" for j in journals)


def test_generate_definition_raises_for_a_malformed_definition_itself(tmp_path):
    """A malformed DEFINITION (not a bad row) raises -- the caller (daemon/
    materialize_run) is responsible for that level of isolation, per
    module docstring.
    """
    data_dir = tmp_path / "data"
    with pytest.raises(object_materialize.DefinitionError):
        object_materialize.generate_definition(
            _recurring_definition(idempotency_key=""), base_dir=data_dir, roots=[],
        )


# --- flags --------------------------------------------------

def test_materialize_pass_enabled_defaults_true_without_feature_flags_collection(tmp_path):
    data_dir = tmp_path / "data"
    assert object_materialize.materialize_pass_enabled(base_dir=data_dir) is True


def test_materialize_pass_enabled_honors_off_flag(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(data_dir, "feature_flags", [{"name": "id"}, {"name": "flag", "type": "text"}, {"name": "value", "type": "text"}])
    object_records.create_collection_record(
        "feature_flags", {"id": "f1", "flag": "materialize_enabled", "value": "off"}, base_dir=data_dir, roots=[],
    )
    assert object_materialize.materialize_pass_enabled(base_dir=data_dir) is False


def test_is_definition_enabled_and_blocked_defaults():
    assert object_materialize.is_definition_enabled({}) is True
    assert object_materialize.is_definition_enabled({"enabled": "false"}) is False
    assert object_materialize.is_definition_blocked({}) is False
    assert object_materialize.is_definition_blocked({"block": "true"}) is True


def test_is_definition_due_never_run_is_always_due():
    assert object_materialize.is_definition_due({"id": "d1"}) is True


def test_is_definition_due_respects_interval(tmp_path):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    record = {
        "id": "d1", "last_run_at": now.isoformat().replace("+00:00", "Z"),
        "trigger": json.dumps({"mode": "scheduled", "interval_seconds": 3600, "period_field": "f", "anchor_field": "a"}),
    }
    assert object_materialize.is_definition_due(record, now=now + timedelta(seconds=10)) is False
    assert object_materialize.is_definition_due(record, now=now + timedelta(hours=2)) is True


# --- event-mode helpers --------------------------------------------------

def test_event_definitions_for_collection_filters_by_mode_and_source(tmp_path):
    data_dir = tmp_path / "data"
    _make_tasks(data_dir, [])
    write_schema(data_dir, "materialize_definitions", [
        {"name": "id"}, {"name": "source_collection", "type": "text"},
        {"name": "trigger", "type": "textarea"}, {"name": "enabled", "type": "boolean"},
        {"name": "block", "type": "boolean"},
    ])
    object_records.create_collection_record(
        "materialize_definitions", _creatework_definition(), base_dir=data_dir, roots=[], actor="tester",
    )
    object_records.create_collection_record(
        "materialize_definitions",
        {"id": "not_event", "source_collection": "tasks",
         "trigger": json.dumps({"mode": "scheduled", "interval_seconds": 3600, "period_field": "f", "anchor_field": "a"}),
         "enabled": "true", "block": "false"},
        base_dir=data_dir, roots=[], actor="tester",
    )

    matches = object_materialize.event_definitions_for_collection("tasks", base_dir=data_dir, roots=[])
    assert [m["id"] for m in matches] == ["matgen_task_seed"]


def test_compute_event_handles_returns_sorted_distinct_events(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(data_dir, "materialize_definitions", [
        {"name": "id"}, {"name": "source_collection", "type": "text"},
        {"name": "trigger", "type": "textarea"}, {"name": "enabled", "type": "boolean"},
        {"name": "block", "type": "boolean"},
    ])
    object_records.create_collection_record(
        "materialize_definitions",
        _creatework_definition(id="d1", source_collection="tasks"),
        base_dir=data_dir, roots=[], actor="tester",
    )
    object_records.create_collection_record(
        "materialize_definitions",
        _creatework_definition(id="d2", source_collection="deals"),
        base_dir=data_dir, roots=[], actor="tester",
    )
    object_records.create_collection_record(
        "materialize_definitions",
        _creatework_definition(id="d3", source_collection="tasks", block="true"),
        base_dir=data_dir, roots=[], actor="tester",
    )

    events = object_materialize.compute_event_handles(base_dir=data_dir, roots=[])
    assert events == ["deals.record.created", "tasks.record.created"]


def test_generate_one_event_rejects_non_event_definition(tmp_path):
    data_dir = tmp_path / "data"
    _make_fin_recurring(data_dir, [{
        "id": "rec1", "name": "R", "template_lines": "[]", "frequency": "monthly",
        "next_run": "2020-01-01", "auto_post": "false", "is_active": "true",
    }])
    _make_fin_journals(data_dir)
    _make_fin_journal_lines(data_dir)
    row = object_records.get_collection_record("fin_recurring", "rec1", base_dir=data_dir, roots=[])
    with pytest.raises(object_materialize.DefinitionError, match="not an event-mode definition"):
        object_materialize.generate_one_event(_recurring_definition(), row, base_dir=data_dir, roots=[])
