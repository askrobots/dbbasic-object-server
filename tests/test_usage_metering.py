"""Metering: measure, fold, rate at close.

The three failures this design exists to prevent, each with a test:
a retried request billed twice; an allowance that never applies because
each event was priced as it landed; and a bill a customer can dispute but
not check.
"""

import json
import pathlib

from conftest import stage_collection

import object_billing
import object_execution
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
BILLING_OBJECTS = PACKAGES / "app-billing" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

PRICES = json.dumps({"ai.tokens": {"included": 100000, "unit_minor": 2}})


def setup_env(tmp_path, monkeypatch, *, prices=PRICES, base_minor="4900"):
    data_dir = tmp_path / "data"
    for pkg, name in (("app-billing", "billing_plans"), ("app-billing", "subscriptions"),
                      ("app-billing", "usage_events"), ("app-billing", "usage_summaries"),
                      ("app-billing", "wallets"), ("app-billing", "wallet_entries"),
                      ("app-invoices", "invoices"), ("app-invoices", "invoice_lines"),
                      ("app-settings", "app_settings"), ("app-finance", "denominations")):
        stage_collection(data_dir, pkg, name, seed=(name == "denominations"))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    object_records.create_collection_record(
        "billing_plans",
        {"id": "plan-ai", "name": "AI Team", "code": "ai", "period": "month",
         "base_minor": base_minor, "prices": prices, "is_active": "true",
         "owner_id": "dan"}, base_dir=data_dir)
    object_records.create_collection_record(
        "subscriptions",
        {"id": "sub-1", "customer_name": "Acme", "plan_id": "plan-ai",
         "billing_mode": "hybrid", "status": "active",
         "current_period_start": "2026-06-01", "current_period_end": "2026-06-30",
         "owner_id": "dan"}, base_dir=data_dir)
    return data_dir


def meter(data_dir, event_id, quantity, *, metric="ai.tokens", cost=0, eid=None):
    return object_records.create_collection_record(
        "usage_events",
        {"id": eid or f"ue-{event_id}", "event_id": event_id, "subscription_id": "sub-1",
         "metric": metric, "quantity": str(quantity), "unit": "tokens",
         "cost_minor": str(cost), "occurred_at": "2026-06-15", "owner_id": "dan"},
        base_dir=data_dir)


def run(object_id, payload=None):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            object_id, method="POST", payload=payload or {}),
        roots=[BILLING_OBJECTS]).result


def hook(record):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "hook_usage_events", method="BEFORE_WRITE",
            payload={"action": "create", "collection": "usage_events", "record": record}),
        roots=[BILLING_OBJECTS]).result


def summaries(data_dir):
    return object_records.read_collection_records("usage_summaries", base_dir=data_dir)


def lines(data_dir):
    return object_records.read_collection_records("invoice_lines", base_dir=data_dir)


# --- pure rating ----------------------------------------------------------------

def test_the_allowance_applies_to_the_period_not_to_each_call():
    """Rating per event would charge the first call at retail and never
    reach the included quantity -- the classic metered-billing bug."""
    spec = {"included": 100000, "unit_minor": 2}
    assert object_billing.rate_metric(80000, spec)["amount_minor"] == 0
    assert object_billing.rate_metric(100000, spec)["amount_minor"] == 0
    # 20k over at 2 minor units each
    assert object_billing.rate_metric(120000, spec)["amount_minor"] == 40000


def test_tiers_price_bands_of_the_overage():
    spec = {"included": 100, "tiers": [{"upto": 1000, "unit_minor": 2},
                                       {"unit_minor": 1}]}
    # 1600 used, 100 included -> 1500 overage: 1000 @2 + 500 @1
    assert object_billing.rate_metric(1600, spec)["amount_minor"] == 2500


def test_rounding_happens_once_for_the_metric():
    spec = {"included": 0, "unit_minor": "0.4"}
    # 5 * 0.4 = 2.0 exactly; 6 * 0.4 = 2.4 -> 2 (half-up on the total, not
    # per unit, which would have rounded 0.4 to 0 six times and billed 0)
    assert object_billing.rate_metric(5, spec)["amount_minor"] == 2
    assert object_billing.rate_metric(6, spec)["amount_minor"] == 2


def test_a_metric_with_no_price_is_reported_not_silently_billed():
    rated = object_billing.rate_period(
        [{"metric": "mystery", "quantity": "500"}], {"ai.tokens": {"unit_minor": 1}})
    assert rated["total_minor"] == 0
    assert rated["unpriced"] == ["mystery"]


def test_unparseable_plan_prices_yield_no_charges_not_a_crash():
    """A malformed plan must fail as 'nothing to charge', never as a
    billing run that dies halfway through the customer base."""
    assert object_billing.parse_prices("{not json") == {}
    assert object_billing.parse_prices(None) == {}


def test_margin_is_a_fold_over_stored_facts():
    m = object_billing.margin(revenue_minor=40000, cost_minor=15000)
    assert m["gross_minor"] == 25000 and m["gross_pct"] == 62.5


