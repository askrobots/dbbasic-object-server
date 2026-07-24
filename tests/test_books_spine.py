"""Books spine (plan/books-spine-spec.md): the billing ledger composes real
double-entry journals — payment/refund/bounce/issue composers (system_books,
an event handler), the Reverse action on posted journals, and the
fin_recurring runner (the schema that shipped without an engine).

All reactions per docs/logic-decisions.md #6: post-commit, best-effort,
idempotent by generated_from provenance, balanced by construction and
re-verified before posting.
"""

import json
import pathlib

import object_execution
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"

CASH, AR, REV = "acct-cash", "acct-ar", "acct-rev"


def setup_env(tmp_path, monkeypatch, *, basis="cash", configured=True, books=True):
    data_dir = tmp_path / "data"
    schema_dir = data_dir / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    names = [("app-invoices", "invoices"), ("app-payments", "payments"),
             ("app-payments", "refunds"), ("app-settings", "app_settings")]
    if books:
        names += [("app-finance", "fin_journals"), ("app-finance", "fin_journal_lines"),
                  ("app-finance", "fin_accounts"), ("app-finance", "fin_recurring")]
    for pkg, name in names:
        (schema_dir / f"{name}.json").write_text(
            (PACKAGES / pkg / "schemas" / f"{name}.json").read_text())

    def coll(name, header):
        d = data_dir / "collections" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "records.tsv").write_text(header)

    coll("invoices",
         "id\tnumber\tcustomer_name\tstatus\tissue_date\ttotal_cents"
         "\tpayments_received_cents\trefunded_cents\tamount_paid_cents"
         "\tbalance_due_cents\towner_id\n"
         "inv1\tINV-1\tAcme\tdraft\t2026-07-01\t10000\t\t\t\t\tdan\n")
    coll("payments",
         "id\tinvoice_id\tamount_cents\tmethod\treceived_on\treference\tnotes"
         "\tstatus\trefunded_cents\towner_id\tcreated_at\n")
    coll("refunds",
         "id\tpayment_id\tinvoice_id\tamount_cents\treason\trefunded_on\towner_id\tcreated_at\n")
    settings = "id\tkey\tvalue\tdescription\n"
    settings += f"s0\tpayments.accounting_basis\t{basis}\t\n"
    if configured:
        settings += (f"s1\tpayments.journal.cash_account\t{CASH}\t\n"
                     f"s2\tpayments.journal.receivable_account\t{AR}\t\n"
                     f"s3\tpayments.journal.revenue_account\t{REV}\t\n")
    coll("app_settings", settings)
    if books:
        coll("fin_accounts",
             "id\tname\taccount_type\towner_id\n"
             f"{CASH}\tCash\tasset\tdan\n{AR}\tAR\tasset\tdan\n{REV}\tRevenue\tincome\tdan\n")
        coll("fin_journals",
             "id\tdate\tdescription\tstatus\tkind\tcontact_id\treference\tcurrency"
             "\tgenerated_from\tdebit_total_cents\tcredit_total_cents\towner_id"
             "\tentity_id\tcreated_at\n")
        coll("fin_journal_lines",
             "id\tjournal_id\taccount_id\tdebit_cents\tcredit_cents\tmemo"
             "\towner_id\tentity_id\tcreated_at\n")
        coll("fin_recurring",
             "id\tname\ttemplate_lines\tfrequency\tnext_run\tlast_run\tauto_post"
             "\tis_active\towner_id\tentity_id\tcreated_at\n")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(tmp_path / "objects-unused"))
    return data_dir


RUNTIME = python_object_runtime.PythonObjectRuntime()


def fire(collection, record_id, action):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "system_books", method="EVENT",
            payload={"event": f"{collection}.record.{action}",
                     "collection": collection, "record_id": record_id,
                     "action": action},
        ),
        roots=[PACKAGES / "app-payments" / "objects"],
    )


def run_action(object_id, payload, root):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(object_id, method="POST", payload=payload),
        roots=[PACKAGES / "app-finance" / "objects" / ".."],  # finance objects root
    )


def journals(data_dir):
    return object_records.read_collection_records("fin_journals", base_dir=data_dir)


def lines_for(data_dir, journal_id):
    return [l for l in object_records.read_collection_records("fin_journal_lines", base_dir=data_dir)
            if l["journal_id"] == journal_id]


