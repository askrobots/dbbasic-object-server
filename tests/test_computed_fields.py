"""Formula & rollup fields (plan/formula-rollup-spec.md): derived values,
materialized on write at the storage layer -- so EVERY writer (HTTP, daemon,
seeds, imports) keeps them consistent, and every surface (lists, tables,
detail, search, filters, realtime, backups) sees plain stored values.

Formulas recompute on writes of the record itself; rollups recompute on
create/update/delete in the source collection (single-hop, guarded). A broken
formula yields "" and never fails the write -- derived values are
non-authoritative, the opposite posture of pre-write hooks.
"""

import json
from pathlib import Path

import pytest

import object_computed
import object_records


def _setup(tmp_path, schemas: dict[str, dict], rows: dict[str, str]):
    data_dir = tmp_path / "data"
    schema_dir = data_dir / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    for name, schema in schemas.items():
        (schema_dir / f"{name}.json").write_text(json.dumps(schema))
    for name, content in rows.items():
        collection_dir = data_dir / "collections" / name
        collection_dir.mkdir(parents=True, exist_ok=True)
        (collection_dir / "records.tsv").write_text(content)
    return data_dir


CONTACTS = {
    "name": "contacts",
    "fields": [
        {"name": "id"},
        {"name": "first_name", "type": "text"},
        {"name": "last_name", "type": "text"},
        {"name": "full_name", "type": "computed",
         "formula": 'first_name + " " + last_name'},
    ],
}

INVOICES = {
    "name": "invs",
    "fields": [
        {"name": "id"},
        {"name": "customer", "type": "text"},
        {"name": "total_cents", "type": "computed",
         "rollup": {"collection": "inv_lines", "fk_field": "invoice_id",
                    "op": "sum", "field": "amount_cents"}},
        {"name": "line_count", "type": "computed",
         "rollup": {"collection": "inv_lines", "fk_field": "invoice_id",
                    "op": "count"}},
        {"name": "display", "type": "computed",
         "formula": 'customer + ": " + total_cents'},
    ],
}

INV_LINES = {
    "name": "inv_lines",
    "fields": [
        {"name": "id"},
        {"name": "invoice_id", "type": "text"},
        {"name": "amount_cents", "type": "integer"},
        {"name": "kind", "type": "text"},
    ],
}


# ---------------------------------------------------------------------------
# The expression language itself
# ---------------------------------------------------------------------------

def test_formula_language_shapes():
    ev = object_computed.evaluate_formula
    assert ev('first + " " + last', {"first": "Grace", "last": "Hopper"}) == "Grace Hopper"
    assert ev("qty * price_cents", {"qty": "3", "price_cents": "250"}) == "750"
    assert ev("(a + b) * 2", {"a": "1", "b": "2"}) == "6"
    assert ev("a - b", {"a": "10", "b": "4"}) == "6"
    assert ev("-a + 5", {"a": "2"}) == "3"
    assert ev('"fixed"', {}) == "fixed"
    # numeric-looking strings add numerically; anything else concatenates
    assert ev("a + b", {"a": "1.5", "b": "2.5"}) == "4"
    assert ev("a + b", {"a": "x", "b": "1"}) == "x1"
    with pytest.raises(object_computed.FormulaError):
        ev("a / b", {"a": "1", "b": "0"})
    with pytest.raises(object_computed.FormulaError):
        ev("a * b", {"a": "text", "b": "2"})


# ---------------------------------------------------------------------------
# Formulas at the storage layer
# ---------------------------------------------------------------------------

def test_formula_computed_on_create_and_recomputed_on_update(tmp_path):
    data_dir = _setup(tmp_path, {"contacts": CONTACTS},
                      {"contacts": "id\tfirst_name\tlast_name\tfull_name\n"})
    stored = object_records.create_collection_record(
        "contacts", {"id": "c1", "first_name": "Grace", "last_name": "Hopper"},
        base_dir=data_dir)
    assert stored["full_name"] == "Grace Hopper"

    object_records.update_collection_record(
        "contacts", "c1", {"last_name": "Murray"}, base_dir=data_dir)
    row = object_records.get_collection_record("contacts", "c1", base_dir=data_dir)
    assert row["full_name"] == "Grace Murray"


def test_broken_formula_yields_empty_and_write_succeeds(tmp_path):
    schema = {
        "name": "things",
        "fields": [
            {"name": "id"},
            {"name": "a", "type": "text"},
            {"name": "bad", "type": "computed", "formula": "a / 0"},
        ],
    }
    data_dir = _setup(tmp_path, {"things": schema}, {"things": "id\ta\tbad\n"})
    stored = object_records.create_collection_record(
        "things", {"id": "t1", "a": "5"}, base_dir=data_dir)
    assert stored["bad"] == ""


def test_client_still_cannot_submit_a_computed_field(tmp_path):
    data_dir = _setup(tmp_path, {"contacts": CONTACTS},
                      {"contacts": "id\tfirst_name\tlast_name\tfull_name\n"})
    with pytest.raises(object_records.InvalidRecordPayloadError):
        object_records.create_collection_record(
            "contacts",
            {"id": "c1", "first_name": "G", "last_name": "H", "full_name": "forged"},
            base_dir=data_dir)


