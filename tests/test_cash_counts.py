"""Counting the accounts nobody sends a statement for
(plan/value-accounts-and-denominations-spec.md §4-5).

A till is real money with no independent witness, so the interesting tests
are not the arithmetic -- they are the controls: a witness can be required,
the witness cannot be the counter, and a variance is booked as a real
expense rather than quietly dropped.
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

CASH, OVER_SHORT = "acct-cash", "acct-over-short"
TILL = "va-till"


def _header(pkg, name):
    schema = json.loads((PACKAGES / pkg / "schemas" / f"{name}.json").read_text())
    return "\t".join(f["name"] for f in schema["fields"]) + "\n"


def setup_env(tmp_path, monkeypatch, *, settings=(("reconcile.journal.cash_over_short_account", OVER_SHORT),)):
    data_dir = tmp_path / "data"
    schema_dir = data_dir / "schemas"
    schema_dir.mkdir(parents=True)
    for pkg, name in (("app-banking", "value_accounts"),
                      ("app-banking", "value_account_counts"),
                      ("app-finance", "fin_accounts"),
                      ("app-finance", "fin_journals"),
                      ("app-finance", "fin_journal_lines"),
                      ("app-finance", "denominations"),
                      ("app-settings", "app_settings")):
        (schema_dir / f"{name}.json").write_text(
            (PACKAGES / pkg / "schemas" / f"{name}.json").read_text())
        coll = data_dir / "collections" / name
        coll.mkdir(parents=True)
        coll.joinpath("records.tsv").write_text(_header(pkg, name))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))

    rows = _header("app-settings", "app_settings")
    for i, (k, v) in enumerate(settings):
        rows += f"s{i}\t{k}\t{v}\t\n"
    (data_dir / "collections" / "app_settings" / "records.tsv").write_text(rows)

    for acct, kind in ((CASH, "asset"), (OVER_SHORT, "expense")):
        object_records.create_collection_record(
            "fin_accounts", {"id": acct, "name": acct, "account_type": kind,
                             "owner_id": "dan"}, base_dir=data_dir)
    return data_dir


def make_till(data_dir, *, requires_witness=False, verification="physical_count",
              aid=TILL):
    return object_records.create_collection_record(
        "value_accounts",
        {"id": aid, "name": "Front Register", "kind": "cash_box",
         "fin_account_id": CASH, "custody": "self", "custodian": "front register",
         "verification": verification,
         "requires_second_attestor": "true" if requires_witness else "false",
         "owner_id": "dan"},
        base_dir=data_dir)


def book_cash(data_dir, amount_minor, *, jid="j-open", on="2026-07-01"):
    """Put a posted balance in the till's book account."""
    object_records.create_collection_record(
        "fin_journals",
        {"id": jid, "date": on, "description": "Opening float", "status": "posted",
         "kind": "standard", "owner_id": "dan"}, base_dir=data_dir)
    object_records.create_collection_record(
        "fin_journal_lines",
        {"id": f"{jid}-dr", "journal_id": jid, "account_id": CASH,
         "debit_cents": str(amount_minor), "credit_cents": "0", "owner_id": "dan"},
        base_dir=data_dir)
    object_records.create_collection_record(
        "fin_journal_lines",
        {"id": f"{jid}-cr", "journal_id": jid, "account_id": OVER_SHORT,
         "debit_cents": "0", "credit_cents": str(amount_minor), "owner_id": "dan"},
        base_dir=data_dir)


def count(payload, *, user="dan"):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "action_record_count", method="POST",
            payload={"_identity": {"user_id": user}, **payload}),
        roots=[BANKING_OBJECTS]).result


def counts(data_dir):
    return object_records.read_collection_records("value_account_counts", base_dir=data_dir)


def journal_lines(data_dir, journal_id):
    return {l["account_id"]: l for l in
            object_records.read_collection_records("fin_journal_lines", base_dir=data_dir)
            if l["journal_id"] == journal_id}