def make_payment(data_dir, pid="p1", cents="4000", status="received"):
    return object_records.create_collection_record(
        "payments",
        {"id": pid, "invoice_id": "inv1", "amount_cents": cents, "method": "card",
         "received_on": "2026-07-10", "reference": f"ref-{pid}", "status": status,
         "owner_id": "dan"},
        base_dir=data_dir)


def test_cash_basis_payment_composes_posted_entry_idempotently(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch, basis="cash")
    make_payment(data_dir)
    result = fire("payments", "p1", "created")
    assert result.ok, result.error
    assert result.result.get("posted") is True

    js = journals(data_dir)
    assert len(js) == 1
    j = js[0]
    assert j["generated_from"] == "payments/p1"
    assert j["status"] == "posted"
    assert j["kind"] == "standard"
    ls = {l["account_id"]: l for l in lines_for(data_dir, j["id"])}
    assert ls[CASH]["debit_cents"] == "4000" and ls[CASH]["credit_cents"] == "0"
    assert ls[REV]["credit_cents"] == "4000" and ls[REV]["debit_cents"] == "0"

    # Replay: composes nothing new.
    replay = fire("payments", "p1", "created")
    assert "already composed" in str(replay.result)
    assert len(journals(data_dir)) == 1


def test_accrual_full_chain_issue_payment_refund_bounce(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch, basis="accrual")
    # issue: draft -> sent
    object_records.update_collection_record("invoices", "inv1", {"status": "sent"},
                                            base_dir=data_dir)
    fire("invoices", "inv1", "updated")
    # pay + refund
    make_payment(data_dir, "p1", "10000")
    fire("payments", "p1", "created")
    object_records.create_collection_record(
        "refunds", {"id": "r1", "payment_id": "p1", "invoice_id": "inv1",
                    "amount_cents": "2500", "refunded_on": "2026-07-12", "owner_id": "dan"},
        base_dir=data_dir)
    fire("refunds", "r1", "created")

    by_from = {j["generated_from"]: j for j in journals(data_dir)}
    assert set(by_from) == {"invoices/inv1:issued", "payments/p1", "refunds/r1"}
    issue = by_from["invoices/inv1:issued"]
    ls = {l["account_id"]: l for l in lines_for(data_dir, issue["id"])}
    assert ls[AR]["debit_cents"] == "10000" and ls[REV]["credit_cents"] == "10000"
    pay = by_from["payments/p1"]
    ls = {l["account_id"]: l for l in lines_for(data_dir, pay["id"])}
    assert ls[CASH]["debit_cents"] == "10000" and ls[AR]["credit_cents"] == "10000"
    ref = by_from["refunds/r1"]
    ls = {l["account_id"]: l for l in lines_for(data_dir, ref["id"])}
    assert ls[AR]["debit_cents"] == "2500" and ls[CASH]["credit_cents"] == "2500"
    assert all(j["status"] == "posted" for j in by_from.values())

    # bounce reverses the payment entry
    object_records.update_collection_record("payments", "p1", {"status": "bounced"},
                                            base_dir=data_dir)
    fire("payments", "p1", "updated")
    by_from = {j["generated_from"]: j for j in journals(data_dir)}
    assert "payments/p1:bounced" in by_from
    bounce = by_from["payments/p1:bounced"]
    assert bounce["kind"] == "reversing"
    ls = {l["account_id"]: l for l in lines_for(data_dir, bounce["id"])}
    assert ls[AR]["debit_cents"] == "10000" and ls[CASH]["credit_cents"] == "10000"


def test_unconfigured_accounts_and_missing_books_are_soft(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch, configured=False)
    make_payment(data_dir)
    result = fire("payments", "p1", "created")
    assert "unconfigured" in str(result.result)
    assert journals(data_dir) == []


def test_missing_fin_journals_collection_noops(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch, books=False)
    make_payment(data_dir)
    result = fire("payments", "p1", "created")
    assert result.ok
    assert "books not installed" in str(result.result)


def make_posted_journal(data_dir, jid="j1", owner="dan"):
    object_records.create_collection_record(
        "fin_journals",
        {"id": jid, "date": "2026-07-01", "description": "Manual entry",
         "status": "draft", "kind": "standard", "owner_id": owner},
        base_dir=data_dir)
    for lid, acct, dr, cr in ((f"{jid}-l1", CASH, "500", "0"), (f"{jid}-l2", REV, "0", "500")):
        object_records.create_collection_record(
            "fin_journal_lines",
            {"id": lid, "journal_id": jid, "account_id": acct,
             "debit_cents": dr, "credit_cents": cr, "owner_id": owner},
            base_dir=data_dir)
    object_records.update_collection_record("fin_journals", jid, {"status": "posted"},
                                            base_dir=data_dir)


