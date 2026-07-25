"""Tests for object_finance.profit_and_loss() / .balance_sheet() and the
site_statements page object (packages/app-finance/objects/site/
statements.py) -- the two financial statements plan/accounting-coverage-
and-usability.md's M1 calls the highest-value missing piece.

Fixture style follows tests/test_books_spine.py's setup_env(): a bare
data_dir built from this package's own schemas plus hand-written TSV
collection headers, with fin_accounts/fin_journals/fin_journal_lines
records created through object_records.create_collection_record() (never
hand-typed TSV rows) so every write goes through the same schema
validation and defaulting a real request would. Journals are posted the
same way tests/test_books_spine.py's make_posted_journal() does: create
draft, add lines, then update status -> posted directly -- this is
exactly the "posting is a bare status flip, balance NOT enforced"
behavior fin_journals.json's own status field documents, and it is
deliberately used here (rather than object_finance.compose_posted_journal,
which verifies balance before posting) to construct the one test case
that needs an actually-unbalanced posted journal on the books.
"""
from __future__ import annotations

import pathlib

import object_execution
import object_finance
import object_records
import python_object_runtime
from conftest import stage_collection

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
FINANCE_OBJECTS = PACKAGES / "app-finance" / "objects"

RUNTIME = python_object_runtime.PythonObjectRuntime()

# A small chart of accounts reused across tests.
CASH, AR, LOAN, EQUITY, REV, EXP = (
    "acct-cash", "acct-ar", "acct-loan", "acct-equity", "acct-rev", "acct-exp",
)


def setup_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    for name in ("fin_accounts", "fin_journals", "fin_journal_lines"):
        stage_collection(data_dir, "app-finance", name)

    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(tmp_path / "objects-unused"))
    return data_dir


def make_account(data_dir, account_id, name, account_type, owner="dan", code=""):
    object_records.create_collection_record(
        "fin_accounts",
        {"id": account_id, "name": name, "code": code, "account_type": account_type,
         "owner_id": owner},
        base_dir=data_dir,
    )


def make_default_chart(data_dir, owner="dan", prefix=""):
    # Account ids are globally unique across all owners (fin_accounts has
    # no per-owner namespacing), so a second owner's chart needs distinct
    # ids -- prefix disambiguates when a test builds more than one owner's
    # books in the same data_dir.
    make_account(data_dir, prefix + CASH, "Cash", "asset", owner=owner, code="1000")
    make_account(data_dir, prefix + AR, "Accounts Receivable", "asset", owner=owner, code="1100")
    make_account(data_dir, prefix + LOAN, "Loan Payable", "liability", owner=owner, code="2000")
    make_account(data_dir, prefix + EQUITY, "Owner Equity", "equity", owner=owner, code="3000")
    make_account(data_dir, prefix + REV, "Revenue", "income", owner=owner, code="4000")
    make_account(data_dir, prefix + EXP, "Expenses", "expense", owner=owner, code="5000")


def make_journal(data_dir, jid, *, date, lines, status="posted", owner="dan"):
    """Create a journal with the given (account_id, debit_cents, credit_cents)
    line specs and flip it to `status` directly (a bare field update, same
    as tests/test_books_spine.py's make_posted_journal) -- no balance check
    happens here, which is the point for the unbalanced-ledger test below.
    """
    object_records.create_collection_record(
        "fin_journals",
        {"id": jid, "date": date, "description": f"journal {jid}",
         "status": "draft", "kind": "standard", "owner_id": owner},
        base_dir=data_dir,
    )
    for i, (account_id, debit_cents, credit_cents) in enumerate(lines):
        object_records.create_collection_record(
            "fin_journal_lines",
            {"id": f"{jid}-l{i}", "journal_id": jid, "account_id": account_id,
             "debit_cents": str(debit_cents), "credit_cents": str(credit_cents),
             "owner_id": owner},
            base_dir=data_dir,
        )
    if status != "draft":
        object_records.update_collection_record(
            "fin_journals", jid, {"status": status}, base_dir=data_dir)


def call_statements(payload):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "site_statements", method="GET", payload=payload),
        roots=[FINANCE_OBJECTS],
    )


# ---------------------------------------------------------------------------
# profit_and_loss()
# ---------------------------------------------------------------------------

