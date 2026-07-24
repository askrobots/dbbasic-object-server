"""The bank reconciliation statement (plan/bank-import-reconciliation-spec.md
section 6): object_banking.reconciliation()'s fold math, and site_reconcile,
the page that renders it.

The control under test is the same one tests/test_bank_reconciliation.py
checks for the matcher and resolution verbs: nobody -- not an anonymous
visitor, not another signed-in owner -- can see a bank account's
reconciliation state except the owner who set it up. The math itself is
the classic tie: bank closing balance minus book balance should equal
exactly the outstanding timing items once the unmatched tail is empty.
"""

import json
import pathlib

import object_banking
import object_execution
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
BANKING_OBJECTS = PACKAGES / "app-banking" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

DAN = {"user_id": "dan"}
PAT = {"user_id": "pat"}


def _header(pkg, name):
    schema = json.loads((PACKAGES / pkg / "schemas" / f"{name}.json").read_text())
    return "\t".join(f["name"] for f in schema["fields"]) + "\n"


def setup_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    schema_dir = data_dir / "schemas"
    schema_dir.mkdir(parents=True)
    wanted = [("app-banking", "bank_accounts"), ("app-banking", "bank_statement_imports"),
              ("app-banking", "bank_lines"),
              ("app-finance", "fin_accounts"), ("app-finance", "fin_journals"),
              ("app-finance", "fin_journal_lines")]
    for pkg, name in wanted:
        (schema_dir / f"{name}.json").write_text(
            (PACKAGES / pkg / "schemas" / f"{name}.json").read_text())
        coll = data_dir / "collections" / name
        coll.mkdir(parents=True)
        (coll / "records.tsv").write_text(_header(pkg, name))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    return data_dir


def make_fin_account(data_dir, aid, owner="dan"):
    return object_records.create_collection_record(
        "fin_accounts", {"id": aid, "name": aid, "account_type": "asset", "owner_id": owner},
        base_dir=data_dir)


def make_bank_account(data_dir, bid, *, fin_account_id, owner="dan", name=None, currency="USD"):
    return object_records.create_collection_record(
        "bank_accounts",
        {"id": bid, "name": name or bid, "fin_account_id": fin_account_id,
         "owner_id": owner, "currency": currency},
        base_dir=data_dir)


def make_import(data_dir, iid, *, bank_account_id, period_end, closing_cents,
                opening_cents=0, status="accepted", flags=None, owner="dan"):
    return object_records.create_collection_record(
        "bank_statement_imports",
        {"id": iid, "bank_account_id": bank_account_id, "period_start": "2026-07-01",
         "period_end": period_end, "opening_balance_cents": str(opening_cents),
         "closing_balance_cents": str(closing_cents), "status": status,
         "flags": json.dumps(flags or {}), "owner_id": owner},
        base_dir=data_dir)


def make_line(data_dir, lid, *, bank_account_id, amount, on="2026-07-02", desc="",
             status="unmatched", resolved_as="", owner="dan", suggestions=""):
    return object_records.create_collection_record(
        "bank_lines",
        {"id": lid, "bank_account_id": bank_account_id, "posted_on": on,
         "amount_cents": str(amount), "description": desc, "raw": f"raw:{desc}",
         "line_hash": f"h-{lid}", "match_status": status, "resolved_as": resolved_as,
         "suggestions": suggestions, "owner_id": owner},
        base_dir=data_dir)


def make_journal(data_dir, jid, *, date="2026-07-05", status="posted", owner="dan"):
    return object_records.create_collection_record(
        "fin_journals", {"id": jid, "date": date, "status": status, "owner_id": owner},
        base_dir=data_dir)


def make_journal_line(data_dir, lid, *, journal_id, account_id, debit=0, credit=0, owner="dan"):
    return object_records.create_collection_record(
        "fin_journal_lines",
        {"id": lid, "journal_id": journal_id, "account_id": account_id,
         "debit_cents": str(debit), "credit_cents": str(credit), "owner_id": owner},
        base_dir=data_dir)


