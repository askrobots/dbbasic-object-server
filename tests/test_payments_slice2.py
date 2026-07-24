"""Payments slice 2 (plan/payments-spec.md): aging + dunning.

Doctrinal split under test: paid/partial flips are EVENT-driven
(system_invoice_status reacts the moment money moves); overdue + dunning
escalation are TIME-driven (system_invoice_aging, a daemon-scheduled pass —
docs/logic-decisions.md #2). Dunning emails go straight to the generic
outbox (customer_email is a raw address, not a user id); the owner's in_app
ping is a seeded notify_rule (pure data).
"""

import json
import pathlib

import object_execution
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
RUNTIME = python_object_runtime.PythonObjectRuntime()


def setup_env(tmp_path, monkeypatch, *, settings=()):
    data_dir = tmp_path / "data"
    schema_dir = data_dir / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    for pkg, name in (("app-invoices", "invoices"), ("app-payments", "payments"),
                      ("app-payments", "refunds"), ("app-settings", "app_settings"),
                      ("app-email", "email_outbox")):
        (schema_dir / f"{name}.json").write_text(
            (PACKAGES / pkg / "schemas" / f"{name}.json").read_text())

    def coll(name, header):
        d = data_dir / "collections" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "records.tsv").write_text(header)

    coll("invoices",
         "id\tnumber\tcustomer_name\tcustomer_email\tstatus\tissue_date\tdue_date"
         "\ttotal_cents\tpayments_received_cents\trefunded_cents\tamount_paid_cents"
         "\tbalance_due_cents\tdunning_level\tlast_dunned_on\towner_id\n")
    coll("payments",
         "id\tinvoice_id\tamount_cents\tmethod\treceived_on\treference\tnotes"
         "\tstatus\trefunded_cents\towner_id\tcreated_at\n")
    coll("refunds",
         "id\tpayment_id\tinvoice_id\tamount_cents\treason\trefunded_on\towner_id\tcreated_at\n")
    coll("email_outbox",
         "id\tto\tfrom_addr\treply_to\tsubject\ttext_body\thtml_body\tstatus"
         "\tattempts\tmax_attempts\tlast_error\tnext_attempt_at\tcreated_at"
         "\tupdated_at\tsent_at\tsource_object_id\textra\n")
    rows = "id\tkey\tvalue\tdescription\n"
    for i, (k, v) in enumerate(settings):
        rows += f"s{i}\t{k}\t{v}\t\n"
    coll("app_settings", rows)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    return data_dir


def make_invoice(data_dir, iid="inv1", status="sent", total="10000",
                 due="2026-07-10", email="grace@acme.test", **extra):
    rec = {"id": iid, "number": f"N-{iid}", "customer_name": "Acme",
           "customer_email": email, "status": status, "due_date": due,
           "total_cents": total, "owner_id": "dan"}
    rec.update(extra)
    return object_records.create_collection_record("invoices", rec, base_dir=data_dir)


def pay(data_dir, pid, invoice_id, cents, status="received"):
    return object_records.create_collection_record(
        "payments",
        {"id": pid, "invoice_id": invoice_id, "amount_cents": cents,
         "method": "card", "received_on": "2026-07-09", "status": status,
         "owner_id": "dan"},
        base_dir=data_dir)


def fire_status(collection, record_id, action_raw):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "system_invoice_status", method="EVENT",
            payload={"event": f"{collection}.record.{action_raw}d",
                     "collection": collection, "record_id": record_id,
                     "action": action_raw}),
        roots=[PACKAGES / "app-payments" / "objects"])


def run_aging(today):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "system_invoice_aging", method="POST", payload={"today": today}),
        roots=[PACKAGES / "app-invoices" / "objects"])


def invoice(data_dir, iid="inv1"):
    return object_records.get_collection_record("invoices", iid, base_dir=data_dir)


def outbox(data_dir):
    return object_records.read_collection_records("email_outbox", base_dir=data_dir)