def test_pl_sign_conventions_and_net_income(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_default_chart(data_dir)
    # A sale: debit cash, credit revenue -- revenue is credit-normal, so
    # its P&L amount should be positive (credit - debit = 1000 - 0).
    # An expense paid in cash: debit expense, credit cash -- expense is
    # debit-normal, so its amount should also be positive (1000 - 0 = 400... )
    make_journal(data_dir, "j1", date="2026-07-10",
                 lines=[(CASH, 1000, 0), (REV, 0, 1000)])
    make_journal(data_dir, "j2", date="2026-07-11",
                 lines=[(EXP, 400, 0), (CASH, 0, 400)])

    pl = object_finance.profit_and_loss(base_dir=data_dir, owner="dan")
    income_by_id = {r["account_id"]: r["amount_cents"] for r in pl["income"]}
    expense_by_id = {r["account_id"]: r["amount_cents"] for r in pl["expenses"]}

    assert income_by_id[REV] == 1000  # credit-normal: reported positive
    assert expense_by_id[EXP] == 400  # debit-normal: reported positive
    assert pl["total_income_cents"] == 1000
    assert pl["total_expenses_cents"] == 400
    assert pl["net_income_cents"] == 600  # income - expenses
    # asset/liability/equity accounts never appear on a P&L
    assert all(r["account_id"] != CASH for r in pl["income"] + pl["expenses"])


def test_pl_date_filtering_excludes_out_of_period(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_default_chart(data_dir)
    make_journal(data_dir, "jan", date="2026-01-15",
                 lines=[(CASH, 100, 0), (REV, 0, 100)])
    make_journal(data_dir, "jul", date="2026-07-15",
                 lines=[(CASH, 200, 0), (REV, 0, 200)])

    pl = object_finance.profit_and_loss(
        base_dir=data_dir, owner="dan", start="2026-07-01", end="2026-07-31")
    assert pl["total_income_cents"] == 200  # only the July journal counts

    pl_all = object_finance.profit_and_loss(base_dir=data_dir, owner="dan")
    assert pl_all["total_income_cents"] == 300  # blank bounds = unbounded


def test_pl_excludes_draft_journals(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_default_chart(data_dir)
    make_journal(data_dir, "draft1", date="2026-07-10",
                 lines=[(CASH, 500, 0), (REV, 0, 500)], status="draft")
    pl = object_finance.profit_and_loss(base_dir=data_dir, owner="dan")
    assert pl["income"] == []
    assert pl["total_income_cents"] == 0
    assert pl["net_income_cents"] == 0


def test_pl_owner_scoping(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_default_chart(data_dir, owner="dan")
    make_default_chart(data_dir, owner="mallory", prefix="m-")
    make_journal(data_dir, "j-dan", date="2026-07-10",
                 lines=[(CASH, 1000, 0), (REV, 0, 1000)], owner="dan")
    make_journal(data_dir, "j-mallory", date="2026-07-10",
                 lines=[("m-" + CASH, 9999, 0), ("m-" + REV, 0, 9999)], owner="mallory")

    pl = object_finance.profit_and_loss(base_dir=data_dir, owner="dan")
    assert pl["total_income_cents"] == 1000  # mallory's journal never appears


def test_pl_empty_ledger_returns_zeros(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    pl = object_finance.profit_and_loss(base_dir=data_dir, owner="dan")
    assert pl == {
        "period": {"start": "", "end": ""},
        "income": [],
        "expenses": [],
        "total_income_cents": 0,
        "total_expenses_cents": 0,
        "net_income_cents": 0,
    }


# ---------------------------------------------------------------------------
# balance_sheet()
# ---------------------------------------------------------------------------

def test_balance_sheet_balances_when_net_income_included(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_default_chart(data_dir)
    # Owner contributes cash (equity), then a cash sale (asset up, income
    # up). Assets: cash 1500. Liabilities: 0. Equity: owner equity 500 +
    # current period earnings (net income) 1000 = 1500. Balances.
    make_journal(data_dir, "j1", date="2026-07-01",
                 lines=[(CASH, 500, 0), (EQUITY, 0, 500)])
    make_journal(data_dir, "j2", date="2026-07-10",
                 lines=[(CASH, 1000, 0), (REV, 0, 1000)])

    bs = object_finance.balance_sheet(base_dir=data_dir, owner="dan")
    assert bs["total_assets_cents"] == 1500
    assert bs["total_liabilities_cents"] == 0
    assert bs["total_equity_cents"] == 1500  # 500 stored + 1000 derived earnings
    assert bs["balances"] is True
    assert bs["difference_cents"] == 0

    earnings_rows = [r for r in bs["equity"] if r["account_name"] == "Current period earnings"]
    assert len(earnings_rows) == 1
    assert earnings_rows[0]["account_id"] == ""
    assert earnings_rows[0]["amount_cents"] == 1000


def test_balance_sheet_reports_unbalanced_ledger_loudly(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_default_chart(data_dir)
    # A lopsided posted journal: debit cash 700, credit nothing. This can
    # only happen because posting is a bare status flip that never checks
    # is_balanced (fin_journals.json's own documented gap) -- exactly the
    # scenario balance_sheet()'s difference_cents exists to surface.
    make_journal(data_dir, "bad", date="2026-07-05", lines=[(CASH, 700, 0)])

    bs = object_finance.balance_sheet(base_dir=data_dir, owner="dan")
    assert bs["balances"] is False
    assert bs["difference_cents"] == 700  # assets 700, liabilities+equity 0
    assert bs["total_assets_cents"] == 700


def test_balance_sheet_excludes_draft_journals(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_default_chart(data_dir)
    make_journal(data_dir, "draft1", date="2026-07-05",
                 lines=[(CASH, 700, 0), (EQUITY, 0, 700)], status="draft")
    bs = object_finance.balance_sheet(base_dir=data_dir, owner="dan")
    assert bs["total_assets_cents"] == 0
    assert bs["balances"] is True
    assert bs["difference_cents"] == 0


def test_balance_sheet_owner_scoping(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_default_chart(data_dir, owner="dan")
    make_default_chart(data_dir, owner="mallory", prefix="m-")
    make_journal(data_dir, "j-dan", date="2026-07-01",
                 lines=[(CASH, 100, 0), (EQUITY, 0, 100)], owner="dan")
    make_journal(data_dir, "j-mallory", date="2026-07-01",
                 lines=[("m-" + CASH, 5000, 0), ("m-" + EQUITY, 0, 5000)], owner="mallory")

    bs = object_finance.balance_sheet(base_dir=data_dir, owner="dan")
    assert bs["total_assets_cents"] == 100  # mallory's books never appear
    assert bs["balances"] is True


def test_balance_sheet_empty_ledger_returns_zeros(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    bs = object_finance.balance_sheet(base_dir=data_dir, owner="dan")
    assert bs["total_assets_cents"] == 0
    assert bs["total_liabilities_cents"] == 0
    assert bs["total_equity_cents"] == 0
    assert bs["balances"] is True
    assert bs["difference_cents"] == 0
    # The synthetic earnings line is still present, just zero.
    assert bs["equity"][-1]["account_name"] == "Current period earnings"
    assert bs["equity"][-1]["amount_cents"] == 0


def test_balance_sheet_as_of_filters_later_activity(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_default_chart(data_dir)
    make_journal(data_dir, "early", date="2026-01-01",
                 lines=[(CASH, 100, 0), (EQUITY, 0, 100)])
    make_journal(data_dir, "later", date="2026-12-01",
                 lines=[(CASH, 200, 0), (EQUITY, 0, 200)])

    bs = object_finance.balance_sheet(base_dir=data_dir, owner="dan", as_of="2026-06-30")
    assert bs["total_assets_cents"] == 100
    assert bs["balances"] is True


# ---------------------------------------------------------------------------
# site_statements page
# ---------------------------------------------------------------------------

def test_page_renders_statements_for_signed_in_user(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_default_chart(data_dir)
    make_journal(data_dir, "j1", date="2026-07-10",
                 lines=[(CASH, 1000, 0), (REV, 0, 1000)])

    result = call_statements({"_identity": {"user_id": "dan"}})
    assert result.ok, result.error
    body = result.result["body"]
    assert "Profit &amp; Loss" in body
    assert "Balance Sheet" in body
    assert "Revenue" in body
    assert "USD 10.00" in body  # 1000 cents formatted


def test_page_shows_signin_prompt_for_anonymous(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    result = call_statements({})
    assert result.ok, result.error
    body = result.result["body"]
    assert "Sign in" in body
    assert "Profit" not in body


def test_page_shows_unbalanced_warning(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_default_chart(data_dir)
    make_journal(data_dir, "bad", date="2026-07-05", lines=[(CASH, 700, 0)])

    result = call_statements({"_identity": {"user_id": "dan"}})
    assert result.ok, result.error
    body = result.result["body"]
    assert "does NOT balance" in body or "does not balance" in body.lower()
