"""Expenses: the materials half of time and materials.

An expense is an accounting fact first and a billable item second, and
almost every property here follows from that ordering. Unbillable
spending still posts. Who paid decides which account is credited, not a
note on a report. And approval -- by somebody other than the spender --
is the single event that both composes the journal and fixes the price
the client will see.
"""

import pathlib

from conftest import stage_collection

import object_execution
import object_rates
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
FINANCE_OBJECTS = PACKAGES / "app-finance" / "objects"
INVOICE_OBJECTS = PACKAGES / "app-invoices" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

ACCOUNTS = (
    ("acct-travel", "Travel", "expense"),
    ("acct-card", "Company Card Clearing", "liability"),
    ("acct-owed", "Owed To Staff", "liability"),
)

SETTINGS = (
    ("expenses.journal.expense_account", "acct-travel"),
    ("expenses.journal.paid_account", "acct-card"),
    ("expenses.journal.reimbursable_account", "acct-owed"),
)


def setup_env(tmp_path, monkeypatch, *, settings=SETTINGS):
    data_dir = tmp_path / "data"
    for pkg, name in (("app-finance", "expenses"), ("app-finance", "fin_accounts"),
                      ("app-finance", "fin_journals"), ("app-finance", "fin_journal_lines"),
                      ("app-projects", "projects"),
                      ("app-invoices", "invoices"), ("app-invoices", "invoice_lines"),
                      ("app-timers", "time_logs"), ("app-timers", "rate_cards")):
        stage_collection(data_dir, pkg, name)
    rows = "".join(f"s{i}\t{k}\t{v}\t\n" for i, (k, v) in enumerate(settings))
    stage_collection(data_dir, "app-settings", "app_settings", rows=rows)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))

    object_records.create_collection_record(
        "projects", {"id": "proj-acme", "name": "Acme Rebuild", "owner_id": "boss"},
        base_dir=data_dir)
    for account_id, name, kind in ACCOUNTS:
        object_records.create_collection_record(
            "fin_accounts",
            {"id": account_id, "name": name, "account_type": kind,
             "code": account_id[-4:], "is_active": "true", "owner_id": "boss"},
            base_dir=data_dir)
    return data_dir


def spend(data_dir, expense_id, cents=25000, **fields):
    record = {"id": expense_id, "description": "Flight to Chicago",
              "incurred_on": "2026-06-15", "amount_cents": str(cents),
              "currency": "USD", "project_id": "proj-acme", "paid_by": "company",
              "billable": "true", "markup_bps": "0", "status": "draft",
              "owner_id": "dana"}
    record.update({k: str(v) for k, v in fields.items()})
    return object_records.create_collection_record(
        "expenses", record, base_dir=data_dir)


def hook(record, *, existing=None, changes=None, actor="boss", action="update"):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "hook_expenses", method="BEFORE_WRITE",
            payload={"action": action, "collection": "expenses",
                     "record": record, "existing": existing,
                     "changes": changes if changes is not None else dict(record),
                     "subject": {"user_id": actor, "roles": ["manager"]}}),
        roots=[FINANCE_OBJECTS]).result


def approve(data_dir, expense_id, *, actor="boss"):
    existing = object_records.get_collection_record("expenses", expense_id,
                                                    base_dir=data_dir)
    outcome = hook({"status": "approved"}, existing=existing,
                   changes={"status": "approved"}, actor=actor)
    if outcome and outcome.get("error"):
        return outcome
    patch = (outcome or {}).get("record") or {"status": "approved"}
    object_records.update_collection_record("expenses", expense_id, patch,
                                            base_dir=data_dir, actor=actor)
    return {"ok": True}


def post_books(data_dir, expense_id):
    expense = object_records.get_collection_record("expenses", expense_id,
                                                   base_dir=data_dir)
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "system_expense_books", method="POST",
            payload={"collection": "expenses", "record": expense}),
        roots=[FINANCE_OBJECTS]).result


