"""Bank statement import (plan/bank-import-reconciliation-spec.md, slice 1).

The tests ARE the simulated bank: a statement that ties out, a truncated
one, a gapped sequence, an overlapping re-import, a bank that exports
withdrawals as positive numbers, and one that gives no balances at all.

What is being pinned is the evidence posture: raw preserved verbatim,
lines append-only, checks that record whether they RAN as well as whether
they passed, and flagged imports that still land their lines (hiding a
truncated statement is the failure this control exists to catch).
"""

import json
import pathlib

import pytest

import object_banking
import object_execution
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
BANKING_OBJECTS = PACKAGES / "app-banking" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

ACCOUNT = "bank-1"
PROFILE = "profile-1"
COLUMN_MAP = {"date": "Date", "amount": "Amount", "description": "Description",
              "date_format": "%m/%d/%Y"}


def _header(pkg, name):
    schema = json.loads((PACKAGES / pkg / "schemas" / f"{name}.json").read_text())
    return "\t".join(f["name"] for f in schema["fields"]) + "\n"


def setup_env(tmp_path, monkeypatch, *, column_map=None, has_balances=True):
    data_dir = tmp_path / "data"
    schema_dir = data_dir / "schemas"
    schema_dir.mkdir(parents=True)
    for pkg, name in (("app-banking", "bank_accounts"),
                      ("app-banking", "bank_import_profiles"),
                      ("app-banking", "bank_statement_imports"),
                      ("app-banking", "bank_lines"),
                      ("app-finance", "fin_accounts")):
        (schema_dir / f"{name}.json").write_text(
            (PACKAGES / pkg / "schemas" / f"{name}.json").read_text())
        coll = data_dir / "collections" / name
        coll.mkdir(parents=True)
        (coll / "records.tsv").write_text(_header(pkg, name))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))

    object_records.create_collection_record(
        "fin_accounts", {"id": "acct-cash", "name": "Cash", "account_type": "asset",
                         "owner_id": "dan"}, base_dir=data_dir)
    object_records.create_collection_record(
        "bank_accounts", {"id": ACCOUNT, "name": "Business Checking",
                          "institution": "Test Bank", "last4": "1004",
                          "fin_account_id": "acct-cash", "owner_id": "dan"},
        base_dir=data_dir)
    object_records.create_collection_record(
        "bank_import_profiles",
        {"id": PROFILE, "name": "Test Bank CSV", "bank_account_id": ACCOUNT,
         "source_format": "csv",
         "column_map": json.dumps(column_map if column_map is not None else COLUMN_MAP),
         "has_balances": "true" if has_balances else "false",
         "owner_id": "dan"},
        base_dir=data_dir)
    return data_dir


STATEMENT = """Date,Description,Amount
07/02/2026,ACME CORP PAYMENT INV-1001,1500.00
07/05/2026,MONTHLY SERVICE FEE,-25.00
07/09/2026,OFFICE SUPPLY CO,-142.35
"""


def run_import(payload):
    result = object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "action_import_bank_csv", method="POST",
            payload={"_identity": {"user_id": "dan"}, **payload}),
        roots=[BANKING_OBJECTS])
    return result.result


def import_statement(content=STATEMENT, *, opening=0, closing=133265, **extra):
    payload = {"bank_account_id": ACCOUNT, "profile_id": PROFILE, "content": content,
               "opening_balance_cents": opening, "closing_balance_cents": closing}
    payload.update(extra)
    return run_import(payload)


def lines(data_dir):
    rows = object_records.read_collection_records("bank_lines", base_dir=data_dir)
    rows.sort(key=lambda r: r["posted_on"])
    return rows


def imports(data_dir):
    return object_records.read_collection_records("bank_statement_imports", base_dir=data_dir)


# --- 1. parsing and provenance ------------------------------------------------