# --- the dedup gate --------------------------------------------------------------

def test_a_retried_request_is_not_billed_twice(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    meter(data_dir, "req-abc", 1000)
    repeat = hook({"event_id": "req-abc", "metric": "ai.tokens",
                   "quantity": "1000", "owner_id": "dan"})
    assert repeat["status"] == 409
    assert "already recorded" in repeat["error"]


def test_negative_usage_is_refused(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    verdict = hook({"event_id": "req-neg", "metric": "ai.tokens",
                    "quantity": "-5", "owner_id": "dan"})
    assert verdict["status"] == 400
    assert "correction is a credit" in verdict["error"]


# --- the fold ---------------------------------------------------------------------

def test_events_fold_into_one_bucket_per_metric_and_period(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    meter(data_dir, "e1", 40000, cost=300)
    meter(data_dir, "e2", 30000, cost=200)
    result = run("system_usage_rollup", {"today": "2026-06-20"})
    assert result["events_folded"] == 2 and result["summaries_written"] == 1
    bucket = summaries(data_dir)[0]
    assert bucket["quantity"] == "70000"
    assert bucket["event_count"] == "2"
    assert bucket["cost_minor"] == "500"        # margin is a read, not a project


def test_folding_twice_counts_nothing_twice(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    meter(data_dir, "e1", 40000)
    run("system_usage_rollup", {"today": "2026-06-20"})
    again = run("system_usage_rollup", {"today": "2026-06-20"})
    assert again["events_folded"] == 0
    assert summaries(data_dir)[0]["quantity"] == "40000"


def test_usage_with_no_subscription_stays_visible_and_unrated(tmp_path, monkeypatch):
    """Unpriceable usage must not be folded somewhere wrong."""
    data_dir = setup_env(tmp_path, monkeypatch)
    object_records.create_collection_record(
        "usage_events",
        {"id": "ue-orphan", "event_id": "orphan", "metric": "ai.tokens",
         "quantity": "500", "owner_id": "dan"}, base_dir=data_dir)
    result = run("system_usage_rollup", {"today": "2026-06-20"})
    assert result["orphaned_events"] == 1 and summaries(data_dir) == []


# --- rating into the invoice --------------------------------------------------------

def test_overage_becomes_a_line_a_customer_can_check(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    meter(data_dir, "e1", 120000, cost=1200)       # 20k over the 100k allowance
    run("system_usage_rollup", {"today": "2026-06-20"})
    result = run("system_billing_runner", {"today": "2026-07-01"})
    assert result["invoiced"] == 1

    invoice = object_records.read_collection_records("invoices", base_dir=data_dir)[0]
    assert invoice["total_cents"] == "44900"       # 4900 base + 40000 overage

    rows = sorted(lines(data_dir), key=lambda r: int(r["line_total_cents"]))
    assert len(rows) == 2
    usage_line = rows[-1]
    # The line shows used / included / billable, not one opaque number.
    assert "120000 used" in usage_line["description"]
    assert "100000 included" in usage_line["description"]
    assert "20000 billable" in usage_line["description"]
    assert usage_line["line_total_cents"] == "40000"


def test_usage_within_the_allowance_bills_only_the_base(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    meter(data_dir, "e1", 50000)
    run("system_usage_rollup", {"today": "2026-06-20"})
    run("system_billing_runner", {"today": "2026-07-01"})
    invoice = object_records.read_collection_records("invoices", base_dir=data_dir)[0]
    assert invoice["total_cents"] == "4900"
    assert len(lines(data_dir)) == 1


def test_a_summary_is_never_billed_in_two_periods(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    meter(data_dir, "e1", 120000)
    run("system_usage_rollup", {"today": "2026-06-20"})
    run("system_billing_runner", {"today": "2026-07-01"})
    assert summaries(data_dir)[0]["invoiced"] == "true"
    # Next period closes with no new usage: the base only, and the overage
    # already billed does not ride along a second time.
    run("system_billing_runner", {"today": "2026-08-01"})
    totals = sorted(int(i["total_cents"]) for i in
                    object_records.read_collection_records("invoices", base_dir=data_dir))
    assert totals == [4900, 44900]


def test_pure_metered_plan_with_no_base_still_bills_usage(tmp_path, monkeypatch):
    """base 0 + usage is a real shape: pay-as-you-go with an invoice."""
    data_dir = setup_env(tmp_path, monkeypatch, base_minor="0")
    meter(data_dir, "e1", 150000)
    run("system_usage_rollup", {"today": "2026-06-20"})
    result = run("system_billing_runner", {"today": "2026-07-01"})
    assert result["invoiced"] == 1
    invoice = object_records.read_collection_records("invoices", base_dir=data_dir)[0]
    assert invoice["total_cents"] == "100000"      # 50k over at 2
    assert len(lines(data_dir)) == 1