def generate(payload=None):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "action_generate_tm_invoice", method="POST", payload=payload or {}),
        roots=[INVOICE_OBJECTS]).result


def expense(data_dir, expense_id):
    return object_records.get_collection_record("expenses", expense_id,
                                                base_dir=data_dir)


def journal_lines(data_dir):
    return object_records.read_collection_records("fin_journal_lines",
                                                  base_dir=data_dir)


def lines(data_dir):
    return object_records.read_collection_records("invoice_lines", base_dir=data_dir)


# --- markup ------------------------------------------------------------------

def test_markup_is_basis_points_rounded_once():
    assert object_rates.with_markup(10000, 0) == 10000        # at cost
    assert object_rates.with_markup(10000, 1500) == 11500     # 15%
    # 3333 at 15% is 3832.95 -> 3833, rounded once on the whole amount.
    assert object_rates.with_markup(3333, 1500) == 3833


# --- the gate ----------------------------------------------------------------

def test_nobody_approves_their_own_spending(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    spend(data_dir, "e1", status="submitted")
    outcome = approve(data_dir, "e1", actor="dana")
    assert outcome["status"] == 403 and "not an approval" in outcome["error"]


def test_unbillable_spending_is_still_approved(tmp_path, monkeypatch):
    """It still left the bank account. Refusing to approve it would leave
    real money unrecorded -- a worse failure than an unbilled hour."""
    data_dir = setup_env(tmp_path, monkeypatch)
    spend(data_dir, "e1", status="submitted", billable="false")
    assert approve(data_dir, "e1")["ok"]

    row = expense(data_dir, "e1")
    assert row["status"] == "approved"
    assert row["billable_amount_cents"] == "0"     # approved, never billable


def test_approval_stamps_the_markup_that_applied_then(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    spend(data_dir, "e1", cents=10000, markup_bps=1500, status="submitted")
    approve(data_dir, "e1")

    row = expense(data_dir, "e1")
    assert row["markup_bps"] == "1500"
    assert row["billable_amount_cents"] == "11500"
    assert row["approved_by"] == "boss" and row["approved_at"]


def test_the_default_markup_comes_from_settings(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=SETTINGS + (("billing.expense_markup_bps", "1000"),))
    spend(data_dir, "e1", cents=10000, markup_bps="", status="submitted")
    approve(data_dir, "e1")
    assert expense(data_dir, "e1")["billable_amount_cents"] == "11000"


def test_an_expense_needs_an_amount(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    outcome = hook({"description": "Nothing", "amount_cents": "0",
                    "owner_id": "dana"}, action="create")
    assert outcome["status"] == 400 and "negative spend" in outcome["error"]


def test_submitting_needs_the_date_the_money_was_spent(tmp_path, monkeypatch):
    """Schema `required` already blocks this on the public write path;
    the gate matters for rows a trusted server-side writer put there,
    which skip validation by design."""
    setup_env(tmp_path, monkeypatch)
    outcome = hook({"status": "submitted"},
                   existing={"id": "e1", "amount_cents": "25000",
                             "incurred_on": "", "owner_id": "dana",
                             "status": "draft"},
                   changes={"status": "submitted"}, actor="dana")
    assert outcome["status"] == 400
    assert "period its journal belongs to" in outcome["error"]


def test_an_explicit_zero_markup_beats_the_house_policy(tmp_path, monkeypatch):
    """Blank means 'whatever the house does'; 0 means 'this one at cost'.
    A schema default would have collapsed the two."""
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=SETTINGS + (("billing.expense_markup_bps", "1000"),))
    spend(data_dir, "e1", cents=10000, markup_bps=0, status="submitted")
    approve(data_dir, "e1")
    assert expense(data_dir, "e1")["billable_amount_cents"] == "10000"


def test_approved_spending_is_frozen(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    spend(data_dir, "e1", status="submitted")
    approve(data_dir, "e1")

    existing = expense(data_dir, "e1")
    outcome = hook({"amount_cents": "99999"}, existing=existing,
                   changes={"amount_cents": "99999"})
    assert outcome["status"] == 409 and "settled" in outcome["error"]


# --- the books ----------------------------------------------------------------

def test_a_company_paid_expense_credits_the_card(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    spend(data_dir, "e1", status="submitted")
    approve(data_dir, "e1")

    result = post_books(data_dir, "e1")
    assert result.get("journal_id") and result.get("posted")

    rows = {r["account_id"]: r for r in journal_lines(data_dir)}
    assert rows["acct-travel"]["debit_cents"] == "25000"
    assert rows["acct-card"]["credit_cents"] == "25000"


def test_a_personally_paid_expense_credits_what_the_business_owes(
        tmp_path, monkeypatch):
    """Booking somebody's own card against company cash claims money left
    an account it never left, and leaves them chasing a reimbursement the
    ledger has no record of."""
    data_dir = setup_env(tmp_path, monkeypatch)
    spend(data_dir, "e1", status="submitted", paid_by="personal")
    approve(data_dir, "e1")
    post_books(data_dir, "e1")

    rows = {r["account_id"]: r for r in journal_lines(data_dir)}
    assert rows["acct-travel"]["debit_cents"] == "25000"
    assert rows["acct-owed"]["credit_cents"] == "25000"
    assert "acct-card" not in rows
    assert "reimbursable to dana" in rows["acct-owed"]["memo"]


def test_an_unapproved_expense_posts_nothing(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    spend(data_dir, "e1", status="submitted")
    assert post_books(data_dir, "e1")["skipped"] == "not approved"
    assert journal_lines(data_dir) == []


def test_posting_twice_composes_one_journal(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    spend(data_dir, "e1", status="submitted")
    approve(data_dir, "e1")
    post_books(data_dir, "e1")
    again = post_books(data_dir, "e1")
    assert "already composed" in again["skipped"]
    assert len(journal_lines(data_dir)) == 2      # the one balanced pair


def test_unconfigured_books_skip_rather_than_block_the_expense(
        tmp_path, monkeypatch):
    """Recording that you bought a plane ticket must not require somebody
    to have built a chart of accounts first."""
    data_dir = setup_env(tmp_path, monkeypatch, settings=())
    spend(data_dir, "e1", status="submitted")
    assert approve(data_dir, "e1")["ok"]          # the expense stands
    result = post_books(data_dir, "e1")
    assert result["skipped"] == "accounts unconfigured"
    assert result["needs"]


def test_an_expense_naming_its_own_account_overrides_the_default(
        tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    object_records.create_collection_record(
        "fin_accounts", {"id": "acct-meals", "name": "Meals",
                         "account_type": "expense", "code": "6100",
                         "is_active": "true", "owner_id": "boss"},
        base_dir=data_dir)
    spend(data_dir, "e1", status="submitted", account_id="acct-meals")
    approve(data_dir, "e1")
    post_books(data_dir, "e1")

    rows = {r["account_id"]: r for r in journal_lines(data_dir)}
    assert rows["acct-meals"]["debit_cents"] == "25000"


# --- reaching the invoice -------------------------------------------------------

def test_costs_and_hours_land_on_one_invoice(tmp_path, monkeypatch):
    """A client engaged one firm for one project and gets one bill."""
    data_dir = setup_env(tmp_path, monkeypatch)
    object_records.create_collection_record(
        "rate_cards", {"id": "house", "label": "House", "hourly_rate_cents": "15000",
                       "valid_from": "2026-01-01", "is_active": "true",
                       "owner_id": "boss"}, base_dir=data_dir)
    object_records.create_collection_record(
        "time_logs", {"id": "t1", "project_id": "proj-acme",
                      "started_at": "2026-06-15T09:00:00Z",
                      "ended_at": "2026-06-15T10:00:00Z", "is_running": "false",
                      "billable": "true", "status": "approved",
                      "duration_seconds": "3600", "hourly_rate_cents": "15000",
                      "amount_cents": "15000", "owner_id": "dana"},
        base_dir=data_dir)
    spend(data_dir, "e1", cents=10000, markup_bps=1500, status="submitted")
    approve(data_dir, "e1")

    result = generate({"project_id": "proj-acme"})
    assert result["entries_billed"] == 1 and result["expenses_billed"] == 1
    assert result["time_cents"] == 15000 and result["expense_cents"] == 11500
    assert result["total_cents"] == 26500

    invoice = object_records.read_collection_records("invoices", base_dir=data_dir)[0]
    assert invoice["total_cents"] == "26500"
    assert expense(data_dir, "e1")["status"] == "billed"


def test_the_line_says_when_a_cost_was_marked_up(tmp_path, monkeypatch):
    """A pass-through cost quietly grossed up is what clients discover
    later and remember."""
    data_dir = setup_env(tmp_path, monkeypatch)
    spend(data_dir, "e1", cents=10000, markup_bps=1500, status="submitted")
    spend(data_dir, "e2", cents=5000, markup_bps=0, status="submitted",
          description="Taxi", incurred_on="2026-06-16")
    approve(data_dir, "e1")
    approve(data_dir, "e2")
    generate({"project_id": "proj-acme"})

    rows = sorted(lines(data_dir), key=lambda r: r["description"])
    assert "cost plus 15%" in rows[0]["description"]
    assert "(at cost)" in rows[1]["description"]


def test_unbillable_costs_never_reach_the_invoice(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    spend(data_dir, "e1", status="submitted", billable="false")
    approve(data_dir, "e1")
    assert generate({"project_id": "proj-acme"})["invoiced"] == 0


def test_billing_twice_bills_no_cost_twice(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    spend(data_dir, "e1", status="submitted")
    approve(data_dir, "e1")
    generate({"project_id": "proj-acme"})
    assert generate({"project_id": "proj-acme"})["invoiced"] == 0
    assert len(lines(data_dir)) == 1


def test_expenses_are_never_collapsed_into_one_line(tmp_path, monkeypatch):
    """One receipt, one line: grouping would hide exactly what a client
    wants itemised."""
    data_dir = setup_env(tmp_path, monkeypatch)
    spend(data_dir, "e1", status="submitted")
    spend(data_dir, "e2", status="submitted", description="Hotel",
          incurred_on="2026-06-16")
    approve(data_dir, "e1")
    approve(data_dir, "e2")
    generate({"project_id": "proj-acme", "grouping": "by_person"})
    assert len(lines(data_dir)) == 2


def test_expenses_absent_leaves_time_billing_working(tmp_path, monkeypatch):
    """app-finance is a soft dependency: time still bills without it."""
    data_dir = tmp_path / "data"
    for pkg, name in (("app-timers", "time_logs"), ("app-timers", "rate_cards"),
                      ("app-projects", "projects"),
                      ("app-invoices", "invoices"), ("app-invoices", "invoice_lines"),
                      ("app-settings", "app_settings")):
        stage_collection(data_dir, pkg, name)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    object_records.create_collection_record(
        "projects", {"id": "proj-acme", "name": "Acme", "owner_id": "boss"},
        base_dir=data_dir)
    object_records.create_collection_record(
        "time_logs", {"id": "t1", "project_id": "proj-acme",
                      "started_at": "2026-06-15T09:00:00Z",
                      "ended_at": "2026-06-15T10:00:00Z", "is_running": "false",
                      "billable": "true", "status": "approved",
                      "duration_seconds": "3600", "hourly_rate_cents": "15000",
                      "amount_cents": "15000", "owner_id": "dana"},
        base_dir=data_dir)
    result = generate({"project_id": "proj-acme"})
    assert result["invoiced"] == 1 and result["expenses_billed"] == 0
