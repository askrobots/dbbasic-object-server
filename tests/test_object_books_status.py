"""Tests for object_books_status.py -- the readiness check that surfaces the
books spine's silent-skip posture (see that module's docstring, and
packages/app-payments/objects/system/books.py, packages/app-catalog/
objects/system/stock_books.py, packages/app-banking/objects/action/
resolve_bank_line.py for what actually skips).

Fixture style borrowed from tests/test_books_spine.py (real schemas copied
from packages/*/schemas, hand-written minimal TSVs) but kept self-contained
here: this module is pure and read-only, so a test only needs the
collections + app_settings/fin_accounts rows that make a given check
"installed" or not -- no dispatcher, no object execution.
"""

import pathlib

import object_books_status

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"

CASH, AR, REV = "acct-cash", "acct-ar", "acct-rev"
INV, SHRINK = "acct-inventory", "acct-shrinkage"
FEES, INTEREST = "acct-fees", "acct-interest"


def _copy_schema(schema_dir, pkg, name):
    (schema_dir / f"{name}.json").write_text(
        (PACKAGES / pkg / "schemas" / f"{name}.json").read_text()
    )


def _write_tsv(data_dir, name, text):
    d = data_dir / "collections" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "records.tsv").write_text(text)


def setup_env(
    tmp_path,
    *,
    books=True,
    app_settings=True,
    payments=False,
    inventory=False,
    banking=False,
    settings_rows="",
    accounts_rows=(),
):
    """Build a data_dir with just enough schema + TSV to drive one check.

    books=False omits fin_journals/fin_journal_lines/fin_accounts entirely
    (the "no accounting books installed at all" case). payments/inventory/
    banking each install just the one collection whose presence gates that
    area (payments, stock_moves, bank_lines) -- no real payment/move/line
    rows are needed since this module only checks account mapping, never
    the operational data itself.

    settings_rows is raw extra app_settings TSV row text (id\\tkey\\tvalue
    \\tdescription\\n per row); accounts_rows is an iterable of
    (id, name, account_type) tuples for fin_accounts.
    """
    data_dir = tmp_path / "data"
    schema_dir = data_dir / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)

    if books:
        _copy_schema(schema_dir, "app-finance", "fin_journals")
        _copy_schema(schema_dir, "app-finance", "fin_journal_lines")
        _copy_schema(schema_dir, "app-finance", "fin_accounts")
        _write_tsv(
            data_dir, "fin_journals",
            "id\tdate\tdescription\tstatus\tkind\tgenerated_from\towner_id\n",
        )
        _write_tsv(
            data_dir, "fin_journal_lines",
            "id\tjournal_id\taccount_id\tdebit_cents\tcredit_cents\towner_id\n",
        )
        accounts_text = "id\tname\taccount_type\towner_id\n"
        for account_id, name, account_type in accounts_rows:
            accounts_text += f"{account_id}\t{name}\t{account_type}\tdan\n"
        _write_tsv(data_dir, "fin_accounts", accounts_text)

    if app_settings:
        _copy_schema(schema_dir, "app-settings", "app_settings")
        _write_tsv(
            data_dir, "app_settings",
            "id\tkey\tvalue\tdescription\n" + settings_rows,
        )

    if payments:
        _copy_schema(schema_dir, "app-payments", "payments")
        _write_tsv(data_dir, "payments", "id\n")

    if inventory:
        _copy_schema(schema_dir, "app-catalog", "stock_moves")
        _write_tsv(data_dir, "stock_moves", "id\n")

    if banking:
        _copy_schema(schema_dir, "app-banking", "bank_lines")
        _write_tsv(data_dir, "bank_lines", "id\n")

    return data_dir


def _problem_ids(status):
    return {p["id"] for p in status["problems"]}


def _check(status, check_id):
    for c in status["checks"]:
        if c["id"] == check_id:
            return c
    raise AssertionError(f"no check {check_id!r} among {[c['id'] for c in status['checks']]}")


# -- fully configured -> ready ------------------------------------------------

def test_fully_configured_cash_basis_is_ready(tmp_path):
    settings = (
        "s0\tpayments.accounting_basis\tcash\t\n"
        f"s1\tpayments.journal.cash_account\t{CASH}\t\n"
        f"s2\tpayments.journal.revenue_account\t{REV}\t\n"
    )
    data_dir = setup_env(
        tmp_path, payments=True, settings_rows=settings,
        accounts_rows=[(CASH, "Cash", "asset"), (REV, "Revenue", "income")],
    )
    status = object_books_status.books_status(base_dir=data_dir)
    assert status["ready"] is True
    assert status["problems"] == []
    assert {c["id"] for c in status["checks"]} == {
        "payments.cash_account", "payments.revenue_account",
    }
    assert all(c["status"] == "ok" for c in status["checks"])
    assert object_books_status.is_ready(base_dir=data_dir) is True
    # No inventory/banking apps installed -> nothing reported for them.
    assert not any(c["area"] in ("inventory", "banking") for c in status["checks"])


