"""Wallet money reaching the books, and proving it still adds up.

The gap: wallet_entries recorded every movement of customer money and
none of it reached fin_journals. app-payments' system_books composed
journals for payments/refunds/invoices; nothing did the same for the
prepaid wallet, so a server charging real money for template runs showed
zero revenue on the P&L and neither the cash nor the customer liability
on the balance sheet.

The assertion that carries the most weight in this file is that **a
top-up is not revenue**. It is the flattering mistake -- book top-ups as
income and every unspent dollar in a customer wallet inflates profit
while the debt owed for it goes unrecorded. The policy tests below pin
the deferral explicitly, because a future refactor that "simplifies"
top-up straight to revenue would pass every balance check and every
idempotency test while making the books lie.
"""

import pathlib

import pytest
from conftest import schema_header, stage_collection

import object_billing
import object_execution
import object_finance
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BILLING_OBJECTS = REPO_ROOT / "packages" / "app-billing" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

CASH, FUNDS, REVENUE, PROMO, ADJUST = (
    "acct-cash", "acct-customer-funds", "acct-revenue",
    "acct-promo", "acct-adjust",
)
ALL_ACCOUNTS = {
    "cash": CASH, "customer_funds": FUNDS, "revenue": REVENUE,
    "promo_expense": PROMO, "adjustment": ADJUST,
}


# --- the accounting policy (pure) -----------------------------------------------

def test_a_topup_is_a_liability_not_revenue():
    """THE assertion. Cash arrives and service is owed; nothing is earned
    until the work is done. Booking this to revenue overstates income by
    every unspent cent and understates what is owed by the same amount."""
    posting = object_billing.wallet_posting("topup", 5000, ALL_ACCOUNTS)
    assert posting == {"debit": CASH, "credit": FUNDS, "amount_minor": 5000}
    assert REVENUE not in posting.values()


def test_a_debit_is_where_revenue_is_finally_earned():
    posting = object_billing.wallet_posting("debit", -25, ALL_ACCOUNTS)
    assert posting == {"debit": FUNDS, "credit": REVENUE, "amount_minor": 25}


def test_a_refund_un_earns_revenue_without_moving_cash():
    """Credit returned to a wallet reverses the earning, but the money
    never left the wallet, so cash must not appear."""
    posting = object_billing.wallet_posting("refund", 25, ALL_ACCOUNTS)
    assert posting == {"debit": REVENUE, "credit": FUNDS, "amount_minor": 25}
    assert CASH not in posting.values()


def test_promo_credit_costs_us_something():
    posting = object_billing.wallet_posting("promo", 500, ALL_ACCOUNTS)
    assert posting == {"debit": PROMO, "credit": FUNDS, "amount_minor": 500}


def test_an_adjustment_gets_its_own_account_not_revenue():
    """Burying corrections in revenue is how a P&L stops being auditable."""
    posting = object_billing.wallet_posting("adjustment", -100, ALL_ACCOUNTS)
    assert posting == {"debit": FUNDS, "credit": ADJUST, "amount_minor": 100}


@pytest.mark.parametrize("kind", ["hold", "release"])
def test_holds_compose_nothing_and_say_why(kind):
    """Doctrine, not an omission: ring-fencing funds inside the same
    liability is not an economic event."""
    posting = object_billing.wallet_posting(kind, -25, ALL_ACCOUNTS)
    assert "skip" in posting
    assert "never spent" in posting["skip"]


def test_a_negative_amount_never_reaches_a_journal_line():
    """Direction is carried by WHICH account, never by a sign -- a
    negative debit_cents is rejected by the composer, so a policy that
    leaked one would fail at post time instead of here."""
    for kind, amount in [("topup", 5000), ("debit", -25), ("refund", 25),
                         ("promo", 500), ("adjustment", -100), ("topup", -300)]:
        posting = object_billing.wallet_posting(kind, amount, ALL_ACCOUNTS)
        assert posting["amount_minor"] > 0, (kind, amount)


def test_an_unconfigured_account_skips_and_names_the_setting():
    """Never guess an account. Posting real money somewhere plausible is
    worse than not posting, and the operator needs the key to fix."""
    posting = object_billing.wallet_posting("topup", 5000, {"cash": CASH})
    assert "skip" in posting
    assert "billing.journal.customer_funds_account" in posting["skip"]


def test_an_unknown_kind_refuses_rather_than_inventing_a_policy():
    posting = object_billing.wallet_posting("mystery", 100, ALL_ACCOUNTS)
    assert "no accounting policy" in posting["skip"]