def page(method, payload):
    result = object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "site_reconcile", method=method, payload=payload),
        roots=[BANKING_OBJECTS])
    return result.result


# --- reconciliation() fold math ----------------------------------------------

def test_no_data_folds_to_all_nones_never_raises(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    result = object_banking.reconciliation("no-such-account", base_dir=tmp_path / "data")
    assert result["bank_closing_cents"] is None
    assert result["book_balance_cents"] is None
    assert result["difference_cents"] is None
    assert result["reconciled"] is False
    assert result["assurance"] is None


def test_bank_closing_book_balance_and_difference(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_fin_account(data_dir, "cash")
    make_bank_account(data_dir, "bank-1", fin_account_id="cash")
    make_import(data_dir, "imp1", bank_account_id="bank-1",
               period_end="2026-07-31", closing_cents=100000,
               flags={"tie_out": {"ran": True, "passed": True}})
    # Book side: one posted journal debiting cash 90000.
    make_journal(data_dir, "j1")
    make_journal_line(data_dir, "jl1", journal_id="j1", account_id="cash", debit=90000)

    result = object_banking.reconciliation("bank-1", base_dir=data_dir)
    assert result["bank_closing_cents"] == 100000
    assert result["bank_statement_date"] == "2026-07-31"
    assert result["book_balance_cents"] == 90000
    assert result["difference_cents"] == 10000


def test_reconciled_true_when_difference_equals_timing_items(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_fin_account(data_dir, "cash")
    make_bank_account(data_dir, "bank-1", fin_account_id="cash")
    make_import(data_dir, "imp1", bank_account_id="bank-1",
               period_end="2026-07-31", closing_cents=100000,
               flags={"tie_out": {"ran": True, "passed": True}})
    make_journal(data_dir, "j1")
    make_journal_line(data_dir, "jl1", journal_id="j1", account_id="cash", debit=90000)
    # A deposit-in-transit: on the statement (part of the 100000 closing
    # balance) but never booked -- classic outstanding timing item.
    make_line(data_dir, "L1", bank_account_id="bank-1", amount=10000, desc="DEPOSIT IN TRANSIT",
              status="resolved", resolved_as="timing")

    result = object_banking.reconciliation("bank-1", base_dir=data_dir)
    assert result["timing_cents"] == 10000
    assert result["timing_count"] == 1
    assert result["difference_cents"] == 10000
    assert result["reconciled"] is True


def test_unmatched_tail_breaks_reconciliation(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_fin_account(data_dir, "cash")
    make_bank_account(data_dir, "bank-1", fin_account_id="cash")
    make_import(data_dir, "imp1", bank_account_id="bank-1",
               period_end="2026-07-31", closing_cents=100000,
               flags={"tie_out": {"ran": True, "passed": True}})
    make_journal(data_dir, "j1")
    make_journal_line(data_dir, "jl1", journal_id="j1", account_id="cash", debit=90000)
    # Same 10000 gap, but this time it is an unexplained unmatched line,
    # not a timing item -- the numbers still differ by 10000, but the
    # difference is not EXPLAINED, so reconciled must be False.
    make_line(data_dir, "L1", bank_account_id="bank-1", amount=10000, desc="MYSTERY DEPOSIT",
              status="unmatched")

    result = object_banking.reconciliation("bank-1", base_dir=data_dir)
    assert result["unmatched_cents"] == 10000
    assert result["unmatched_count"] == 1
    assert result["difference_cents"] == 10000
    assert result["timing_cents"] == 0
    assert result["reconciled"] is False


def test_matched_and_resolved_non_timing_lines_count_as_reconciled(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_bank_account(data_dir, "bank-1", fin_account_id="")
    make_line(data_dir, "L1", bank_account_id="bank-1", amount=5000, status="matched")
    make_line(data_dir, "L2", bank_account_id="bank-1", amount=-2500, status="resolved", resolved_as="fee")
    make_line(data_dir, "L3", bank_account_id="bank-1", amount=200, status="suggested")
    make_line(data_dir, "L4", bank_account_id="bank-1", amount=100, status="unmatched")

    result = object_banking.reconciliation("bank-1", base_dir=data_dir)
    assert result["matched_cents"] == 5000 + (-2500)
    assert result["unmatched_cents"] == 200 + 100
    assert result["suggested_count"] == 1
    assert result["unmatched_count"] == 1
    # No fin_account_id on this bank account -- book_balance_cents is
    # genuinely unknown, not zero.
    assert result["book_balance_cents"] is None
    assert result["difference_cents"] is None


def test_as_of_scopes_imports_and_journals(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_fin_account(data_dir, "cash")
    make_bank_account(data_dir, "bank-1", fin_account_id="cash")
    make_import(data_dir, "imp1", bank_account_id="bank-1",
               period_end="2026-06-30", closing_cents=50000,
               flags={"tie_out": {"ran": True, "passed": True}})
    make_import(data_dir, "imp2", bank_account_id="bank-1",
               period_end="2026-07-31", closing_cents=100000,
               flags={"tie_out": {"ran": True, "passed": True}})
    make_journal(data_dir, "j1", date="2026-06-15")
    make_journal_line(data_dir, "jl1", journal_id="j1", account_id="cash", debit=50000)
    make_journal(data_dir, "j2", date="2026-07-15")
    make_journal_line(data_dir, "jl2", journal_id="j2", account_id="cash", debit=50000)

    as_of_june = object_banking.reconciliation("bank-1", base_dir=data_dir, as_of="2026-06-30")
    assert as_of_june["bank_closing_cents"] == 50000
    assert as_of_june["book_balance_cents"] == 50000

    unscoped = object_banking.reconciliation("bank-1", base_dir=data_dir)
    assert unscoped["bank_closing_cents"] == 100000
    assert unscoped["book_balance_cents"] == 100000


# --- assurance levels ---------------------------------------------------------

def test_assurance_verified_when_tie_out_passed(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_bank_account(data_dir, "bank-1", fin_account_id="")
    make_import(data_dir, "imp1", bank_account_id="bank-1", period_end="2026-07-31",
               closing_cents=100000, flags={"tie_out": {"ran": True, "passed": True}})
    assert object_banking.reconciliation("bank-1", base_dir=data_dir)["assurance"] == "verified"


def test_assurance_unverified_when_tie_out_did_not_run(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_bank_account(data_dir, "bank-1", fin_account_id="")
    make_import(data_dir, "imp1", bank_account_id="bank-1", period_end="2026-07-31",
               closing_cents=100000,
               flags={"tie_out": {"ran": False, "reason": "statement carried no balances"}})
    assert object_banking.reconciliation("bank-1", base_dir=data_dir)["assurance"] == "unverified"


def test_assurance_flagged_when_a_check_failed(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_bank_account(data_dir, "bank-1", fin_account_id="")
    make_import(data_dir, "imp1", bank_account_id="bank-1", period_end="2026-07-31",
               closing_cents=90000, status="flagged",
               flags={"tie_out": {"ran": True, "passed": False, "delta_cents": -10000}})
    assert object_banking.reconciliation("bank-1", base_dir=data_dir)["assurance"] == "flagged"


def test_assurance_reflects_latest_import_even_when_bank_closing_falls_back(tmp_path, monkeypatch):
    """The newest statement failed tie-out (flagged), so its own balance is
    not trustworthy enough to report as bank_closing_cents -- that falls
    back to the last accepted statement. But assurance must still surface
    the flagged state of the NEWEST statement; silently showing 'verified'
    off an old import would hide exactly the anti-fraud signal this report
    exists to raise."""
    data_dir = setup_env(tmp_path, monkeypatch)
    make_bank_account(data_dir, "bank-1", fin_account_id="")
    make_import(data_dir, "imp1", bank_account_id="bank-1", period_end="2026-06-30",
               closing_cents=50000, status="accepted",
               flags={"tie_out": {"ran": True, "passed": True}})
    make_import(data_dir, "imp2", bank_account_id="bank-1", period_end="2026-07-31",
               closing_cents=999999, status="flagged",
               flags={"tie_out": {"ran": True, "passed": False, "delta_cents": -500}})

    result = object_banking.reconciliation("bank-1", base_dir=data_dir)
    assert result["bank_closing_cents"] == 50000  # last trustworthy number
    assert result["bank_statement_date"] == "2026-06-30"
    assert result["assurance"] == "flagged"        # but the newest import failed


# --- the page: identity gate + owner scoping ----------------------------------

def test_anonymous_visitor_sees_a_sign_in_prompt_and_no_data(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_fin_account(data_dir, "cash")
    make_bank_account(data_dir, "bank-1", fin_account_id="cash", name="Dan's Checking")
    make_line(data_dir, "L1", bank_account_id="bank-1", amount=5000, desc="SECRET DEPOSIT")

    body = page("GET", {})["body"]
    assert "Sign in" in body
    assert "Dan's Checking" not in body
    assert "SECRET DEPOSIT" not in body


def test_another_owners_account_and_lines_never_appear(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_fin_account(data_dir, "cash-dan", owner="dan")
    make_fin_account(data_dir, "cash-pat", owner="pat")
    make_bank_account(data_dir, "bank-dan", fin_account_id="cash-dan", owner="dan", name="Dan Checking")
    make_bank_account(data_dir, "bank-pat", fin_account_id="cash-pat", owner="pat", name="Pat Checking")
    make_line(data_dir, "Ldan", bank_account_id="bank-dan", amount=1000, desc="DAN LINE", owner="dan")
    make_line(data_dir, "Lpat", bank_account_id="bank-pat", amount=2000, desc="PAT LINE", owner="pat")

    dan_body = page("GET", {"_identity": DAN})["body"]
    assert "Dan Checking" in dan_body
    assert "Pat Checking" not in dan_body

    # Pat tries to view Dan's account by guessing its id in the query
    # string -- the page must ignore it rather than looking it up.
    pat_body_snooping = page("GET", {"_identity": PAT, "account": "bank-dan"})["body"]
    assert "Dan Checking" not in pat_body_snooping
    assert "DAN LINE" not in pat_body_snooping
    assert "not found among yours" in pat_body_snooping

    # And when Pat legitimately opens their own account, only their own
    # lines show up.
    pat_body_own = page("GET", {"_identity": PAT, "account": "bank-pat"})["body"]
    assert "Pat Checking" in pat_body_own
    assert "PAT LINE" in pat_body_own
    assert "DAN LINE" not in pat_body_own


def test_page_renders_the_statement_for_a_selected_account(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_fin_account(data_dir, "cash")
    make_bank_account(data_dir, "bank-1", fin_account_id="cash", name="Business Checking")
    make_import(data_dir, "imp1", bank_account_id="bank-1", period_end="2026-07-31",
               closing_cents=100000, flags={"tie_out": {"ran": True, "passed": True}})
    make_journal(data_dir, "j1")
    make_journal_line(data_dir, "jl1", journal_id="j1", account_id="cash", debit=90000)
    make_line(data_dir, "L1", bank_account_id="bank-1", amount=10000, desc="DEPOSIT IN TRANSIT",
              status="resolved", resolved_as="timing")
    make_line(data_dir, "L2", bank_account_id="bank-1", amount=500, desc="UNKNOWN CREDIT",
              status="suggested", suggestions=json.dumps(
                  [{"tier": 2, "refs": ["payments/p1"], "why": "exact amount within 3 day(s)"}]))

    body = page("GET", {"_identity": DAN, "account": "bank-1"})["body"]
    assert "Business Checking" in body
    assert "USD 1,000.00" in body   # bank closing balance
    assert "USD 900.00" in body     # book balance
    assert "Reconciled" in body
    assert "DEPOSIT IN TRANSIT" in body
    assert "UNKNOWN CREDIT" in body
    assert "exact amount within 3 day(s)" in body
