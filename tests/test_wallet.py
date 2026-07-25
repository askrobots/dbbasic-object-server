"""Wallets: the ledger IS the balance, and the cap is the safety.

Two claims under test. First, that a wallet's balance cannot drift from
its entries -- the predecessor's mutable balance column beside a
transaction table is the failure this design exists to prevent, and when
those two disagree the customer is right and you cannot prove otherwise.
Second, that auto top-up is bounded: autopay plus one runaway metering
bug is a four-figure surprise, and a monthly cap turns that into
something a human notices instead.
"""

import json
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
    for pkg, name in (("app-billing", "wallets"), ("app-billing", "wallet_entries"),
                      ("app-finance", "denominations"), ("app-settings", "app_settings")):
        stage_collection(data_dir, pkg, name,
                         seed=(name == "denominations"))
    rows = ""
    for i, (k, v) in enumerate(settings):
        rows += f"s{i}\t{k}\t{v}\t\n"
    if rows:
        stage_collection(data_dir, "app-settings", "app_settings", rows=rows)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.delenv("DBBASIC_STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("DBBASIC_STRIPE_WEBHOOK_SECRET", raising=False)
    return data_dir


def make_wallet(data_dir, wid="w1", **fields):
    record = {"id": wid, "owner_id": "dan", "is_active": "true"}
    record.update(fields)
    return object_records.create_collection_record("wallets", record, base_dir=data_dir)


def entry(data_dir, wid, amount, *, kind="topup", eid=None, generated_from="",
          created_at=None):
    record = {"id": eid or f"e-{abs(amount)}-{kind}-{object_records.new_record_id()}"
              if hasattr(object_records, "new_record_id") else (eid or f"e{amount}{kind}"),
              "wallet_id": wid, "amount_minor": str(amount), "kind": kind,
              "generated_from": generated_from, "owner_id": "dan"}
    if created_at:
        record["created_at"] = created_at
    return object_records.create_collection_record(
        "wallet_entries", record, base_dir=data_dir)


def wallet(data_dir, wid="w1"):
    return object_records.get_collection_record("wallets", wid, base_dir=data_dir)


def hook(record, action="create"):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "hook_wallet_entries", method="BEFORE_WRITE",
            payload={"action": action, "collection": "wallet_entries", "record": record}),
        roots=[BILLING_OBJECTS]).result


def replenish(payload=None):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "system_wallet_replenish", method="POST", payload=payload or {}),
        roots=[BILLING_OBJECTS]).result


# --- the ledger is the balance ------------------------------------------------