def test_a_wrong_signed_movement_is_refused():
    assert "skip" in object_billing.wallet_posting("debit", 25, ALL_ACCOUNTS)
    assert "skip" in object_billing.wallet_posting("refund", -25, ALL_ACCOUNTS)


# --- the reconciliation invariant -----------------------------------------------

def test_reconciliation_balances_when_the_books_match():
    result = object_billing.customer_funds_reconciliation([3000, 1750], 250, 5000)
    assert result["owed_to_customers_minor"] == 4750
    assert result["booked_liability_minor"] == 4750
    assert result["balanced"] is True


def test_an_in_flight_hold_does_not_look_like_a_discrepancy():
    """THE false-alarm test. A wallet with an open hold reports a balance
    below what its owner is owed -- the money is still theirs -- while
    the books correctly composed nothing for the hold. A reconciliation
    that ignores holds reports a difference every time a template run is
    in progress, and a report that cries wolf is one nobody reads."""
    entries = [
        {"kind": "topup", "amount_minor": "5000"},
        {"kind": "hold", "amount_minor": "-25"},     # in flight, unreleased
    ]
    holds = object_billing.outstanding_holds_minor(entries)
    assert holds == 25

    balanced = object_billing.customer_funds_reconciliation(
        [4975], 0, 5000, outstanding_holds=holds)
    assert balanced["balanced"] is True, balanced

    naive = object_billing.customer_funds_reconciliation([4975], 0, 5000)
    assert naive["balanced"] is False   # what the wrong version would report


def test_a_settled_hold_cancels_itself():
    entries = [
        {"kind": "hold", "amount_minor": "-25"},
        {"kind": "release", "amount_minor": "25"},
    ]
    assert object_billing.outstanding_holds_minor(entries) == 0


def test_a_real_shortfall_is_still_caught():
    """The report must not be so forgiving it can never fail."""
    result = object_billing.customer_funds_reconciliation([5000], 0, 4000)
    assert result["balanced"] is False
    assert result["difference_minor"] == 1000


# --- the composer, end to end ---------------------------------------------------

def setup_books(tmp_path, monkeypatch, *, books=True, configured=True):
    data_dir = tmp_path / "data"
    for pkg, name in (("app-billing", "wallets"), ("app-billing", "wallet_entries")):
        stage_collection(data_dir, pkg, name)

    settings = ""
    if configured:
        settings = (f"s1\tbilling.journal.cash_account\t{CASH}\t\n"
                    f"s2\tbilling.journal.customer_funds_account\t{FUNDS}\t\n"
                    f"s3\tbilling.journal.revenue_account\t{REVENUE}\t\n")
    stage_collection(data_dir, "app-settings", "app_settings", rows=settings)

    if books:
        # Field-order pinned against the real schema, like test_books_spine:
        # a later schema edit must not silently shift values into the wrong
        # columns.
        fields = schema_header("app-finance", "fin_accounts").strip("\n").split("\t")

        def account_row(account_id, name, account_type):
            values = {"id": account_id, "name": name,
                      "account_type": account_type, "owner_id": "dan"}
            return "\t".join(values.get(f, "") for f in fields) + "\n"

        stage_collection(data_dir, "app-finance", "fin_accounts",
                         rows=(account_row(CASH, "Cash", "asset")
                               + account_row(FUNDS, "Customer Funds", "liability")
                               + account_row(REVENUE, "Revenue", "income")))
        stage_collection(data_dir, "app-finance", "fin_journals")
        stage_collection(data_dir, "app-finance", "fin_journal_lines")

    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(tmp_path / "objects-unused"))
    object_records.create_collection_record(
        "wallets", {"id": "w1", "owner_id": "dan", "is_active": "true"},
        base_dir=data_dir)
    return data_dir


def stage_entry(data_dir, entry_id, amount, kind):
    return object_records.create_collection_record(
        "wallet_entries",
        {"id": entry_id, "wallet_id": "w1", "amount_minor": str(amount),
         "kind": kind, "description": f"{kind} {entry_id}", "owner_id": "dan"},
        base_dir=data_dir)


def fire(entry_id, action="created"):
    # Mirror the REAL dispatcher payload: the event name uses the
    # participle, the action field carries the raw verb.
    raw = {"created": "create", "updated": "update", "deleted": "delete"}[action]
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "system_wallet_books", method="EVENT",
            payload={"event": f"wallet_entries.record.{action}",
                     "collection": "wallet_entries", "record_id": entry_id,
                     "action": raw},
        ),
        roots=[BILLING_OBJECTS],
    ).result


