"""Time and materials: hours -> approved -> invoiced.

Consulting hours are a usage metric with an approval gate, so most of
this spine is already proven elsewhere. What is genuinely new -- and
therefore what is tested hardest here -- is the gate itself: nobody
approves their own hours, nothing is submitted while the clock is still
running, the rate is fixed at the moment a human said yes, and the same
hour is never billed twice.
"""

import pathlib

from conftest import stage_collection

import object_execution
import object_rates
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
TIMER_OBJECTS = PACKAGES / "app-timers" / "objects"
INVOICE_OBJECTS = PACKAGES / "app-invoices" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

HOUR = 3600


def setup_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    for pkg, name in (("app-timers", "time_logs"), ("app-timers", "rate_cards"),
                      ("app-invoices", "invoices"), ("app-invoices", "invoice_lines"),
                      ("app-tasks", "tasks"), ("app-projects", "projects"),
                      ("app-settings", "app_settings")):
        stage_collection(data_dir, pkg, name)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    object_records.create_collection_record(
        "projects", {"id": "proj-acme", "name": "Acme Rebuild", "owner_id": "boss"},
        base_dir=data_dir)
    object_records.create_collection_record(
        "tasks", {"id": "task-1", "title": "Data migration", "owner_id": "dana"},
        base_dir=data_dir)
    return data_dir


def rate_card(data_dir, card_id, cents, **fields):
    record = {"id": card_id, "label": card_id, "hourly_rate_cents": str(cents),
              "currency": "USD", "valid_from": "2026-01-01", "is_active": "true",
              "owner_id": "boss"}
    record.update({k: str(v) for k, v in fields.items()})
    return object_records.create_collection_record(
        "rate_cards", record, base_dir=data_dir)


def log_time(data_dir, log_id, *, seconds=HOUR, owner="dana", **fields):
    record = {"id": log_id, "task_id": "task-1", "project_id": "proj-acme",
              "started_at": "2026-06-15T09:00:00Z", "ended_at": "2026-06-15T10:00:00Z",
              "is_running": "false", "billable": "true", "status": "draft",
              "duration_seconds": str(seconds), "owner_id": owner,
              "notes": "Migration work"}
    record.update({k: str(v) for k, v in fields.items()})
    return object_records.create_collection_record(
        "time_logs", record, base_dir=data_dir)


def hook(data_dir, record, *, existing=None, changes=None, actor="boss",
         action="update"):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "hook_time_logs", method="BEFORE_WRITE",
            payload={"action": action, "collection": "time_logs",
                     "record": record, "existing": existing,
                     "changes": changes if changes is not None else dict(record),
                     "subject": {"user_id": actor, "roles": ["manager"]}}),
        roots=[TIMER_OBJECTS]).result


def approve(data_dir, log_id, *, actor="boss"):
    """Move an entry through the gate the way the write path would."""
    existing = object_records.get_collection_record("time_logs", log_id,
                                                    base_dir=data_dir)
    outcome = hook(data_dir, {"status": "approved"}, existing=existing,
                   changes={"status": "approved"}, actor=actor)
    if outcome and outcome.get("error"):
        return outcome
    patch = (outcome or {}).get("record") or {"status": "approved"}
    object_records.update_collection_record("time_logs", log_id, patch,
                                            base_dir=data_dir, actor=actor)
    return {"ok": True}


def generate(payload=None):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "action_generate_tm_invoice", method="POST", payload=payload or {}),
        roots=[INVOICE_OBJECTS]).result


def entry(data_dir, log_id):
    return object_records.get_collection_record("time_logs", log_id, base_dir=data_dir)


def invoices(data_dir):
    return object_records.read_collection_records("invoices", base_dir=data_dir)


def lines(data_dir):
    return object_records.read_collection_records("invoice_lines", base_dir=data_dir)


# --- resolving the rate -----------------------------------------------------

def test_the_most_specific_card_wins():
    """The reason anyone writes a rate for one person on one project is
    that it must beat both the project rate and their standard rate."""
    cards = [
        {"id": "house", "hourly_rate_cents": "10000", "valid_from": "2026-01-01"},
        {"id": "dana", "person_id": "dana", "hourly_rate_cents": "12000",
         "valid_from": "2026-01-01"},
        {"id": "acme", "project_id": "proj-acme", "hourly_rate_cents": "15000",
         "valid_from": "2026-01-01"},
        {"id": "dana_acme", "person_id": "dana", "project_id": "proj-acme",
         "hourly_rate_cents": "18000", "valid_from": "2026-01-01"},
    ]
    pick = lambda **kw: object_rates.find_rate(cards, on_date="2026-06-15", **kw)["id"]
    assert pick(person_id="dana", project_id="proj-acme") == "dana_acme"
    assert pick(person_id="sam", project_id="proj-acme") == "acme"
    assert pick(person_id="dana", project_id="proj-other") == "dana"
    assert pick(person_id="sam", project_id="proj-other") == "house"