def test_a_matching_count_records_evidence_and_books_nothing(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_till(data_dir)
    book_cash(data_dir, 20000)
    result = count({"value_account_id": TILL, "counted_minor": 20000,
                    "counted_on": "2026-07-31"})
    assert result["status"] == 200 and result["variance_minor"] == 0
    assert result["journal"] is None
    row = counts(data_dir)[0]
    # The book figure is stamped, so the count stays meaningful later.
    assert row["book_balance_minor"] == "20000"
    assert row["counted_by"] == "dan"


def test_a_shortage_books_cash_over_short(tmp_path, monkeypatch):
    """The drawer is light: that is an expense the business really incurred,
    and booking it is what makes a slow leak visible as a number."""
    data_dir = setup_env(tmp_path, monkeypatch)
    make_till(data_dir)
    book_cash(data_dir, 20000)
    result = count({"value_account_id": TILL, "counted_minor": 19750,
                    "counted_on": "2026-07-31"})
    assert result["variance_minor"] == -250
    assert result["journal"]["posted"] is True
    booked = journal_lines(data_dir, result["journal"]["journal_id"])
    assert booked[OVER_SHORT]["debit_cents"] == "250"   # expense up
    assert booked[CASH]["credit_cents"] == "250"        # cash down
    assert counts(data_dir)[0]["journal_id"] == result["journal"]["journal_id"]


def test_an_overage_books_the_mirror(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_till(data_dir)
    book_cash(data_dir, 20000)
    result = count({"value_account_id": TILL, "counted_minor": 20100})
    assert result["variance_minor"] == 100
    booked = journal_lines(data_dir, result["journal"]["journal_id"])
    assert booked[CASH]["debit_cents"] == "100"
    assert booked[OVER_SHORT]["credit_cents"] == "100"


def test_an_account_can_refuse_a_self_certified_count(tmp_path, monkeypatch):
    """The oldest control in retail, as one field: the person counting the
    till is usually the person who could take from it."""
    data_dir = setup_env(tmp_path, monkeypatch)
    make_till(data_dir, requires_witness=True)
    book_cash(data_dir, 20000)
    unwitnessed = count({"value_account_id": TILL, "counted_minor": 19750})
    assert unwitnessed["status"] == 409 and "witness" in unwitnessed["error"]
    assert counts(data_dir) == []

    witnessed = count({"value_account_id": TILL, "counted_minor": 19750,
                       "witnessed_by": "pat"})
    assert witnessed["status"] == 200
    assert witnessed["assurance"] == "medium"      # lifted, but still not a statement
    assert counts(data_dir)[0]["witnessed_by"] == "pat"


def test_the_witness_cannot_be_the_counter(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_till(data_dir, requires_witness=True)
    book_cash(data_dir, 20000)
    result = count({"value_account_id": TILL, "counted_minor": 19750,
                    "witnessed_by": "dan"}, user="dan")
    assert result["status"] == 409
    assert "other than the person counting" in result["error"]


def test_an_unwitnessed_count_is_rated_weakest(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_till(data_dir)                      # witness not required
    book_cash(data_dir, 20000)
    result = count({"value_account_id": TILL, "counted_minor": 20000})
    assert result["assurance"] == "weak"     # self-certification, and it says so


def test_a_variance_with_nowhere_to_go_is_refused(tmp_path, monkeypatch):
    """Better to refuse than to record a count whose difference silently
    never reaches the ledger."""
    data_dir = setup_env(tmp_path, monkeypatch, settings=())
    make_till(data_dir)
    book_cash(data_dir, 20000)
    result = count({"value_account_id": TILL, "counted_minor": 19750})
    assert result["status"] == 409 and "cash_over_short_account" in result["error"]
    assert counts(data_dir) == []


def test_only_counted_accounts_can_be_counted(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_till(data_dir, verification="statement_import", aid="va-bank")
    result = count({"value_account_id": "va-bank", "counted_minor": 100})
    assert result["status"] == 409
    assert "Import its statement instead" in result["error"]


def test_refusals(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_till(data_dir)
    assert count({"value_account_id": TILL})["status"] == 400
    assert count({"value_account_id": TILL, "counted_minor": -5})["status"] == 400
    assert count({"value_account_id": "nope", "counted_minor": 100})["status"] == 404
    other = count({"value_account_id": TILL, "counted_minor": 100}, user="mallory")
    assert other["status"] == 403
    anon = object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "action_record_count", method="POST",
            payload={"value_account_id": TILL, "counted_minor": 100}),
        roots=[BANKING_OBJECTS]).result
    assert anon["status"] == 403