def posted_lines(data_dir):
    return {row["account_id"]: (int(row["debit_cents"]), int(row["credit_cents"]))
            for row in object_records.read_collection_records(
                "fin_journal_lines", base_dir=data_dir)}


def test_a_topup_lands_in_the_books_as_cash_and_a_liability(tmp_path, monkeypatch):
    data_dir = setup_books(tmp_path, monkeypatch)
    stage_entry(data_dir, "e1", 5000, "topup")
    result = fire("e1")
    assert result.get("ok") and result.get("journal_id"), result

    lines = posted_lines(data_dir)
    assert lines[CASH] == (5000, 0)
    assert lines[FUNDS] == (0, 5000)
    assert REVENUE not in lines          # nothing earned yet


def test_a_run_charge_moves_the_liability_into_revenue(tmp_path, monkeypatch):
    data_dir = setup_books(tmp_path, monkeypatch)
    stage_entry(data_dir, "e2", -25, "debit")
    assert fire("e2").get("journal_id")

    lines = posted_lines(data_dir)
    assert lines[FUNDS] == (25, 0)
    assert lines[REVENUE] == (0, 25)


def test_a_replayed_event_composes_nothing(tmp_path, monkeypatch):
    """Provenance idempotency, the house rule -- events get redelivered."""
    data_dir = setup_books(tmp_path, monkeypatch)
    stage_entry(data_dir, "e3", 5000, "topup")
    first = fire("e3")
    second = fire("e3")
    assert second.get("skipped")
    assert second.get("journal_id") == first.get("journal_id")
    assert len(object_records.read_collection_records(
        "fin_journals", base_dir=data_dir)) == 1


def test_a_hold_leaves_the_books_untouched(tmp_path, monkeypatch):
    data_dir = setup_books(tmp_path, monkeypatch)
    stage_entry(data_dir, "e4", -25, "hold")
    result = fire("e4")
    assert "never spent" in result.get("skipped", "")
    assert object_records.read_collection_records(
        "fin_journals", base_dir=data_dir) == []


def test_billing_works_with_no_books_installed(tmp_path, monkeypatch):
    """Soft dependency: the books simply learn nothing."""
    data_dir = setup_books(tmp_path, monkeypatch, books=False)
    stage_entry(data_dir, "e5", 5000, "topup")
    result = fire("e5")
    assert result.get("ok") is True
    assert "books not installed" in result.get("skipped", "")


def test_unconfigured_accounts_skip_and_name_the_setting(tmp_path, monkeypatch):
    data_dir = setup_books(tmp_path, monkeypatch, configured=False)
    stage_entry(data_dir, "e7", 5000, "topup")
    result = fire("e7")
    assert "billing.journal.cash_account" in result.get("skipped", "")
    assert object_records.read_collection_records(
        "fin_journals", base_dir=data_dir) == []


def test_an_edit_to_an_entry_never_double_books(tmp_path, monkeypatch):
    data_dir = setup_books(tmp_path, monkeypatch)
    stage_entry(data_dir, "e6", 5000, "topup")
    fire("e6")
    updated = fire("e6", action="updated")
    assert updated.get("skipped") == "only new entries compose"
    assert len(object_records.read_collection_records(
        "fin_journals", base_dir=data_dir)) == 1


def test_the_full_cycle_reconciles(tmp_path, monkeypatch):
    """Top up, hold, release, charge -- then the books and the wallet must
    agree about what is still owed, WITH a hold left in flight so the
    holds term is exercised end to end rather than only in the unit."""
    data_dir = setup_books(tmp_path, monkeypatch)
    for entry_id, amount, kind in [
        ("c1", 5000, "topup"), ("c2", -25, "hold"), ("c3", 25, "release"),
        ("c4", -25, "debit"), ("c5", -25, "hold"),      # c5 still in flight
    ]:
        stage_entry(data_dir, entry_id, amount, kind)
        fire(entry_id)

    wallet = object_records.get_collection_record("wallets", "w1", base_dir=data_dir)
    balance = int(wallet["balance_minor"])
    assert balance == 4950          # 5000 - 25 charged - 25 held

    entries = object_records.read_collection_records("wallet_entries", base_dir=data_dir)
    totals = {row["account_id"]: row for row in
              object_finance.trial_balance(base_dir=data_dir)}
    funds = totals[FUNDS]
    result = object_billing.customer_funds_reconciliation(
        [balance],
        int(funds["debit_total_cents"]), int(funds["credit_total_cents"]),
        outstanding_holds=object_billing.outstanding_holds_minor(entries))

    assert result["outstanding_holds_minor"] == 25
    assert result["balanced"] is True, result
    assert int(totals[REVENUE]["credit_total_cents"]) == 25