def test_balance_is_the_sum_of_entries_and_nothing_writes_it(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_wallet(data_dir)
    entry(data_dir, "w1", 5000, kind="topup", eid="e1")
    assert wallet(data_dir)["balance_minor"] == "5000"
    entry(data_dir, "w1", -1250, kind="debit", eid="e2")
    assert wallet(data_dir)["balance_minor"] == "3750"
    entry(data_dir, "w1", 1250, kind="refund", eid="e3")
    assert wallet(data_dir)["balance_minor"] == "5000"


def test_the_balance_field_cannot_be_written_by_anyone(tmp_path, monkeypatch):
    """The predecessor's drift bug is impossible here by construction: the
    field is computed, and the write path refuses it."""
    data_dir = setup_env(tmp_path, monkeypatch)
    make_wallet(data_dir)
    entry(data_dir, "w1", 1000, eid="e1")
    try:
        object_records.update_collection_record(
            "wallets", "w1", {"balance_minor": "999999"}, base_dir=data_dir)
        raise AssertionError("a computed balance must not be writable")
    except object_records.InvalidRecordPayloadError as exc:
        assert "computed" in str(exc) or "read-only" in str(exc)
    assert wallet(data_dir)["balance_minor"] == "1000"


# --- the debit gate -------------------------------------------------------------

def test_a_debit_beyond_the_balance_is_refused_with_402(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_wallet(data_dir)
    entry(data_dir, "w1", 1000, eid="e1")
    ok = hook({"wallet_id": "w1", "amount_minor": "-1000", "kind": "debit"})
    assert ok is None                      # exactly to zero is allowed
    over = hook({"wallet_id": "w1", "amount_minor": "-1001", "kind": "debit"})
    assert over["status"] == 402           # Payment Required, the honest code
    assert "Insufficient wallet balance" in over["error"]


def test_credits_are_never_gated(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_wallet(data_dir)
    assert hook({"wallet_id": "w1", "amount_minor": "5000", "kind": "topup"}) is None


def test_an_overdraft_floor_can_be_allowed_deliberately(tmp_path, monkeypatch):
    """A high-frequency workload may prefer a few cents of race over
    failing a customer's request mid-flight -- but it must be a decision."""
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("billing.wallet.overdraft_minor", "500"),))
    make_wallet(data_dir)
    entry(data_dir, "w1", 100, eid="e1")
    assert hook({"wallet_id": "w1", "amount_minor": "-600", "kind": "debit"}) is None
    assert hook({"wallet_id": "w1", "amount_minor": "-601",
                 "kind": "debit"})["status"] == 402


def test_a_zero_entry_is_refused(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    assert hook({"wallet_id": "w1", "amount_minor": "0", "kind": "debit"})["status"] == 400


# --- auto top-up, and the cap that bounds it ------------------------------------

def test_the_monthly_cap_stops_a_runaway(tmp_path, monkeypatch):
    """The scenario: metering goes wrong and drains the wallet repeatedly.
    Without a cap, autopay follows it down forever."""
    data_dir = setup_env(tmp_path, monkeypatch)
    make_wallet(data_dir, auto_replenish_enabled="true",
                auto_replenish_threshold_minor="1000",
                auto_replenish_amount_minor="2000",
                auto_replenish_monthly_cap_minor="3000",
                stripe_customer_id="cus_1", payment_method_ref="pm_1")
    # The real runaway shape: an auto top-up already happened this month,
    # then metering drained the wallet back below the threshold. Without a
    # cap the pass would top up again, and again, following the bug down.
    entry(data_dir, "w1", 2000, kind="auto_topup", eid="e1",
          generated_from="auto_topup/w1/2026-07/2000")
    entry(data_dir, "w1", -1800, kind="debit", eid="e2")   # balance now 200

    result = replenish({"today": "2026-07-25"})
    capped = [r for r in result["results"] if r.get("skipped") == "monthly cap reached"]
    assert capped, result
    assert capped[0]["capped_at_minor"] == 3000
    assert capped[0]["already_this_month_minor"] == 2000
    assert result["charged"] == 0


def test_a_zero_cap_means_auto_topup_is_off(tmp_path, monkeypatch):
    """A blank number must never read as 'unlimited' -- the safe default."""
    data_dir = setup_env(tmp_path, monkeypatch)
    make_wallet(data_dir, auto_replenish_enabled="true",
                auto_replenish_threshold_minor="1000",
                auto_replenish_amount_minor="2000",
                stripe_customer_id="cus_1", payment_method_ref="pm_1")
    entry(data_dir, "w1", 0 + 500, eid="e1")
    result = replenish({"today": "2026-07-25"})
    assert result["charged"] == 0
    assert any("cap is zero" in str(r.get("skipped", "")) for r in result["results"])


def test_a_healthy_balance_is_left_alone(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_wallet(data_dir, auto_replenish_enabled="true",
                auto_replenish_threshold_minor="1000",
                auto_replenish_amount_minor="2000",
                auto_replenish_monthly_cap_minor="10000",
                stripe_customer_id="cus_1", payment_method_ref="pm_1")
    entry(data_dir, "w1", 5000, eid="e1")
    result = replenish({"today": "2026-07-25"})
    assert result["charged"] == 0 and result["results"] == []


def test_without_stripe_it_reports_what_it_would_charge(tmp_path, monkeypatch):
    """The rule can be tuned before any money moves."""
    data_dir = setup_env(tmp_path, monkeypatch)
    make_wallet(data_dir, auto_replenish_enabled="true",
                auto_replenish_threshold_minor="1000",
                auto_replenish_amount_minor="2000",
                auto_replenish_monthly_cap_minor="10000")
    entry(data_dir, "w1", 100, eid="e1")
    result = replenish({"today": "2026-07-25"})
    assert result["charged"] == 0
    would = [r for r in result["results"] if r.get("would_charge_minor")]
    assert would and would[0]["would_charge_minor"] == 2000
    assert "not configured" in would[0]["reason"]


def test_a_wallet_without_a_saved_card_is_skipped_not_failed(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    monkeypatch.setenv("DBBASIC_STRIPE_SECRET_KEY", "sk_test")
    monkeypatch.setenv("DBBASIC_STRIPE_WEBHOOK_SECRET", "whsec_test")
    make_wallet(data_dir, auto_replenish_enabled="true",
                auto_replenish_threshold_minor="1000",
                auto_replenish_amount_minor="2000",
                auto_replenish_monthly_cap_minor="10000")
    entry(data_dir, "w1", 100, eid="e1")
    result = replenish({"today": "2026-07-25"})
    assert result["failed"] == 0
    assert any("no saved card" in str(r.get("skipped", "")) for r in result["results"])


def test_billing_not_installed_is_a_graceful_skip(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    assert replenish()["skipped"].startswith("billing not installed")
