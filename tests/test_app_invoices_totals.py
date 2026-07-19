"""Behavior tests for packages/app-invoices: the invoice_totals HANDLES
handler (integer-cents arithmetic, floor-not-round tax, idempotence,
delete handling) and the guarded status transitions on invoices.status.

Structural/manifest/permission tests live in tests/test_app_invoices_package.py.
"""

import os
from pathlib import Path

import pytest

import object_execution
import object_packages
import object_permissions
import object_record_changes
import object_records
import python_object_runtime

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"


def _install(tmp_path):
    """Install app-invoices (+ app-contacts, for the customer_id relation
    target) into an isolated data dir/object root and return
    (data_dir, object_root, runtime).
    """
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    # conftest's isolated_default_data_dir fixture already points
    # DBBASIC_DATA_DIR at tmp_path / "data" for this test process; keep
    # them in sync so invoice_totals.py's own env-based _data_dir()
    # resolves to the same directory this test writes into.
    os.environ["DBBASIC_DATA_DIR"] = str(data_dir)

    object_packages.install_package(
        "app-invoices", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root]
    )
    runtime = python_object_runtime.PythonObjectRuntime(base_dir=data_dir)
    return data_dir, object_root, runtime


def _fire(runtime, object_root, record_id, action):
    return object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest(
            "system_invoice_totals",
            method="EVENT",
            payload={
                "event": f"invoice_lines.record.{action}",
                "collection": "invoice_lines",
                "record_id": record_id,
                "action": action,
            },
        ),
        roots=[object_root],
    )


def _make_invoice(data_dir, **overrides):
    record = {
        "id": "inv_1",
        "number": "INV-0001",
        "customer_name": "Example Consulting LLC",
        "owner_id": "u1",
    }
    record.update(overrides)
    return object_records.create_collection_record("invoices", record, base_dir=data_dir, actor="test")


def _make_line(data_dir, **overrides):
    record = {
        "invoice_id": "inv_1",
        "description": "Line item",
        "quantity": "1",
        "unit_price_cents": "100",
        "owner_id": "u1",
    }
    record.update(overrides)
    return object_records.create_collection_record("invoice_lines", record, base_dir=data_dir, actor="test")


def test_line_total_is_quantity_times_unit_price_floored(tmp_path):
    data_dir, object_root, runtime = _install(tmp_path)
    _make_invoice(data_dir)
    _make_line(data_dir, id="il_1", quantity="3", unit_price_cents="1999")

    result = _fire(runtime, object_root, "il_1", "created")
    assert result.ok is True

    line = object_records.get_collection_record("invoice_lines", "il_1", base_dir=data_dir)
    assert line["line_total_cents"] == "5997"  # 3 * 1999


def test_line_tax_uses_basis_points_floored_not_rounded(tmp_path):
    """8.25% (825 bps) of 5997 cents is 494.7825 cents -- must floor to
    494, never round to 495, per 20-invoice-spec.md's 'exact arithmetic,
    floor not round' instruction.
    """
    data_dir, object_root, runtime = _install(tmp_path)
    _make_invoice(data_dir)
    _make_line(data_dir, id="il_1", quantity="3", unit_price_cents="1999", tax_rate_bps="825")

    _fire(runtime, object_root, "il_1", "created")

    line = object_records.get_collection_record("invoice_lines", "il_1", base_dir=data_dir)
    assert line["line_total_cents"] == "5997"
    assert line["line_tax_cents"] == "494"


def test_multi_line_invoice_totals_match_hand_computed_cents(tmp_path):
    """The worked example this package's report cites:

    Line 1: qty 3, unit_price_cents 1999, tax_rate_bps 825
        line_total = 3 * 1999 = 5997
        line_tax   = 5997 * 825 // 10000 = 494  (floor of 494.7825)
    Line 2: qty 1, unit_price_cents 5000, tax_rate_bps 0
        line_total = 5000
        line_tax   = 0
    subtotal_cents    = 5997 + 5000 = 10997
    tax_cents         = 494 + 0     = 494
    total_cents       = 10997 + 494 = 11491
    amount_paid_cents = 2000 (set directly on the invoice, simulating a
                              future partial-payment write)
    balance_due_cents = 11491 - 2000 = 9491
    """
    data_dir, object_root, runtime = _install(tmp_path)
    _make_invoice(data_dir, amount_paid_cents="2000")
    _make_line(data_dir, id="il_1", description="Consulting hours",
               quantity="3", unit_price_cents="1999", tax_rate_bps="825")
    _make_line(data_dir, id="il_2", description="Software license",
               quantity="1", unit_price_cents="5000", tax_rate_bps="0")

    _fire(runtime, object_root, "il_1", "created")
    _fire(runtime, object_root, "il_2", "created")

    invoice = object_records.get_collection_record("invoices", "inv_1", base_dir=data_dir)
    assert invoice["subtotal_cents"] == "10997"
    assert invoice["tax_cents"] == "494"
    assert invoice["total_cents"] == "11491"
    assert invoice["balance_due_cents"] == "9491"


def test_fractional_quantity_uses_decimal_not_float(tmp_path):
    """2.5 hours at 10000 cents/hr must floor to exactly 25000 cents --
    Decimal arithmetic, never a bare float multiplication that could
    introduce binary rounding error.
    """
    data_dir, object_root, runtime = _install(tmp_path)
    _make_invoice(data_dir)
    _make_line(data_dir, id="il_1", quantity="2.5", unit_price_cents="10000")

    _fire(runtime, object_root, "il_1", "created")

    line = object_records.get_collection_record("invoice_lines", "il_1", base_dir=data_dir)
    assert line["line_total_cents"] == "25000"


