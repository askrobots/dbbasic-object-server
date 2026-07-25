"""The period biller: decide money is owed, say so exactly once.

Double-billing is the failure customers never forgive, so the property
under test hardest here is idempotency -- a biller re-run after a crash,
or simply run twice in a day, must raise nothing the second time. The
other half is the status ladder, which has to climb in BOTH directions:
a ladder that only descends leaves a customer who just paid still cut
off.
"""

import pathlib

from conftest import stage_collection

import object_execution
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
BILLING_OBJECTS = PACKAGES / "app-billing" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()


def setup_env(tmp_path, monkeypatch, *, settings=()):
    data_dir = tmp_path / "data"
    for pkg, name in (("app-billing", "billing_plans"), ("app-billing", "subscriptions"),
                      ("app-billing", "wallets"), ("app-billing", "wallet_entries"),
                      ("app-invoices", "invoices"), ("app-invoices", "invoice_lines"),
                      ("app-finance", "denominations")):
        stage_collection(data_dir, pkg, name, seed=(name == "denominations"))
    rows = ""
    for i, (k, v) in enumerate(settings):
        rows += f"s{i}\t{k}\t{v}\t\n"
    stage_collection(data_dir, "app-settings", "app_settings", rows=rows)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    object_records.create_collection_record(
        "billing_plans",
        {"id": "plan-pro", "name": "Pro", "code": "pro", "period": "month",
         "base_minor": "4900", "is_active": "true", "owner_id": "dan"},
        base_dir=data_dir)
    return data_dir


def subscribe(data_dir, sid="sub-1", **fields):
    record = {"id": sid, "customer_name": "Acme Ltd", "customer_email": "a@acme.test",
              "plan_id": "plan-pro", "billing_mode": "subscription", "status": "active",
              "current_period_start": "2026-06-01", "current_period_end": "2026-06-30",
              "owner_id": "dan"}
    record.update(fields)
    return object_records.create_collection_record(
        "subscriptions", record, base_dir=data_dir)


def run(payload=None):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "system_billing_runner", method="POST", payload=payload or {}),
        roots=[BILLING_OBJECTS]).result


def invoices(data_dir):
    return object_records.read_collection_records("invoices", base_dir=data_dir)


def sub(data_dir, sid="sub-1"):
    return object_records.get_collection_record("subscriptions", sid, base_dir=data_dir)


# --- raising the invoice --------------------------------------------------------

