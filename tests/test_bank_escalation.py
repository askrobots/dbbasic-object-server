"""system_bank_escalation (plan/bank-import-reconciliation-spec.md section 5,
"THE SIGNAL"): the runner that turns a quietly-growing unreconciled tail --
in EITHER direction -- into a task someone is accountable for.

The control being tested is not "does it flag a stale line" but "does it
ever flag the same item twice, and does it also catch the direction people
forget" (a book payment the bank never confirmed, which is the more
serious of the two per the spec).
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
DAN = {"user_id": "dan"}


def _header(pkg, name):
    schema = json.loads((PACKAGES / pkg / "schemas" / f"{name}.json").read_text())
    return "\t".join(f["name"] for f in schema["fields"]) + "\n"


def setup_env(tmp_path, monkeypatch, *, settings=(), with_tasks=True):
    data_dir = tmp_path / "data"
    schema_dir = data_dir / "schemas"
    schema_dir.mkdir(parents=True)
    wanted = [("app-banking", "value_accounts"), ("app-banking", "bank_import_profiles"),
              ("app-banking", "bank_statement_imports"), ("app-banking", "bank_lines"),
              ("app-finance", "fin_accounts"), ("app-settings", "app_settings"),
              ("app-payments", "payments"), ("app-invoices", "invoices")]
    if with_tasks:
        wanted.append(("app-tasks", "tasks"))
    for pkg, name in wanted:
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
        "value_accounts", {"id": ACCOUNT, "name": "Checking", "fin_account_id": "acct-cash",
                          "owner_id": "dan"}, base_dir=data_dir)

    rows = _header("app-settings", "app_settings")
    for i, (k, v) in enumerate(settings):
        rows += f"s{i}\t{k}\t{v}\t\n"
    (data_dir / "collections" / "app_settings" / "records.tsv").write_text(rows)
    return data_dir


def make_line(data_dir, lid, *, amount, on="2026-07-01", desc="", status="unmatched",
              matched_to="", account=ACCOUNT, **extra):
    rec = {"id": lid, "bank_account_id": account, "posted_on": on,
           "amount_cents": str(amount), "description": desc, "raw": f"raw:{desc}",
           "line_hash": f"h-{lid}", "match_status": status, "matched_to": matched_to,
           "owner_id": "dan"}
    rec.update(extra)
    return object_records.create_collection_record("bank_lines", rec, base_dir=data_dir)


def make_invoice(data_dir, iid="inv1"):
    return object_records.create_collection_record(
        "invoices", {"id": iid, "number": "N-1", "customer_name": "Acme",
                     "status": "sent", "total_cents": "500000", "owner_id": "dan"},
        base_dir=data_dir)


def make_payment(data_dir, pid, *, cents, on="2026-07-01", status="received",
                  invoice="inv1", reference=""):
    return object_records.create_collection_record(
        "payments",
        {"id": pid, "invoice_id": invoice, "amount_cents": str(cents), "method": "card",
         "received_on": on, "reference": reference, "status": status, "owner_id": "dan"},
        base_dir=data_dir)


def run_object(object_id, payload):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            object_id, method="POST", payload={"_identity": DAN, **payload}),
        roots=[BANKING_OBJECTS]).result


def tasks(data_dir):
    return object_records.read_collection_records("tasks", base_dir=data_dir)


def markers(data_dir):
    out = []
    for t in tasks(data_dir):
        meta = json.loads(t["metadata"]) if t.get("metadata") else {}
        out.append(meta.get("generated_from"))
    return out


TODAY = "2026-07-20"   # 19 days after the fixtures' default posted/received date


# --- direction 1: bank lines the books cannot explain -------------------------

def test_stale_unmatched_line_escalates_once(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_line(data_dir, "L1", amount=150000, on="2026-07-01", desc="ACME CORP DEPOSIT")

    result = run_object("system_bank_escalation", {"today": TODAY})
    assert result["ok"] is True
    assert result["scanned"] == 1
    assert result["escalated"] == 1
    assert result["already_open"] == 0
    assert len(tasks(data_dir)) == 1
    assert markers(data_dir) == ["bank_lines/L1"]

    # A second run must not create a second task for the same line.
    result2 = run_object("system_bank_escalation", {"today": TODAY})
    assert result2["escalated"] == 0
    assert result2["already_open"] == 1
    assert len(tasks(data_dir)) == 1


def test_recent_line_does_not_escalate(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_line(data_dir, "L1", amount=150000, on="2026-07-18", desc="DEPOSIT")  # 2 days old

    result = run_object("system_bank_escalation", {"today": TODAY})
    assert result["scanned"] == 1
    assert result["escalated"] == 0
    assert tasks(data_dir) == []


def test_matched_or_resolved_line_never_escalates(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_line(data_dir, "L1", amount=150000, on="2026-07-01", desc="DEPOSIT",
              status="matched", matched_to="payments/p9")
    make_line(data_dir, "L2", amount=-2500, on="2026-07-01", desc="FEE",
              status="resolved")

    result = run_object("system_bank_escalation", {"today": TODAY})
    assert result["scanned"] == 0
    assert result["escalated"] == 0
    assert tasks(data_dir) == []


def test_escalate_after_days_is_honoured(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("reconcile.escalate_after_days", "30"),))
    make_line(data_dir, "L1", amount=150000, on="2026-07-01", desc="DEPOSIT")  # 19 days old

    result = run_object("system_bank_escalation", {"today": TODAY})
    assert result["after_days"] == 30
    assert result["escalated"] == 0
    assert tasks(data_dir) == []

    result = run_object("system_bank_escalation", {"today": "2026-08-05"})  # 35 days old
    assert result["escalated"] == 1


def test_escalation_can_be_disabled(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("reconcile.escalation_enabled", "false"),))
    make_line(data_dir, "L1", amount=150000, on="2026-07-01", desc="DEPOSIT")

    result = run_object("system_bank_escalation", {"today": TODAY})
    assert result["ok"] is True
    assert "skipped" in result
    assert tasks(data_dir) == []


# --- direction 2: book records the bank never confirmed -----------------------

def test_unconfirmed_payment_escalates(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_invoice(data_dir)
    make_payment(data_dir, "p1", cents=150000, on="2026-07-01", reference="INV-1001")

    result = run_object("system_bank_escalation", {"today": TODAY})
    assert result["scanned"] == 1
    assert result["escalated"] == 1
    assert markers(data_dir) == ["payments/p1"]

    result2 = run_object("system_bank_escalation", {"today": TODAY})
    assert result2["escalated"] == 0
    assert result2["already_open"] == 1
    assert len(tasks(data_dir)) == 1


def test_payment_confirmed_by_a_bank_line_never_escalates(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_invoice(data_dir)
    make_payment(data_dir, "p1", cents=150000, on="2026-07-01", reference="INV-1001")
    make_line(data_dir, "L1", amount=150000, on="2026-07-01", desc="PAYMENT INV-1001",
              status="matched", matched_to="payments/p1")

    result = run_object("system_bank_escalation", {"today": TODAY})
    assert result["escalated"] == 0
    assert tasks(data_dir) == []


def test_bounced_payment_never_escalates(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_invoice(data_dir)
    make_payment(data_dir, "p1", cents=150000, on="2026-07-01", status="bounced")

    result = run_object("system_bank_escalation", {"today": TODAY})
    assert result["escalated"] == 0
    assert tasks(data_dir) == []


# --- graceful degradation ------------------------------------------------------

def test_missing_tasks_collection_is_skipped_not_fatal(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch, with_tasks=False)
    make_line(data_dir, "L1", amount=150000, on="2026-07-01", desc="DEPOSIT")

    result = run_object("system_bank_escalation", {"today": TODAY})
    assert result["ok"] is True
    assert "skipped" in result


def test_escalation_is_a_no_op_without_banking_data(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    result = run_object("system_bank_escalation", {"today": TODAY})
    assert result["ok"] is True
    assert result["scanned"] == 0
    assert result["escalated"] == 0


# --- the task itself is actionable without opening the record -----------------

def test_escalated_task_names_amount_date_and_description(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_line(data_dir, "L1", amount=150000, on="2026-07-01", desc="ACME CORP DEPOSIT")

    run_object("system_bank_escalation", {"today": TODAY})
    task = tasks(data_dir)[0]
    assert "150000" in task["title"] and "150000" in task["description"]
    assert "2026-07-01" in task["title"] and "2026-07-01" in task["description"]
    assert "ACME CORP DEPOSIT" in task["title"]
    assert task["owner_id"] == "dan"
    assert task["urgency"] == "high"


def test_escalated_payment_task_names_amount_date_and_invoice(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_invoice(data_dir)
    make_payment(data_dir, "p1", cents=250000, on="2026-07-01", reference="REF-9")

    run_object("system_bank_escalation", {"today": TODAY})
    task = tasks(data_dir)[0]
    assert "250000" in task["title"] and "250000" in task["description"]
    assert "2026-07-01" in task["title"] and "2026-07-01" in task["description"]
    assert "inv1" in task["title"]
    assert task["urgency"] == "critical"


def test_escalate_to_setting_overrides_the_record_owner(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("reconcile.escalate_to", "controller"),))
    make_line(data_dir, "L1", amount=150000, on="2026-07-01", desc="DEPOSIT")

    run_object("system_bank_escalation", {"today": TODAY})
    assert tasks(data_dir)[0]["owner_id"] == "controller"


def test_bank_account_filter_scopes_the_scan(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    object_records.create_collection_record(
        "value_accounts", {"id": "bank-2", "name": "Savings", "fin_account_id": "acct-cash",
                          "owner_id": "dan"}, base_dir=data_dir)
    make_line(data_dir, "L1", amount=150000, on="2026-07-01", desc="A", account=ACCOUNT)
    make_line(data_dir, "L2", amount=90000, on="2026-07-01", desc="B", account="bank-2")

    result = run_object("system_bank_escalation", {"today": TODAY, "bank_account_id": ACCOUNT})
    assert result["scanned"] == 1
    assert result["escalated"] == 1
    assert markers(data_dir) == ["bank_lines/L1"]