def test_import_lands_lines_with_raw_preserved_and_import_stamped(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    result = import_statement()
    assert result["status"] == 200
    assert result["imported"] == 3 and result["duplicates"] == 0
    assert result["import_status"] == "accepted"

    rows = lines(data_dir)
    assert [r["posted_on"] for r in rows] == ["2026-07-02", "2026-07-05", "2026-07-09"]
    assert [int(r["amount_cents"]) for r in rows] == [150000, -2500, -14235]
    assert rows[0]["description"] == "ACME CORP PAYMENT INV-1001"
    # raw keeps the original row verbatim -- the evidence, never rewritten
    assert "1500.00" in rows[0]["raw"] and "ACME CORP" in rows[0]["raw"]
    assert all(r["match_status"] == "unmatched" for r in rows)
    assert all(r["import_id"] == result["import_id"] for r in rows)

    imp = imports(data_dir)[0]
    assert imp["line_count"] == "3"
    assert imp["period_start"] == "2026-07-02" and imp["period_end"] == "2026-07-09"
    assert imp["file_hash"]


def test_two_column_debit_credit_and_flipped_sign_banks(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch, column_map={
        "date": "Posted", "debit": "Withdrawal", "credit": "Deposit",
        "description": "Memo", "date_format": "%Y-%m-%d"})
    content = ("Posted,Memo,Withdrawal,Deposit\n"
               "2026-07-02,CUSTOMER DEPOSIT,,\"1,500.00\"\n"
               "2026-07-05,BANK FEE,25.00,\n")
    result = import_statement(content, opening=0, closing=147500)
    assert result["imported"] == 2
    assert [int(r["amount_cents"]) for r in lines(data_dir)] == [150000, -2500]
    assert result["import_status"] == "accepted"


def test_parenthesised_negatives_and_european_decimals(tmp_path, monkeypatch):
    assert object_banking.parse_cents("(45.00)") == -4500
    assert object_banking.parse_cents("$1,234.56") == 123456
    assert object_banking.parse_cents("1.234,56", decimal_sep=",") == 123456
    assert object_banking.parse_cents("") == 0


# --- 2. the statement checks itself -------------------------------------------

def test_tie_out_passes_on_a_good_file(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    result = import_statement(opening=0, closing=133265)
    assert result["import_status"] == "accepted"
    assert result["checks"]["tie_out"] == {
        "ran": True, "passed": True, "expected_closing_cents": 133265,
        "stated_closing_cents": 133265, "delta_cents": 0}


def test_truncated_file_is_flagged_with_the_delta_but_lines_still_land(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    truncated = "\n".join(STATEMENT.splitlines()[:3]) + "\n"  # last line missing
    result = import_statement(truncated, opening=0, closing=133265)
    assert result["import_status"] == "flagged"
    assert "tie_out" in result["failed_checks"]
    assert result["checks"]["tie_out"]["delta_cents"] == -14235  # the missing row
    # Evidence still lands -- hiding a truncated statement is the failure
    # this control exists to catch.
    assert result["imported"] == 2
    assert imports(data_dir)[0]["status"] == "flagged"


def test_continuity_chains_statements_and_flags_a_gap(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    first = import_statement(opening=0, closing=133265)
    assert first["checks"]["continuity"]["ran"] is False  # nothing to chain to yet

    august = ("Date,Description,Amount\n"
              "08/03/2026,CUSTOMER PAYMENT,500.00\n")
    good = import_statement(august, opening=133265, closing=183265)
    assert good["import_status"] == "accepted"
    assert good["checks"]["continuity"]["passed"] is True

    september = ("Date,Description,Amount\n"
                 "09/03/2026,CUSTOMER PAYMENT,100.00\n")
    gapped = import_statement(september, opening=200000, closing=210000)
    assert gapped["import_status"] == "flagged"
    assert "continuity" in gapped["failed_checks"]
    assert gapped["checks"]["continuity"]["gap_cents"] == 200000 - 183265


def test_balance_less_csv_records_that_no_check_ran(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch, has_balances=False)
    result = run_import({"bank_account_id": ACCOUNT, "profile_id": PROFILE,
                         "content": STATEMENT})
    # Honest weaker assurance: accepted, but the flags say nothing was verified.
    assert result["import_status"] == "accepted"
    assert result["checks"]["tie_out"]["ran"] is False
    assert result["checks"]["continuity"]["ran"] is False
    assert result["failed_checks"] == []


# --- 3. dedup -----------------------------------------------------------------

def test_reimporting_the_same_file_is_a_no_op(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    first = import_statement()
    again = import_statement()
    assert again["already_imported"] is True
    assert again["import_id"] == first["import_id"]
    assert again["imported"] == 0
    assert len(lines(data_dir)) == 3
    assert len(imports(data_dir)) == 1


def test_overlapping_statement_skips_the_lines_already_on_file(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    import_statement()
    overlapping = ("Date,Description,Amount\n"
                   "07/09/2026,OFFICE SUPPLY CO,-142.35\n"   # already imported
                   "07/12/2026,NEW CHARGE,-50.00\n")          # new
    result = import_statement(overlapping, opening=133265, closing=128265)
    assert result["imported"] == 1 and result["duplicates"] == 1
    assert len(lines(data_dir)) == 4


def test_same_day_twins_both_land_but_a_replay_does_not(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    twins = ("Date,Description,Amount\n"
             "07/02/2026,COFFEE,-4.50\n"
             "07/02/2026,COFFEE,-4.50\n")
    first = import_statement(twins, opening=0, closing=-900)
    assert first["imported"] == 2          # two real transactions
    replay = import_statement(twins + "07/03/2026,COFFEE,-4.50\n", opening=0, closing=-1350)
    assert replay["imported"] == 1         # only the new day
    assert replay["duplicates"] == 2
    assert len(lines(data_dir)) == 3


def test_hook_rejects_a_duplicate_line_on_the_http_path(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    import_statement()
    existing = lines(data_dir)[0]
    outcome = object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "hook_bank_lines", method="BEFORE_WRITE",
            payload={"action": "create", "collection": "bank_lines",
                     "record": {"bank_account_id": ACCOUNT,
                                "posted_on": existing["posted_on"],
                                "amount_cents": existing["amount_cents"],
                                "description": existing["description"]}}),
        roots=[BANKING_OBJECTS]).result
    assert outcome["status"] == 409
    assert "already on file" in outcome["error"]

    fresh = object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "hook_bank_lines", method="BEFORE_WRITE",
            payload={"action": "create", "collection": "bank_lines",
                     "record": {"bank_account_id": ACCOUNT, "posted_on": "2026-07-20",
                                "amount_cents": "-999", "description": "SOMETHING NEW"}}),
        roots=[BANKING_OBJECTS]).result
    # A new line is allowed through, with its dedup hash stamped for next time.
    assert fresh["record"]["line_hash"]


def test_external_ids_dedup_when_the_bank_supplies_them(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch, column_map={
        "date": "Date", "amount": "Amount", "description": "Description",
        "external_id": "FITID", "date_format": "%m/%d/%Y"})
    first = ("Date,Description,Amount,FITID\n"
             "07/02/2026,PAYMENT,100.00,TXN-A\n")
    # Same transaction id, description re-worded by the bank on a later export.
    second = ("Date,Description,Amount,FITID\n"
              "07/02/2026,PAYMENT (POSTED),100.00,TXN-A\n")
    assert import_statement(first, opening=0, closing=10000)["imported"] == 1
    result = import_statement(second, opening=0, closing=10000)
    assert result["imported"] == 0 and result["duplicates"] == 1
    assert len(lines(data_dir)) == 1


# --- 4. refusals --------------------------------------------------------------

def test_import_requires_identity_and_ownership(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    anon = object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "action_import_bank_csv", method="POST",
            payload={"bank_account_id": ACCOUNT, "content": STATEMENT}),
        roots=[BANKING_OBJECTS]).result
    assert anon["status"] == 403
    object_records.create_collection_record(
        "bank_accounts", {"id": "bank-2", "name": "Someone Else",
                          "owner_id": "pat"}, base_dir=data_dir)
    other = run_import({"bank_account_id": "bank-2", "profile_id": PROFILE,
                        "content": STATEMENT})
    assert other["status"] == 403


def test_unreadable_statements_are_refused_not_half_read(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    bad_date = ("Date,Description,Amount\n"
                "not-a-date,PAYMENT,100.00\n")
    result = import_statement(bad_date)
    assert result["status"] == 422
    assert lines(data_dir) == [] and imports(data_dir) == []
    empty = import_statement("Date,Description,Amount\n")
    assert empty["status"] == 422


def test_missing_profile_and_bad_column_map_are_reported(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    missing = run_import({"bank_account_id": ACCOUNT, "profile_id": "nope",
                          "content": STATEMENT})
    assert missing["status"] == 404
    object_records.update_collection_record(
        "bank_import_profiles", PROFILE, {"column_map": "{not json"},
        base_dir=data_dir)
    broken = import_statement()
    assert broken["status"] == 409
