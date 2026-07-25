"""OFX import through the action (plan/bank-import-reconciliation-spec.md
section 3, v1.5).

Parsing itself is covered in test_object_ofx.py; what matters here is that
OFX lands in the SAME canonical shape as CSV -- one landing zone, thin
importers -- and that the two things OFX carries and CSV usually does not
are actually put to work: FITID makes dedup exact, and a stated closing
balance plus a chained opening lets the statement check its own arithmetic.
"""

import json
import pathlib

import object_execution
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
BANKING_OBJECTS = PACKAGES / "app-banking" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

ACCOUNT = "bank-1"


def _header(pkg, name):
    schema = json.loads((PACKAGES / pkg / "schemas" / f"{name}.json").read_text())
    return "\t".join(f["name"] for f in schema["fields"]) + "\n"


def setup_env(tmp_path, monkeypatch):
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
        "bank_accounts", {"id": ACCOUNT, "name": "Checking",
                          "fin_account_id": "acct-cash", "owner_id": "dan"},
        base_dir=data_dir)
    return data_dir


def ofx(transactions, *, closing="1475.00", start="20260701", end="20260731"):
    body = "".join(
        f"<STMTTRN><TRNTYPE>{t['type']}<DTPOSTED>{t['date']}<TRNAMT>{t['amount']}"
        f"<FITID>{t['fitid']}<NAME>{t['name']}</STMTTRN>\n"
        for t in transactions)
    return f"""OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>
<CURDEF>USD
<BANKACCTFROM><BANKID>021000021<ACCTID>1234567890<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST><DTSTART>{start}<DTEND>{end}
{body}</BANKTRANLIST>
<LEDGERBAL><BALAMT>{closing}<DTASOF>{end}</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"""


JULY = ofx([
    {"type": "CREDIT", "date": "20260702120000[-5:EST]", "amount": "1500.00",
     "fitid": "TXN-A", "name": "ACME CORP PAYMENT"},
    {"type": "DEBIT", "date": "20260705", "amount": "-25.00",
     "fitid": "TXN-B", "name": "SERVICE FEE"},
])


def run(payload):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "action_import_ofx", method="POST",
            payload={"_identity": {"user_id": "dan"}, **payload}),
        roots=[BANKING_OBJECTS]).result


def lines(data_dir):
    rows = object_records.read_collection_records("bank_lines", base_dir=data_dir)
    rows.sort(key=lambda r: r["posted_on"])
    return rows


def imports(data_dir):
    return object_records.read_collection_records(
        "bank_statement_imports", base_dir=data_dir)


def test_ofx_lands_in_the_same_shape_as_csv(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    result = run({"bank_account_id": ACCOUNT, "content": JULY,
                  "opening_balance_cents": 0})
    assert result["status"] == 200 and result["imported"] == 2
    rows = lines(data_dir)
    assert [int(r["amount_cents"]) for r in rows] == [150000, -2500]
    assert [r["posted_on"] for r in rows] == ["2026-07-02", "2026-07-05"]
    # FITID travels through as external_id -- the strong dedup key.
    assert [r["external_id"] for r in rows] == ["TXN-A", "TXN-B"]
    assert all(r["raw"] for r in rows)
    imp = imports(data_dir)[0]
    assert imp["source_format"] == "ofx"
    assert imp["closing_balance_cents"] == "147500"     # read from LEDGERBAL
    assert imp["status"] == "accepted"                  # 0 + 147500 == 147500


def test_fitid_dedups_even_when_the_bank_rewords_the_memo(tmp_path, monkeypatch):
    """The reason OFX beats CSV: a content hash breaks when a pending
    transaction posts under a tidied-up description; the bank's own id
    does not."""
    data_dir = setup_env(tmp_path, monkeypatch)
    run({"bank_account_id": ACCOUNT, "content": JULY, "opening_balance_cents": 0})
    reworded = ofx([
        {"type": "CREDIT", "date": "20260702120000[-5:EST]", "amount": "1500.00",
         "fitid": "TXN-A", "name": "ACME CORP PAYMENT (POSTED)"},
        {"type": "DEBIT", "date": "20260709", "amount": "-142.35",
         "fitid": "TXN-C", "name": "OFFICE SUPPLY"},
    ], closing="1332.65")
    result = run({"bank_account_id": ACCOUNT, "content": reworded,
                  "opening_balance_cents": 0})
    assert result["duplicates"] == 1 and result["imported"] == 1
    assert len(lines(data_dir)) == 3


def test_the_same_file_twice_is_a_no_op(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    first = run({"bank_account_id": ACCOUNT, "content": JULY, "opening_balance_cents": 0})
    again = run({"bank_account_id": ACCOUNT, "content": JULY, "opening_balance_cents": 0})
    assert again["already_imported"] is True
    assert again["import_id"] == first["import_id"]
    assert len(lines(data_dir)) == 2


def test_a_chained_opening_makes_tie_out_work_and_says_it_was_derived(tmp_path, monkeypatch):
    """OFX states no opening balance, so a lone file cannot tie out. Once a
    prior statement exists its closing balance supplies one -- and the
    import must record that the figure was derived, because it makes
    continuity true by construction."""
    data_dir = setup_env(tmp_path, monkeypatch)
    run({"bank_account_id": ACCOUNT, "content": JULY, "opening_balance_cents": 0})

    august = ofx([{"type": "CREDIT", "date": "20260803", "amount": "500.00",
                   "fitid": "TXN-D", "name": "CUSTOMER PAYMENT"}],
                 closing="1975.00", start="20260801", end="20260831")
    result = run({"bank_account_id": ACCOUNT, "content": august})
    assert result["opening_derived"] is True
    assert result["checks"]["opening_balance"]["derived"] is True
    assert result["checks"]["continuity"]["tautological"] is True
    assert result["checks"]["tie_out"]["passed"] is True     # 147500 + 50000 == 197500
    assert result["import_status"] == "accepted"


def test_a_chained_statement_that_does_not_add_up_is_flagged(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    run({"bank_account_id": ACCOUNT, "content": JULY, "opening_balance_cents": 0})
    wrong = ofx([{"type": "CREDIT", "date": "20260803", "amount": "500.00",
                  "fitid": "TXN-E", "name": "CUSTOMER PAYMENT"}],
                closing="9999.00", start="20260801", end="20260831")
    result = run({"bank_account_id": ACCOUNT, "content": wrong})
    assert result["import_status"] == "flagged"
    assert "tie_out" in result["failed_checks"]
    assert result["imported"] == 1          # evidence still lands


def test_refusals(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    anon = object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "action_import_ofx", method="POST",
            payload={"bank_account_id": ACCOUNT, "content": JULY}),
        roots=[BANKING_OBJECTS]).result
    assert anon["status"] == 403
    assert run({"bank_account_id": "nope", "content": JULY})["status"] == 404
    assert run({"bank_account_id": ACCOUNT, "content": "not an ofx file"})["status"] == 422
    assert run({"bank_account_id": ACCOUNT})["status"] == 400