def reverse(payload):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "action_reverse_journal", method="POST", payload=payload),
        roots=[PACKAGES / "app-finance" / "objects"],
    )


def test_reverse_action_composes_mirror_and_refuses_repeats(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_posted_journal(data_dir)

    ok = reverse({"journal_id": "j1", "_identity": {"user_id": "dan", "roles": []}})
    assert ok.ok and ok.result["status"] == 200 and ok.result["posted"] is True
    mirror_id = ok.result["reversal_id"]
    mirror = object_records.get_collection_record("fin_journals", mirror_id, base_dir=data_dir)
    assert mirror["kind"] == "reversing"
    assert mirror["generated_from"] == "reversal:j1"
    ls = {l["account_id"]: l for l in lines_for(data_dir, mirror_id)}
    assert ls[CASH]["credit_cents"] == "500" and ls[REV]["debit_cents"] == "500"  # swapped

    again = reverse({"journal_id": "j1", "_identity": {"user_id": "dan", "roles": []}})
    assert again.result["status"] == 409 and "Already reversed" in again.result["error"]

    stranger = reverse({"journal_id": mirror_id, "_identity": {"user_id": "mallory", "roles": []}})
    assert stranger.result["status"] == 403


def test_reverse_refuses_draft(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    object_records.create_collection_record(
        "fin_journals", {"id": "jd", "date": "2026-07-01", "description": "draft",
                         "status": "draft", "owner_id": "dan"}, base_dir=data_dir)
    result = reverse({"journal_id": "jd", "_identity": {"user_id": "dan", "roles": []}})
    assert result.result["status"] == 409


def run_recurring(payload=None):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "system_fin_recurring_runner", method="POST", payload=payload or {}),
        roots=[PACKAGES / "app-finance" / "objects"],
    )


def add_template(data_dir, tid="t1", lines=None, auto_post="true", next_run="2026-07-01",
                 frequency="monthly", active="true"):
    tmpl = json.dumps(lines if lines is not None else [
        {"account_id": REV, "debit_cents": 100, "credit_cents": 0, "memo": "depr"},
        {"account_id": CASH, "debit_cents": 0, "credit_cents": 100},
    ])
    object_records.create_collection_record(
        "fin_recurring",
        {"id": tid, "name": "Monthly depreciation", "template_lines": tmpl,
         "frequency": frequency, "next_run": next_run, "auto_post": auto_post,
         "is_active": active, "owner_id": "dan"},
        base_dir=data_dir)


def test_recurring_runner_composes_advances_and_is_idempotent(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    add_template(data_dir)

    result = run_recurring({"today": "2026-07-24"})
    assert result.ok and result.result["ran"] == 1
    js = journals(data_dir)
    assert len(js) == 1
    assert js[0]["generated_from"] == "fin_recurring/t1:2026-07-01"
    assert js[0]["kind"] == "adjusting" and js[0]["status"] == "posted"
    tpl = object_records.get_collection_record("fin_recurring", "t1", base_dir=data_dir)
    assert tpl["next_run"] == "2026-08-01" and tpl["last_run"] == "2026-07-01"

    # Same pass again: next_run now 2026-08-01 > today -> nothing runs.
    result = run_recurring({"today": "2026-07-24"})
    assert result.result["ran"] == 0
    assert len(journals(data_dir)) == 1


def test_recurring_unbalanced_template_stays_draft(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    add_template(data_dir, tid="t2", lines=[
        {"account_id": REV, "debit_cents": 100, "credit_cents": 0},
        {"account_id": CASH, "debit_cents": 0, "credit_cents": 90},
    ])
    result = run_recurring({"today": "2026-07-24"})
    assert result.result["ran"] == 1
    j = journals(data_dir)[0]
    assert j["status"] == "draft"


def test_recurring_inactive_and_future_skipped(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    add_template(data_dir, tid="t3", active="false")
    add_template(data_dir, tid="t4", next_run="2027-01-01")
    result = run_recurring({"today": "2026-07-24"})
    assert result.result["ran"] == 0
    assert journals(data_dir) == []