# ---------------------------------------------------------------------------
# Rollups: child writes keep the parent current
# ---------------------------------------------------------------------------

def _invoice_env(tmp_path):
    return _setup(
        tmp_path,
        {"invs": INVOICES, "inv_lines": INV_LINES},
        {
            "invs": "id\tcustomer\ttotal_cents\tline_count\tdisplay\ni1\tAcme\t\t\t\ni2\tBlue\t\t\t\n",
            "inv_lines": "id\tinvoice_id\tamount_cents\tkind\n",
        },
    )


def _invoice(data_dir, invoice_id="i1"):
    return object_records.get_collection_record("invs", invoice_id, base_dir=data_dir)


def test_rollup_updates_on_child_create_update_delete(tmp_path):
    data_dir = _invoice_env(tmp_path)
    object_records.create_collection_record(
        "inv_lines", {"id": "l1", "invoice_id": "i1", "amount_cents": "1000"},
        base_dir=data_dir)
    assert _invoice(data_dir)["total_cents"] == "1000"
    assert _invoice(data_dir)["line_count"] == "1"

    object_records.create_collection_record(
        "inv_lines", {"id": "l2", "invoice_id": "i1", "amount_cents": "250"},
        base_dir=data_dir)
    assert _invoice(data_dir)["total_cents"] == "1250"
    assert _invoice(data_dir)["line_count"] == "2"

    object_records.update_collection_record(
        "inv_lines", "l2", {"amount_cents": "500"}, base_dir=data_dir)
    assert _invoice(data_dir)["total_cents"] == "1500"

    object_records.delete_collection_record("inv_lines", "l1", base_dir=data_dir)
    assert _invoice(data_dir)["total_cents"] == "500"
    assert _invoice(data_dir)["line_count"] == "1"


def test_parent_formula_sees_fresh_rollup_value(tmp_path):
    data_dir = _invoice_env(tmp_path)
    object_records.create_collection_record(
        "inv_lines", {"id": "l1", "invoice_id": "i1", "amount_cents": "700"},
        base_dir=data_dir)
    assert _invoice(data_dir)["display"] == "Acme: 700"


def test_only_the_affected_parent_recomputes(tmp_path):
    data_dir = _invoice_env(tmp_path)
    object_records.create_collection_record(
        "inv_lines", {"id": "l1", "invoice_id": "i2", "amount_cents": "42"},
        base_dir=data_dir)
    assert _invoice(data_dir, "i2")["total_cents"] == "42"
    # i1 untouched: still the header's empty value, no phantom recompute
    assert _invoice(data_dir, "i1")["total_cents"] == ""


def test_rollup_where_filter_and_ops(tmp_path):
    schema = dict(INVOICES)
    schema["fields"] = list(INVOICES["fields"]) + [
        {"name": "fee_total", "type": "computed",
         "rollup": {"collection": "inv_lines", "fk_field": "invoice_id",
                    "op": "sum", "field": "amount_cents",
                    "where": {"kind": "fee"}}},
        {"name": "biggest", "type": "computed",
         "rollup": {"collection": "inv_lines", "fk_field": "invoice_id",
                    "op": "max", "field": "amount_cents"}},
    ]
    data_dir = _setup(
        tmp_path,
        {"invs": schema, "inv_lines": INV_LINES},
        {
            "invs": "id\tcustomer\ttotal_cents\tline_count\tdisplay\tfee_total\tbiggest\ni1\tAcme\t\t\t\t\t\n",
            "inv_lines": "id\tinvoice_id\tamount_cents\tkind\n",
        },
    )
    for line_id, cents, kind in (("l1", "100", "fee"), ("l2", "900", "work"), ("l3", "50", "fee")):
        object_records.create_collection_record(
            "inv_lines",
            {"id": line_id, "invoice_id": "i1", "amount_cents": cents, "kind": kind},
            base_dir=data_dir)
    row = _invoice(data_dir)
    assert row["total_cents"] == "1050"
    assert row["fee_total"] == "150"
    assert row["biggest"] == "900"


def test_backfill_helper_recomputes_existing_rows(tmp_path):
    data_dir = _setup(
        tmp_path,
        {"contacts": CONTACTS},
        {"contacts": "id\tfirst_name\tlast_name\tfull_name\nc1\tAda\tLovelace\t\nc2\tAlan\tTuring\tSTALE\n"},
    )
    changed = object_records.recompute_computed_fields("contacts", base_dir=data_dir)
    assert changed == 2
    rows = {r["id"]: r for r in object_records.read_collection_records("contacts", base_dir=data_dir)}
    assert rows["c1"]["full_name"] == "Ada Lovelace"
    assert rows["c2"]["full_name"] == "Alan Turing"
    # second run is a no-op
    assert object_records.recompute_computed_fields("contacts", base_dir=data_dir) == 0