# -- missing setting -----------------------------------------------------------

def test_missing_cash_account_is_one_named_problem(tmp_path):
    settings = (
        "s0\tpayments.accounting_basis\tcash\t\n"
        f"s2\tpayments.journal.revenue_account\t{REV}\t\n"
    )
    data_dir = setup_env(
        tmp_path, payments=True, settings_rows=settings,
        accounts_rows=[(REV, "Revenue", "income")],
    )
    status = object_books_status.books_status(base_dir=data_dir)
    assert status["ready"] is False
    assert _problem_ids(status) == {"payments.cash_account"}
    problem = _check(status, "payments.cash_account")
    assert problem["status"] == "missing"
    assert problem["setting"] == "payments.journal.cash_account"
    # impact must state the CONSEQUENCE, not just repeat "missing".
    assert "never reach the ledger" in problem["impact"]
    assert object_books_status.is_ready(base_dir=data_dir) is False


# -- dangling: configured but points nowhere -----------------------------------

def test_setting_pointing_at_nonexistent_account_is_dangling(tmp_path):
    settings = (
        "s0\tpayments.accounting_basis\tcash\t\n"
        "s1\tpayments.journal.cash_account\tacct-does-not-exist\t\n"
        f"s2\tpayments.journal.revenue_account\t{REV}\t\n"
    )
    data_dir = setup_env(
        tmp_path, payments=True, settings_rows=settings,
        accounts_rows=[(REV, "Revenue", "income")],
    )
    status = object_books_status.books_status(base_dir=data_dir)
    assert status["ready"] is False
    problem = _check(status, "payments.cash_account")
    assert problem["status"] == "dangling"
    assert "acct-does-not-exist" in problem["detail"]


# -- accrual needs receivable; cash basis does not -----------------------------

def test_accrual_basis_requires_receivable_account(tmp_path):
    settings = (
        "s0\tpayments.accounting_basis\taccrual\t\n"
        f"s1\tpayments.journal.cash_account\t{CASH}\t\n"
        f"s2\tpayments.journal.revenue_account\t{REV}\t\n"
    )
    data_dir = setup_env(
        tmp_path, payments=True, settings_rows=settings,
        accounts_rows=[(CASH, "Cash", "asset"), (REV, "Revenue", "income")],
    )
    status = object_books_status.books_status(base_dir=data_dir)
    assert status["ready"] is False
    assert _problem_ids(status) == {"payments.receivable_account"}
    assert _check(status, "payments.receivable_account")["status"] == "missing"


def test_cash_basis_does_not_require_receivable_account(tmp_path):
    settings = (
        "s0\tpayments.accounting_basis\tcash\t\n"
        f"s1\tpayments.journal.cash_account\t{CASH}\t\n"
        f"s2\tpayments.journal.revenue_account\t{REV}\t\n"
    )
    data_dir = setup_env(
        tmp_path, payments=True, settings_rows=settings,
        accounts_rows=[(CASH, "Cash", "asset"), (REV, "Revenue", "income")],
    )
    status = object_books_status.books_status(base_dir=data_dir)
    assert status["ready"] is True
    assert not any(c["id"] == "payments.receivable_account" for c in status["checks"])


# -- areas not installed are not reported --------------------------------------

def test_uninstalled_areas_are_never_reported(tmp_path):
    # Only banking is installed; payments and inventory are absent. Even
    # though payments/inventory settings are entirely unconfigured, they
    # must not show up -- system_books/system_stock_books can't even run.
    settings = (
        f"s1\treconcile.journal.fees_account\t{FEES}\t\n"
        f"s2\treconcile.journal.interest_account\t{INTEREST}\t\n"
    )
    data_dir = setup_env(
        tmp_path, banking=True, settings_rows=settings,
        accounts_rows=[(FEES, "Bank Fees", "expense"), (INTEREST, "Interest Income", "income")],
    )
    status = object_books_status.books_status(base_dir=data_dir)
    assert {c["area"] for c in status["checks"]} == {"banking"}
    assert status["ready"] is True


