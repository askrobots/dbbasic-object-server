"""Behavior tests for object_finance.py: journal_totals() and
trial_balance(), the pure computed helpers packages/app-finance ships in
place of a totals-stamping handler (see object_finance.py's module
docstring for why there is no HANDLES handler in this package).

Structural/manifest/permission tests for packages/app-finance live in
tests/test_app_finance_package.py.
"""

import os
from pathlib import Path

import pytest

import object_finance
import object_packages
import object_permissions
import object_records

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"


def _install(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    # Keep DBBASIC_DATA_DIR in sync with the base_dir this test writes
    # into, same belt-and-suspenders as tests/test_app_invoices_totals.py.
    os.environ["DBBASIC_DATA_DIR"] = str(data_dir)

    object_packages.install_package(
        "app-finance", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root],
    )
    return data_dir


def _make_account(data_dir, **overrides):
    record = {"name": "Cash", "account_type": "asset", "owner_id": "u1"}
    record.update(overrides)
    return object_records.create_collection_record("fin_accounts", record, base_dir=data_dir, actor="test")


def _make_default_account(data_dir):
    """fin_journal_lines.account_id is a validated relation (the target
    record must already exist -- see object_records._validate_field_
    relation), so every _make_line() call below needs a real fin_accounts
    row at id "a_1" (its own default account_id) to point at.
    """
    _make_account(data_dir, id="a_1")


def _make_journal(data_dir, **overrides):
    record = {"date": "2026-07-01", "description": "Opening balance", "owner_id": "u1"}
    record.update(overrides)
    return object_records.create_collection_record("fin_journals", record, base_dir=data_dir, actor="test")


def _make_line(data_dir, **overrides):
    record = {"journal_id": "j_1", "account_id": "a_1", "owner_id": "u1"}
    record.update(overrides)
    return object_records.create_collection_record("fin_journal_lines", record, base_dir=data_dir, actor="test")


# -- journal_totals --------------------------------------------------------

def test_journal_totals_folds_debits_and_credits(tmp_path):
    data_dir = _install(tmp_path)
    _make_default_account(data_dir)
    _make_journal(data_dir, id="j_1")
    _make_line(data_dir, id="l_1", journal_id="j_1", debit_cents="1000", credit_cents="0")
    _make_line(data_dir, id="l_2", journal_id="j_1", debit_cents="0", credit_cents="1000")

    totals = object_finance.journal_totals("j_1", base_dir=data_dir)
    assert totals["total_debits_cents"] == 1000
    assert totals["total_credits_cents"] == 1000
    assert totals["is_balanced"] is True


def test_journal_totals_is_balanced_only_when_debits_equal_credits(tmp_path):
    data_dir = _install(tmp_path)
    _make_default_account(data_dir)
    _make_journal(data_dir, id="j_1")
    _make_line(data_dir, id="l_1", journal_id="j_1", debit_cents="1500", credit_cents="0")
    _make_line(data_dir, id="l_2", journal_id="j_1", debit_cents="0", credit_cents="1000")

    totals = object_finance.journal_totals("j_1", base_dir=data_dir)
    assert totals["total_debits_cents"] == 1500
    assert totals["total_credits_cents"] == 1000
    assert totals["is_balanced"] is False


def test_journal_totals_ignores_lines_belonging_to_other_journals(tmp_path):
    data_dir = _install(tmp_path)
    _make_default_account(data_dir)
    _make_journal(data_dir, id="j_1")
    _make_journal(data_dir, id="j_2")
    _make_line(data_dir, id="l_1", journal_id="j_1", debit_cents="500", credit_cents="500")
    _make_line(data_dir, id="l_2", journal_id="j_2", debit_cents="9999", credit_cents="1")

    totals = object_finance.journal_totals("j_1", base_dir=data_dir)
    assert totals["total_debits_cents"] == 500
    assert totals["total_credits_cents"] == 500


def test_journal_totals_zero_lines_is_balanced_at_zero(tmp_path):
    data_dir = _install(tmp_path)
    _make_journal(data_dir, id="j_1")

    totals = object_finance.journal_totals("j_1", base_dir=data_dir)
    assert totals == {
        "total_debits_cents": 0,
        "total_credits_cents": 0,
        "is_balanced": True,
    }