def test_status_flips_partial_then_paid_then_back_on_refund(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_invoice(data_dir)
    pay(data_dir, "p1", "inv1", "4000")
    fire_status("payments", "p1", "create")
    assert invoice(data_dir)["status"] == "partial"

    pay(data_dir, "p2", "inv1", "6000")
    fire_status("payments", "p2", "create")
    assert invoice(data_dir)["status"] == "paid"

    object_records.create_collection_record(
        "refunds", {"id": "r1", "payment_id": "p2", "invoice_id": "inv1",
                    "amount_cents": "2000", "refunded_on": "2026-07-11",
                    "owner_id": "dan"}, base_dir=data_dir)
    fire_status("refunds", "r1", "create")
    assert invoice(data_dir)["status"] == "partial"


def test_status_never_touches_draft_or_void(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_invoice(data_dir, iid="invd", status="draft")
    pay(data_dir, "p1", "invd", "10000")
    fire_status("payments", "p1", "create")
    assert invoice(data_dir, "invd")["status"] == "draft"


def test_aging_flips_overdue_dunns_and_respects_grace(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("payments.grace_days", "3"),))
    make_invoice(data_dir)  # due 2026-07-10, unpaid

    # Inside grace: due+3 = 07-13; today 07-12 -> untouched.
    run_aging("2026-07-12")
    assert invoice(data_dir)["status"] == "sent"
    assert outbox(data_dir) == []

    # Past grace -> overdue, level 1, dunning email queued to the customer.
    result = run_aging("2026-07-14")
    assert result.result["flipped_overdue"] == 1
    row = invoice(data_dir)
    assert row["status"] == "overdue"
    assert row["dunning_level"] == "1"
    assert row["last_dunned_on"] == "2026-07-14"
    mails = outbox(data_dir)
    assert len(mails) == 1
    assert mails[0]["to"] == "grace@acme.test"
    assert "overdue" in mails[0]["subject"]
    assert mails[0]["status"] == "queued"


def test_dunning_escalates_on_schedule_and_stops_at_max(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("payments.dunning_repeat_days", "7"),
                                   ("payments.dunning_max_level", "3")))
    make_invoice(data_dir)
    run_aging("2026-07-11")                       # -> overdue, level 1
    run_aging("2026-07-12")                       # too soon: no escalation
    assert invoice(data_dir)["dunning_level"] == "1"
    run_aging("2026-07-18")                       # +7d -> level 2
    assert invoice(data_dir)["dunning_level"] == "2"
    run_aging("2026-07-25")                       # +7d -> level 3 (max)
    assert invoice(data_dir)["dunning_level"] == "3"
    run_aging("2026-08-10")                       # capped: stays 3
    assert invoice(data_dir)["dunning_level"] == "3"
    assert len(outbox(data_dir)) == 3             # one email per level


def test_aging_clears_overdue_when_balance_paid_out_of_band(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_invoice(data_dir)
    run_aging("2026-07-11")
    assert invoice(data_dir)["status"] == "overdue"
    pay(data_dir, "p1", "inv1", "10000")           # storage-level: no event fired
    result = run_aging("2026-07-12")
    assert invoice(data_dir)["status"] == "paid"
    assert result.result["flipped_overdue"] == 0


def test_aging_skips_paid_void_and_no_due_date(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_invoice(data_dir, iid="a", status="paid")
    make_invoice(data_dir, iid="b", status="void")
    make_invoice(data_dir, iid="c", status="sent", due="")
    run_aging("2026-08-01")
    assert invoice(data_dir, "a")["status"] == "paid"
    assert invoice(data_dir, "b")["status"] == "void"
    assert invoice(data_dir, "c")["status"] == "sent"
    assert outbox(data_dir) == []


def test_dunning_without_customer_email_still_escalates(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_invoice(data_dir, email="")
    result = run_aging("2026-07-11")
    assert invoice(data_dir)["status"] == "overdue"
    assert outbox(data_dir) == []
    assert result.result["results"][0]["emailed"] is False


def test_owner_notify_rule_seed_matches_the_overdue_change(tmp_path, monkeypatch):
    """The dunning notify seed is pure data — prove it fires with the real
    notify engine against the change our aging pass produces."""
    import object_notify
    data_dir = setup_env(tmp_path, monkeypatch)
    make_invoice(data_dir)
    run_aging("2026-07-11")
    row = invoice(data_dir)

    seed = (PACKAGES / "app-invoices" / "seed" / "notify_rules.tsv").read_text().splitlines()
    header = seed[0].split("\t")
    import csv, io
    parsed = next(csv.DictReader(io.StringIO("\n".join(seed)), delimiter="\t"))
    rule = dict(parsed)
    # Mirror a REAL record-change entry: changed_fields present (the notify
    # engine's transition semantics — a match condition must be on a field the
    # write actually changed, so the rule fires once per transition, not on
    # every later edit of an overdue invoice).
    change = {"collection": "invoices", "record_id": "inv1",
              "action": "update", "after": row, "actor": "system_invoice_aging",
              "changed_fields": ["status", "dunning_level", "last_dunned_on"]}
    notes = object_notify.notifications_for_change(rule, change, base_dir=data_dir)
    assert len(notes) == 1
    assert notes[0]["user_id"] == "dan"
    assert "overdue" in notes[0]["body"]