def test_a_rate_never_looks_forward():
    """Next quarter's rate must not price last quarter's unbilled hours."""
    cards = [{"id": "old", "hourly_rate_cents": "10000", "valid_from": "2026-01-01"},
             {"id": "new", "hourly_rate_cents": "13000", "valid_from": "2026-07-01"}]
    assert object_rates.find_rate(cards, on_date="2026-06-15")["id"] == "old"
    assert object_rates.find_rate(cards, on_date="2026-07-02")["id"] == "new"


def test_a_retired_card_stops_applying_to_new_work():
    cards = [{"id": "old", "hourly_rate_cents": "10000", "valid_from": "2026-01-01",
              "is_active": "false"}]
    assert object_rates.find_rate(cards, on_date="2026-06-15") is None


def test_the_increment_rounds_up_because_that_is_what_an_increment_is():
    # 61 minutes at a 15-minute increment bills 75 minutes.
    assert object_rates.billable_seconds(61 * 60, 15) == 75 * 60
    assert object_rates.billable_seconds(HOUR, 15) == HOUR      # exact stays exact
    assert object_rates.billable_seconds(HOUR, 0) == HOUR       # 0 bills what happened


def test_the_amount_rounds_once_for_the_entry():
    # 20 minutes at $150/hr is exactly 5000; 25 minutes is 6250.
    assert object_rates.amount_cents(20 * 60, 15000) == 5000
    assert object_rates.amount_cents(25 * 60, 15000) == 6250
    # 1 second at $150/hr rounds half-up to 4 cents, not away to nothing.
    assert object_rates.amount_cents(1, 15000) == 4


def test_time_with_no_applicable_card_is_reported_not_priced_at_zero():
    """Hours nobody wrote a rate for must not look like unbillable hours."""
    rated = object_rates.rate_entry(
        {"owner_id": "dana", "project_id": "proj-acme", "duration_seconds": HOUR},
        [])
    assert rated["amount_cents"] == 0
    assert "no rate card applies" in rated["unrated_reason"]


# --- the gate ---------------------------------------------------------------

def test_nobody_approves_their_own_hours(tmp_path, monkeypatch):
    """An approval you can grant yourself is not an approval."""
    data_dir = setup_env(tmp_path, monkeypatch)
    rate_card(data_dir, "house", 15000)
    log_time(data_dir, "t1", status="submitted")
    outcome = approve(data_dir, "t1", actor="dana")     # dana logged it
    assert outcome["status"] == 403
    assert "not an approval" in outcome["error"]
    assert entry(data_dir, "t1")["status"] == "submitted"


def test_a_running_timer_cannot_be_submitted(tmp_path, monkeypatch):
    """Billing a duration that is still growing means the number invoiced
    was never the number approved."""
    data_dir = setup_env(tmp_path, monkeypatch)
    log_time(data_dir, "t1", is_running="true", ended_at="", duration_seconds=0)
    existing = entry(data_dir, "t1")
    outcome = hook(data_dir, {"status": "submitted"}, existing=existing,
                   changes={"status": "submitted"}, actor="dana")
    assert outcome["status"] == 400 and "Stop the timer" in outcome["error"]