def test_journal_totals_unknown_journal_id_folds_to_zero(tmp_path):
    data_dir = _install(tmp_path)

    totals = object_finance.journal_totals("does-not-exist", base_dir=data_dir)
    assert totals["total_debits_cents"] == 0
    assert totals["total_credits_cents"] == 0
    assert totals["is_balanced"] is True


# -- trial_balance -----------------------------------------------------------

def test_trial_balance_sums_posted_journals_only(tmp_path):
    """A draft journal's lines are excluded, matching the source's own
    report semantics (reconciled against a private predecessor-system
    audit, not part of this repo: reports filter posted lines).
    """
    data_dir = _install(tmp_path)
    _make_account(data_dir, id="a_cash", name="Cash", code="1000", account_type="asset")
    _make_account(data_dir, id="a_rev", name="Revenue", code="4000", account_type="income")

    _make_journal(data_dir, id="j_posted", status="posted")
    _make_line(data_dir, id="l_1", journal_id="j_posted", account_id="a_cash",
               debit_cents="10000", credit_cents="0")
    _make_line(data_dir, id="l_2", journal_id="j_posted", account_id="a_rev",
               debit_cents="0", credit_cents="10000")

    _make_journal(data_dir, id="j_draft", status="draft")
    _make_line(data_dir, id="l_3", journal_id="j_draft", account_id="a_cash",
               debit_cents="99999", credit_cents="0")

    rows = object_finance.trial_balance(base_dir=data_dir, owner="u1")
    by_account = {row["account_id"]: row for row in rows}

    assert by_account["a_cash"]["debit_total_cents"] == 10000
    assert by_account["a_cash"]["credit_total_cents"] == 0
    assert by_account["a_rev"]["debit_total_cents"] == 0
    assert by_account["a_rev"]["credit_total_cents"] == 10000
    # the draft journal's 99999 never shows up anywhere
    assert all(row["debit_total_cents"] != 99999 for row in rows)


def test_trial_balance_excludes_other_owners_journals(tmp_path):
    data_dir = _install(tmp_path)
    _make_account(data_dir, id="a_cash", name="Cash", account_type="asset", owner_id="u1")
    _make_journal(data_dir, id="j_mine", status="posted", owner_id="u1")
    _make_line(data_dir, id="l_1", journal_id="j_mine", account_id="a_cash",
               debit_cents="500", credit_cents="0", owner_id="u1")

    _make_journal(data_dir, id="j_theirs", status="posted", owner_id="u2")
    _make_line(data_dir, id="l_2", journal_id="j_theirs", account_id="a_cash",
               debit_cents="70000", credit_cents="0", owner_id="u2")

    rows = object_finance.trial_balance(base_dir=data_dir, owner="u1")
    assert len(rows) == 1
    assert rows[0]["account_id"] == "a_cash"
    assert rows[0]["debit_total_cents"] == 500


def test_trial_balance_with_no_posted_journals_is_empty(tmp_path):
    data_dir = _install(tmp_path)
    _make_account(data_dir, id="a_cash", name="Cash", account_type="asset")
    _make_journal(data_dir, id="j_1", status="draft")
    _make_line(data_dir, id="l_1", journal_id="j_1", account_id="a_cash", debit_cents="500")

    rows = object_finance.trial_balance(base_dir=data_dir, owner="u1")
    assert rows == []


def test_trial_balance_owner_none_folds_every_owner(tmp_path):
    data_dir = _install(tmp_path)
    _make_account(data_dir, id="a_cash", name="Cash", account_type="asset")
    _make_journal(data_dir, id="j_1", status="posted", owner_id="u1")
    _make_line(data_dir, id="l_1", journal_id="j_1", account_id="a_cash", debit_cents="100", owner_id="u1")
    _make_journal(data_dir, id="j_2", status="posted", owner_id="u2")
    _make_line(data_dir, id="l_2", journal_id="j_2", account_id="a_cash", debit_cents="200", owner_id="u2")

    rows = object_finance.trial_balance(base_dir=data_dir, owner=None)
    assert len(rows) == 1
    assert rows[0]["debit_total_cents"] == 300