def test_recompute_is_idempotent_and_skips_a_no_op_write(tmp_path):
    data_dir, object_root, runtime = _install(tmp_path)
    _make_invoice(data_dir)
    _make_line(data_dir, id="il_1", quantity="3", unit_price_cents="1999", tax_rate_bps="825")

    first = _fire(runtime, object_root, "il_1", "created")
    assert first.result["changed"] is True

    changes_before = object_record_changes.list_record_changes(
        "invoices", record_id="inv_1", base_dir=data_dir
    )["total"]

    second = _fire(runtime, object_root, "il_1", "updated")
    assert second.result["changed"] is False

    changes_after = object_record_changes.list_record_changes(
        "invoices", record_id="inv_1", base_dir=data_dir
    )["total"]
    assert changes_after == changes_before  # no redundant write/record_change


def test_deleting_a_line_recovers_invoice_id_from_record_changes_and_resums(tmp_path):
    """The deleted line is already gone by the time this handler's
    post-commit dispatch fires -- it must recover invoice_id from the
    record_changes log's own 'before' snapshot, not the (now 404) live
    record.
    """
    data_dir, object_root, runtime = _install(tmp_path)
    _make_invoice(data_dir)
    _make_line(data_dir, id="il_1", quantity="3", unit_price_cents="1999", tax_rate_bps="825")
    _make_line(data_dir, id="il_2", quantity="1", unit_price_cents="5000")
    _fire(runtime, object_root, "il_1", "created")
    _fire(runtime, object_root, "il_2", "created")

    invoice = object_records.get_collection_record("invoices", "inv_1", base_dir=data_dir)
    assert invoice["total_cents"] == "11491"

    object_records.delete_collection_record("invoice_lines", "il_2", base_dir=data_dir, actor="test")
    result = _fire(runtime, object_root, "il_2", "deleted")
    assert result.ok is True
    assert result.result["invoice_id"] == "inv_1"

    invoice_after = object_records.get_collection_record("invoices", "inv_1", base_dir=data_dir)
    assert invoice_after["subtotal_cents"] == "5997"
    assert invoice_after["tax_cents"] == "494"
    assert invoice_after["total_cents"] == "6491"


def test_zero_lines_leaves_invoice_at_zero_totals(tmp_path):
    data_dir, object_root, runtime = _install(tmp_path)
    _make_invoice(data_dir)

    invoice = object_records.get_collection_record("invoices", "inv_1", base_dir=data_dir)
    assert invoice["subtotal_cents"] == "0"
    assert invoice["total_cents"] == "0"
    assert invoice["balance_due_cents"] == "0"


def test_event_for_an_unrelated_collection_is_a_no_op(tmp_path):
    data_dir, object_root, runtime = _install(tmp_path)
    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest(
            "system_invoice_totals",
            method="EVENT",
            payload={"event": "notes.record.created", "collection": "notes",
                     "record_id": "n1", "action": "created"},
        ),
        roots=[object_root],
    )
    assert result.ok is True
    assert result.result["skipped"] == "not an invoice_lines event"


# -- Guarded status transitions -------------------------------------------
#
# Exercised directly against object_records' own guard (the same function
# update_collection_record calls internally) rather than round-tripping
# through the full HTTP server for one field's worth of behavior --
# requires the schema actually installed in a data dir first, since
# get_schema resolves from base_dir, not from package source.

def test_owner_may_move_draft_to_sent(tmp_path):
    data_dir, _object_root, _runtime = _install(tmp_path)
    subject = object_permissions.PermissionSubject(user_id="u1")
    existing = {"status": "draft", "owner_id": "u1"}
    updated = {"status": "sent", "owner_id": "u1"}

    object_records._validate_field_transitions(
        "invoices", existing, updated, base_dir=data_dir, roots=None, subject=subject,
    )  # no exception == allowed


def test_non_owner_may_not_move_draft_to_sent(tmp_path):
    data_dir, _object_root, _runtime = _install(tmp_path)
    subject = object_permissions.PermissionSubject(user_id="someone_else")
    existing = {"status": "draft", "owner_id": "u1"}
    updated = {"status": "sent", "owner_id": "u1"}

    with pytest.raises(object_records.TransitionNotAllowedError):
        object_records._validate_field_transitions(
            "invoices", existing, updated, base_dir=data_dir, roots=None, subject=subject,
        )


def test_paid_and_void_are_terminal(tmp_path):
    data_dir, _object_root, _runtime = _install(tmp_path)
    subject = object_permissions.PermissionSubject(user_id="u1")

    for terminal_status in ("paid", "void"):
        existing = {"status": terminal_status, "owner_id": "u1"}
        updated = {"status": "draft", "owner_id": "u1"}
        with pytest.raises(object_records.InvalidRecordPayloadError):
            object_records._validate_field_transitions(
                "invoices", existing, updated, base_dir=data_dir, roots=None, subject=subject,
            )


def test_sent_to_partial_then_partial_to_paid_is_a_legal_path(tmp_path):
    data_dir, _object_root, _runtime = _install(tmp_path)
    subject = object_permissions.PermissionSubject(user_id="u1")

    object_records._validate_field_transitions(
        "invoices", {"status": "sent", "owner_id": "u1"}, {"status": "partial", "owner_id": "u1"},
        base_dir=data_dir, roots=None, subject=subject,
    )
    object_records._validate_field_transitions(
        "invoices", {"status": "partial", "owner_id": "u1"}, {"status": "paid", "owner_id": "u1"},
        base_dir=data_dir, roots=None, subject=subject,
    )