def test_unbillable_time_is_not_approved_for_billing(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    rate_card(data_dir, "house", 15000)
    log_time(data_dir, "t1", status="submitted", billable="false")
    outcome = approve(data_dir, "t1")
    assert outcome["status"] == 400 and "billable" in outcome["error"]


def test_approving_without_a_rate_is_refused_rather_than_approved_at_nothing(
        tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    log_time(data_dir, "t1", status="submitted")
    outcome = approve(data_dir, "t1")
    assert outcome["status"] == 409 and "No rate applies" in outcome["error"]


def test_approval_stamps_the_rate_that_applied_when_the_work_happened(
        tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    rate_card(data_dir, "house", 15000)
    log_time(data_dir, "t1", status="submitted", seconds=90 * 60)
    assert approve(data_dir, "t1")["ok"]

    row = entry(data_dir, "t1")
    assert row["status"] == "approved"
    assert row["hourly_rate_cents"] == "15000"
    assert row["amount_cents"] == "22500"          # 1.5h at $150
    assert row["rate_card_id"] == "house"
    assert row["approved_by"] == "boss"
    assert row["approved_at"]


def test_a_later_rate_change_never_reprices_approved_work(tmp_path, monkeypatch):
    """The stamp is the whole point of stamping."""
    data_dir = setup_env(tmp_path, monkeypatch)
    rate_card(data_dir, "house", 15000)
    log_time(data_dir, "t1", status="submitted")
    approve(data_dir, "t1")

    rate_card(data_dir, "house_2027", 25000, valid_from="2026-07-01")
    generate({"project_id": "proj-acme", "through_date": "2026-12-31"})
    assert lines(data_dir)[0]["line_total_cents"] == "15000"


def test_approved_time_is_frozen(tmp_path, monkeypatch):
    """Correcting settled evidence is a compensating entry, not an edit."""
    data_dir = setup_env(tmp_path, monkeypatch)
    rate_card(data_dir, "house", 15000)
    log_time(data_dir, "t1", status="submitted")
    approve(data_dir, "t1")

    existing = entry(data_dir, "t1")
    outcome = hook(data_dir, {"duration_seconds": "72000"}, existing=existing,
                   changes={"duration_seconds": "72000"})
    assert outcome["status"] == 409 and "settled" in outcome["error"]


def test_billed_time_cannot_be_reopened(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    rate_card(data_dir, "house", 15000)
    log_time(data_dir, "t1", status="submitted")
    approve(data_dir, "t1")
    generate({"project_id": "proj-acme"})

    existing = entry(data_dir, "t1")
    outcome = hook(data_dir, {"status": "draft"}, existing=existing,
                   changes={"status": "draft"})
    assert outcome["status"] == 409 and "credit on the invoice" in outcome["error"]


# --- generating the invoice --------------------------------------------------

def test_approved_hours_become_an_invoice_a_client_can_check(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    rate_card(data_dir, "house", 15000)
    log_time(data_dir, "t1", status="submitted", seconds=2 * HOUR)
    log_time(data_dir, "t2", status="submitted", seconds=HOUR)
    approve(data_dir, "t1")
    approve(data_dir, "t2")

    result = generate({"project_id": "proj-acme"})
    assert result["invoiced"] == 1 and result["entries_billed"] == 2

    invoice = invoices(data_dir)[0]
    assert invoice["total_cents"] == "45000"
    assert invoice["customer_name"] == "Acme Rebuild"
    assert invoice["status"] == "draft"      # a human sends it

    rows = lines(data_dir)
    assert len(rows) == 2
    assert "2.00 hours" in rows[0]["description"]
    assert "at 150.00/hr" in rows[0]["description"]


def test_running_the_generator_twice_bills_nothing_twice(tmp_path, monkeypatch):
    """The failure a consultancy does not get to explain twice."""
    data_dir = setup_env(tmp_path, monkeypatch)
    rate_card(data_dir, "house", 15000)
    log_time(data_dir, "t1", status="submitted")
    approve(data_dir, "t1")

    generate({"project_id": "proj-acme"})
    again = generate({"project_id": "proj-acme"})
    assert again["invoiced"] == 0
    assert len(invoices(data_dir)) == 1
    assert entry(data_dir, "t1")["status"] == "billed"


def test_only_approved_time_is_billed(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    rate_card(data_dir, "house", 15000)
    log_time(data_dir, "t1", status="submitted")       # never approved
    log_time(data_dir, "t2", status="draft")
    assert generate({"project_id": "proj-acme"})["invoiced"] == 0


def test_a_through_date_leaves_later_work_for_the_next_invoice(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    rate_card(data_dir, "house", 15000)
    log_time(data_dir, "t1", status="submitted")
    log_time(data_dir, "t2", status="submitted",
             started_at="2026-07-20T09:00:00Z", ended_at="2026-07-20T10:00:00Z")
    approve(data_dir, "t1")
    approve(data_dir, "t2")

    result = generate({"project_id": "proj-acme", "through_date": "2026-06-30"})
    assert result["entries_billed"] == 1
    assert entry(data_dir, "t2")["status"] == "approved"    # still waiting


def test_grouping_by_person_collapses_the_lines(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    rate_card(data_dir, "house", 15000)
    log_time(data_dir, "t1", status="submitted", owner="dana")
    log_time(data_dir, "t2", status="submitted", owner="dana")
    log_time(data_dir, "t3", status="submitted", owner="sam")
    for log_id in ("t1", "t2", "t3"):
        approve(data_dir, log_id)

    generate({"project_id": "proj-acme", "grouping": "by_person"})
    rows = sorted(lines(data_dir), key=lambda r: r["description"])
    assert len(rows) == 2
    assert rows[0]["description"].startswith("dana: 2.00 hours")
    assert rows[0]["line_total_cents"] == "30000"


def test_a_dry_run_says_what_it_would_bill_and_writes_nothing(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    rate_card(data_dir, "house", 15000)
    log_time(data_dir, "t1", status="submitted")
    approve(data_dir, "t1")

    result = generate({"project_id": "proj-acme", "dry_run": "true"})
    assert result["would_invoice"] == 1 and result["total_cents"] == 15000
    assert invoices(data_dir) == []
    assert entry(data_dir, "t1")["status"] == "approved"


def test_time_tracking_absent_is_a_graceful_skip(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    assert generate()["skipped"].startswith("time tracking not installed")