def test_trial_balance_rows_carry_account_display_fields(tmp_path):
    data_dir = _install(tmp_path)
    _make_account(data_dir, id="a_cash", name="Cash", code="1000", account_type="asset")
    _make_journal(data_dir, id="j_1", status="posted")
    _make_line(data_dir, id="l_1", journal_id="j_1", account_id="a_cash", debit_cents="100")

    rows = object_finance.trial_balance(base_dir=data_dir, owner="u1")
    assert rows[0]["account_name"] == "Cash"
    assert rows[0]["account_code"] == "1000"
    assert rows[0]["account_type"] == "asset"


def test_trial_balance_sorted_in_statement_order_asset_before_income(tmp_path):
    data_dir = _install(tmp_path)
    _make_account(data_dir, id="a_rev", name="Revenue", account_type="income")
    _make_account(data_dir, id="a_cash", name="Cash", account_type="asset")
    _make_journal(data_dir, id="j_1", status="posted")
    _make_line(data_dir, id="l_1", journal_id="j_1", account_id="a_rev", credit_cents="100")
    _make_line(data_dir, id="l_2", journal_id="j_1", account_id="a_cash", debit_cents="100")

    rows = object_finance.trial_balance(base_dir=data_dir, owner="u1")
    assert [row["account_id"] for row in rows] == ["a_cash", "a_rev"]


# -- Guarded draft->posted transition ---------------------------------------
#
# Exercised directly against object_records' own guard, the same pattern
# tests/test_app_invoices_totals.py uses for invoices.status. Confirms the
# transition is owner-gated ONLY -- no balance check anywhere in the guard
# (schemas/fin_journals.json's status field help documents this is
# deliberate, matching the source's own gap).

def test_owner_may_post_a_draft_journal(tmp_path):
    data_dir = _install(tmp_path)
    subject = object_permissions.PermissionSubject(user_id="u1")
    existing = {"status": "draft", "owner_id": "u1"}
    updated = {"status": "posted", "owner_id": "u1"}

    object_records._validate_field_transitions(
        "fin_journals", existing, updated, base_dir=data_dir, roots=None, subject=subject,
    )  # no exception == allowed


def test_non_owner_may_not_post_a_draft_journal(tmp_path):
    data_dir = _install(tmp_path)
    subject = object_permissions.PermissionSubject(user_id="someone_else")
    existing = {"status": "draft", "owner_id": "u1"}
    updated = {"status": "posted", "owner_id": "u1"}

    with pytest.raises(object_records.TransitionNotAllowedError):
        object_records._validate_field_transitions(
            "fin_journals", existing, updated, base_dir=data_dir, roots=None, subject=subject,
        )


def test_posted_is_terminal(tmp_path):
    data_dir = _install(tmp_path)
    subject = object_permissions.PermissionSubject(user_id="u1")
    existing = {"status": "posted", "owner_id": "u1"}
    updated = {"status": "draft", "owner_id": "u1"}

    with pytest.raises(object_records.InvalidRecordPayloadError):
        object_records._validate_field_transitions(
            "fin_journals", existing, updated, base_dir=data_dir, roots=None, subject=subject,
        )


def test_posting_a_wildly_unbalanced_journal_is_still_allowed(tmp_path):
    """The whole point of this package's parity decision: posting never
    checks total_debits_cents == total_credits_cents. A journal with
    debits wildly exceeding credits posts exactly like a balanced one.
    """
    data_dir = _install(tmp_path)
    _make_default_account(data_dir)
    _make_journal(data_dir, id="j_1", status="draft")
    _make_line(data_dir, id="l_1", journal_id="j_1", debit_cents="999999", credit_cents="1")

    subject = object_permissions.PermissionSubject(user_id="u1")
    object_records._validate_field_transitions(
        "fin_journals", {"status": "draft", "owner_id": "u1"}, {"status": "posted", "owner_id": "u1"},
        base_dir=data_dir, roots=None, subject=subject,
    )  # no exception == allowed, despite being wildly unbalanced

    totals = object_finance.journal_totals("j_1", base_dir=data_dir)
    assert totals["is_balanced"] is False