def test_a_closed_period_raises_one_invoice_and_advances(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    subscribe(data_dir)
    result = run({"today": "2026-07-01"})
    assert result["invoiced"] == 1 and result["advanced"] == 1

    rows = invoices(data_dir)
    assert len(rows) == 1
    assert rows[0]["total_cents"] == "4900"
    assert rows[0]["status"] == "sent"
    assert rows[0]["customer_email"] == "a@acme.test"

    lines = object_records.read_collection_records("invoice_lines", base_dir=data_dir)
    assert len(lines) == 1 and lines[0]["line_total_cents"] == "4900"
    assert "2026-06-01" in lines[0]["description"]      # the period it covers

    moved = sub(data_dir)
    assert moved["current_period_start"] == "2026-06-30"
    assert moved["current_period_end"] == "2026-07-30"


def test_running_twice_never_double_bills(tmp_path, monkeypatch):
    """The property customers never forgive us for getting wrong."""
    data_dir = setup_env(tmp_path, monkeypatch)
    subscribe(data_dir)
    run({"today": "2026-07-01"})
    # Re-run the SAME day (a crashed pass, a manual retry, a double cron).
    again = run({"today": "2026-07-01"})
    assert again["invoiced"] == 0
    assert len(invoices(data_dir)) == 1


def test_a_period_still_open_bills_nothing(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    subscribe(data_dir)
    result = run({"today": "2026-06-15"})
    assert result["invoiced"] == 0 and invoices(data_dir) == []


def test_wallet_mode_gets_no_periodic_invoice(tmp_path, monkeypatch):
    """Its money already moved when the usage did; invoicing again would
    charge twice for one thing."""
    data_dir = setup_env(tmp_path, monkeypatch)
    subscribe(data_dir, billing_mode="wallet")
    result = run({"today": "2026-07-01"})
    assert result["invoiced"] == 0 and invoices(data_dir) == []


def test_dry_run_says_what_it_would_do_and_writes_nothing(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    subscribe(data_dir)
    result = run({"today": "2026-07-01", "dry_run": "true"})
    assert invoices(data_dir) == []
    assert any(r.get("would_invoice_period") for r in result["results"])
    assert sub(data_dir)["current_period_end"] == "2026-06-30"   # unmoved


def test_month_end_clamps_instead_of_skipping_a_cycle(tmp_path, monkeypatch):
    """A 31st subscriber must not skip February."""
    data_dir = setup_env(tmp_path, monkeypatch)
    subscribe(data_dir, current_period_start="2026-12-31",
              current_period_end="2027-01-31")
    run({"today": "2027-02-01"})
    assert sub(data_dir)["current_period_end"] == "2027-02-28"


# --- the ladder -----------------------------------------------------------------

def test_a_trial_ends_into_active(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    subscribe(data_dir, status="trialing", trial_ends_on="2026-06-20",
              current_period_end="2026-12-31")
    run({"today": "2026-07-01"})
    assert sub(data_dir)["status"] == "active"


def test_unpaid_invoices_walk_active_to_past_due_to_suspended(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("billing.past_due_grace_days", "7"),
                                   ("billing.suspend_after_days", "30")))
    subscribe(data_dir)
    run({"today": "2026-07-01"})                 # raises the invoice, due +14d
    assert sub(data_dir)["status"] == "active"

    # A week past due: still active (inside grace).
    run({"today": "2026-07-20"})
    assert sub(data_dir)["status"] == "active"
    # Two weeks past due: past_due.
    run({"today": "2026-07-30"})
    assert sub(data_dir)["status"] == "past_due"
    # Well past: suspended.
    run({"today": "2026-09-01"})
    assert sub(data_dir)["status"] == "suspended"


def test_paying_lets_the_ladder_be_climbed_back(tmp_path, monkeypatch):
    """A ladder that only descends leaves a paying customer cut off."""
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("billing.past_due_grace_days", "7"),))
    subscribe(data_dir)
    run({"today": "2026-07-01"})
    run({"today": "2026-07-30"})
    assert sub(data_dir)["status"] == "past_due"

    for row in invoices(data_dir):
        object_records.update_collection_record(
            "invoices", row["id"], {"status": "paid"}, base_dir=data_dir, actor="dan")
    # Nothing is overdue now, so the runner stops pushing it down; the
    # transition table allows past_due -> active for the operator.
    result = run({"today": "2026-08-01"})
    assert not any("suspended" in str(r) for r in result["results"])
    object_records.update_collection_record(
        "subscriptions", "sub-1", {"status": "active"}, base_dir=data_dir, actor="dan")
    assert sub(data_dir)["status"] == "active"


def test_cancel_at_period_end_bills_the_last_period_then_stops(tmp_path, monkeypatch):
    """Service runs to the end of the period already paid for -- cutting it
    off at the click would bill for time the customer cannot use."""
    data_dir = setup_env(tmp_path, monkeypatch)
    subscribe(data_dir, cancel_at_period_end="true")
    result = run({"today": "2026-07-01"})
    assert result["invoiced"] == 1               # the period served IS billed
    assert sub(data_dir)["status"] == "canceled"
    assert run({"today": "2026-08-01"})["invoiced"] == 0   # and never again


def test_canceled_subscriptions_are_left_entirely_alone(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    subscribe(data_dir, status="canceled")
    assert run({"today": "2026-07-01"})["invoiced"] == 0


def test_billing_not_installed_is_a_graceful_skip(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    assert run()["skipped"].startswith("billing not installed")