def test_inventory_area_checked_only_when_stock_moves_installed(tmp_path):
    # inventory settings are missing, but stock_moves isn't installed ->
    # no inventory problems should appear at all.
    data_dir = setup_env(tmp_path, payments=False, inventory=False, banking=False)
    status = object_books_status.books_status(base_dir=data_dir)
    assert status["checks"] == []
    assert status["ready"] is True  # nothing installed that could skip

    # Now install stock_moves with nothing configured: both required
    # inventory accounts become named problems.
    data_dir2 = setup_env(tmp_path / "with-inventory", inventory=True)
    status2 = object_books_status.books_status(base_dir=data_dir2)
    assert status2["ready"] is False
    assert _problem_ids(status2) == {
        "inventory.inventory_account", "inventory.shrinkage_account",
    }
    assert all(c["area"] == "inventory" for c in status2["problems"])


def test_inventory_reason_override_dangling(tmp_path):
    settings = (
        f"s1\tinventory.journal.inventory_account\t{INV}\t\n"
        f"s2\tinventory.journal.shrinkage_account\t{SHRINK}\t\n"
        "s3\tinventory.journal.theft_account\tacct-nowhere\t\n"
    )
    data_dir = setup_env(
        tmp_path, inventory=True, settings_rows=settings,
        accounts_rows=[(INV, "Inventory", "asset"), (SHRINK, "Shrinkage", "expense")],
    )
    status = object_books_status.books_status(base_dir=data_dir)
    assert status["ready"] is False
    assert _problem_ids(status) == {"inventory.theft_account"}
    assert _check(status, "inventory.theft_account")["status"] == "dangling"
    # The two required accounts are fine and not reported as problems.
    assert _check(status, "inventory.inventory_account")["status"] == "ok"
    assert _check(status, "inventory.shrinkage_account")["status"] == "ok"


def test_inventory_reason_override_unset_is_not_a_problem(tmp_path):
    # No per-reason override configured at all -- falls back to
    # shrinkage_account, so no inventory.<reason>_account check exists.
    settings = (
        f"s1\tinventory.journal.inventory_account\t{INV}\t\n"
        f"s2\tinventory.journal.shrinkage_account\t{SHRINK}\t\n"
    )
    data_dir = setup_env(
        tmp_path, inventory=True, settings_rows=settings,
        accounts_rows=[(INV, "Inventory", "asset"), (SHRINK, "Shrinkage", "expense")],
    )
    status = object_books_status.books_status(base_dir=data_dir)
    assert status["ready"] is True
    assert not any(c["id"].startswith("inventory.") and c["id"] not in
                   ("inventory.inventory_account", "inventory.shrinkage_account")
                   for c in status["checks"])


def test_banking_fully_configured_is_ready(tmp_path):
    settings = (
        f"s1\treconcile.journal.fees_account\t{FEES}\t\n"
        f"s2\treconcile.journal.interest_account\t{INTEREST}\t\n"
    )
    data_dir = setup_env(
        tmp_path, banking=True, settings_rows=settings,
        accounts_rows=[(FEES, "Bank Fees", "expense"), (INTEREST, "Interest Income", "income")],
    )
    status = object_books_status.books_status(base_dir=data_dir)
    assert status["ready"] is True
    assert {c["id"] for c in status["checks"]} == {
        "banking.fees_account", "banking.interest_account",
    }


# -- no finance collections at all: clear summary, never a crash --------------

def test_no_finance_collections_at_all_is_not_ready(tmp_path):
    data_dir = setup_env(tmp_path, books=False, app_settings=True, payments=True)
    status = object_books_status.books_status(base_dir=data_dir)
    assert status["ready"] is False
    assert status["checks"] == []
    assert status["problems"] == []
    assert status["collections"]["fin_journals"] is False
    assert "No accounting books are installed" in status["summary"]
    assert object_books_status.is_ready(base_dir=data_dir) is False


def test_completely_empty_data_dir_never_raises(tmp_path):
    # No schemas directory, no collections directory at all -- the
    # bare-minimum "nothing has ever been installed here" server.
    data_dir = tmp_path / "empty-data"
    status = object_books_status.books_status(base_dir=data_dir)
    assert status["ready"] is False
    assert isinstance(status["summary"], str) and status["summary"]
    assert object_books_status.is_ready(base_dir=data_dir) is False


def test_no_apps_installed_but_books_present_is_vacuously_ready(tmp_path):
    # Books exist (fin_* collections + app_settings) but no payments/
    # inventory/banking app is installed: nothing can compose a journal,
    # so there is nothing that could silently skip.
    data_dir = setup_env(tmp_path)
    status = object_books_status.books_status(base_dir=data_dir)
    assert status["ready"] is True
    assert status["checks"] == []
    assert status["problems"] == []
