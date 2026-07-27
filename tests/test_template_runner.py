"""The template runner: hold the money, run the job, settle exactly once.

Slice 1 of plan/template-runner-spec.md, and the money model is the part
that cannot be retrofitted, so it is the part this file leans on:

THE GATE FIRES AT COMMITMENT. The hold is a negative wallet entry placed
at queue time through hook_wallet_entries, so a user with five cents
cannot queue four hundred runs and present the provider bill later. The
four-hundredth hold fails the gate.

SETTLEMENT IS RELEASE + ORDINARY DEBIT, EXACTLY ONCE. Success releases
the hold in full and charges a plain debit for the stamped price; failure
releases alone. Idempotent by provenance marker checked against the
ENTRIES, never run.status, because the crash worth designing for is the
one between writing money and updating status.

A STALE RUN IS NEVER RETRIED. The provider call is not idempotent and is
not ours; abandoned is a terminal status with no exits in the declared
transition map, and the sweeper releases the hold and stops.
"""

import json
import pathlib
import shutil

import pytest
from conftest import stage_collection

import object_execution
import object_records
import object_template_runs
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
RUNNER_OBJECTS = PACKAGES / "app-runner" / "objects"
BILLING_OBJECTS = PACKAGES / "app-billing" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()


# --- the pure module -----------------------------------------------------------

def template(**fields):
    row = {"id": "t1", "name": "Greeting", "handler": "echo",
           "body": "Hello {name}, welcome to {place}.",
           "run_cost_cents": "50",
           "schema": json.dumps({"properties": {"name": {"title": "Name"},
                                                "place": {}},
                                 "required": ["name"]})}
    row.update({k: str(v) for k, v in fields.items()})
    return row


def test_placeholders_fill_from_the_form_and_unknown_ones_survive():
    """A brace in prose must not kill a run, and a silently emptied
    placeholder is a prompt that quietly lost its subject -- the worst
    outcome, because the provider answers SOMETHING and charges for it."""
    out = object_template_runs.render("Hi {name}, re: {TBD}", {"name": "Ada"})
    assert out == "Hi Ada, re: {TBD}"


def test_every_missing_required_field_is_reported_together():
    found = object_template_runs.problems(template(handler=""), {})
    joined = " ".join(found)
    assert len(found) == 2, found
    assert "declares no handler" in joined
    assert "'Name' is required" in joined


def test_the_stamp_carries_everything_execution_needs():
    """The run must be immune to every future edit of the template --
    including re-rendering, because rendering reads the template and by
    execution time the template may say something else."""
    stamped = object_template_runs.stamp(template(), {"name": "Ada",
                                                      "place": "the shop"},
                                         model="anthropic:claude-sonnet-4-5")
    assert stamped["rendered_body"] == "Hello Ada, welcome to the shop."
    assert stamped["price_cents"] == "50"
    assert stamped["handler"] == "echo"
    assert stamped["model"] == "anthropic:claude-sonnet-4-5"
    assert json.loads(stamped["form_data"]) == {"name": "Ada",
                                                "place": "the shop"}


def run_row(**fields):
    row = {"id": "r1", "price_cents": "50", "wallet_id": "w1",
           "owner_id": "dan", "template_name": "Greeting",
           "status": "claimed"}
    row.update({k: str(v) for k, v in fields.items()})
    return row


def test_success_settles_as_release_plus_an_ordinary_debit():
    entries = object_template_runs.settlement(run_row(), succeeded=True)
    assert [e["kind"] for e in entries] == ["release", "debit"]
    assert [int(e["amount_minor"]) for e in entries] == [50, -50]
    assert entries[0]["generated_from"] == "template_run/r1/release"
    assert entries[1]["generated_from"] == "template_run/r1/charge"


def test_failure_settles_as_release_alone():
    """The user pays nothing for a run that did not do the thing,
    whatever it cost US at the provider."""
    entries = object_template_runs.settlement(run_row(), succeeded=False)
    assert [e["kind"] for e in entries] == ["release"]
    assert int(entries[0]["amount_minor"]) == 50


def test_a_free_run_touches_no_wallet_at_all():
    assert object_template_runs.settlement(run_row(price_cents="0"),
                                           succeeded=True) == []
    assert object_template_runs.hold_entry("r1", "w1", 0, "dan") is None


def test_the_charge_is_a_debit_and_the_release_is_not_a_refund():
    """The reason hold/release are the only new kinds: every report that
    already reads debits keeps working, and a released hold never
    masquerades as money returned to a customer."""
    entries = object_template_runs.settlement(run_row(), succeeded=True)
    kinds = {e["kind"] for e in entries}
    assert "refund" not in kinds
    assert "debit" in kinds


def test_already_settled_reads_the_entries_and_never_the_status():
    entries = [{"generated_from": "template_run/r1/release"}]
    assert object_template_runs.already_settled("r1", entries) is True
    assert object_template_runs.already_settled("r2", entries) is False


def test_outstanding_holds_is_a_fold_over_the_entries():
    entries = [
        {"generated_from": "template_run/r1/hold", "amount_minor": "-50"},
        {"generated_from": "template_run/r2/hold", "amount_minor": "-30"},
        {"generated_from": "template_run/r1/release", "amount_minor": "50"},
        {"generated_from": "checkout/x", "amount_minor": "-99"},
    ]
    assert object_template_runs.outstanding_holds(entries) == {"r2": 30}


def test_staleness_is_pure_and_only_claimed_runs_can_be_stale():
    now = "2026-07-26T12:00:00Z"
    old = {"status": "claimed", "heartbeat_at": "2026-07-26T11:30:00Z"}
    fresh = {"status": "claimed", "heartbeat_at": "2026-07-26T11:59:00Z"}
    queued = {"status": "queued", "heartbeat_at": "2026-07-26T01:00:00Z"}
    assert object_template_runs.is_stale(old, now=now, stale_seconds=900)
    assert not object_template_runs.is_stale(fresh, now=now, stale_seconds=900)
    assert not object_template_runs.is_stale(queued, now=now, stale_seconds=900)


# --- through the objects, money and all ----------------------------------------

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    base = tmp_path / "data"
    stage_collection(base, "app-runner", "template_runs")
    stage_collection(base, "app-templates", "templates")
    stage_collection(base, "app-billing", "wallets")
    stage_collection(base, "app-billing", "wallet_entries")
    stage_collection(base, "app-settings", "app_settings")
    # Both roots merged into one tree and exported via the environment,
    # the way test_promotions does it: action_run_template asks
    # hook_wallet_entries through a FRESH in-process runtime (the same
    # mechanism the shop uses), and that runtime resolves objects from the
    # environment, not from this test's execute_object roots.
    objects_root = tmp_path / "objects"
    for source in (RUNNER_OBJECTS, BILLING_OBJECTS):
        shutil.copytree(source, objects_root, dirs_exist_ok=True)
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(base))
    return base


def run_object(object_id, payload, *, method="POST"):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(object_id, method=method,
                                                payload=payload)).result


def seed_template(data_dir, **fields):
    return object_records.create_collection_record(
        "templates", template(**fields), base_dir=data_dir)


def seed_wallet(data_dir, cents, wallet_id="w1", owner="dan"):
    object_records.create_collection_record(
        "wallets", {"id": wallet_id, "owner_id": owner, "kind": "balance",
                    "is_active": "true"},
        base_dir=data_dir)
    if cents:
        object_records.create_collection_record(
            "wallet_entries",
            {"wallet_id": wallet_id, "amount_minor": str(cents),
             "kind": "topup", "owner_id": owner},
            base_dir=data_dir)


def queue(data_dir, **payload):
    payload.setdefault("template_id", "t1")
    payload.setdefault("form_data", {"name": "Ada", "place": "the shop"})
    payload.setdefault("_identity", {"user_id": "dan"})
    return run_object("action_run_template", payload)


def balance(data_dir, wallet_id="w1"):
    return sum(int(row.get("amount_minor") or 0)
               for row in object_records.read_collection_records(
                   "wallet_entries", base_dir=data_dir)
               if row.get("wallet_id") == wallet_id)


def test_queueing_places_the_hold_and_stamps_the_terms(data_dir):
    seed_template(data_dir)
    seed_wallet(data_dir, 200)

    queued = queue(data_dir)
    assert queued["ok"] is True and queued["held"] is True

    assert balance(data_dir) == 150          # 200 - 50 held

    run = object_records.read_collection_records("template_runs",
                                                 base_dir=data_dir)[0]
    assert run["status"] == "queued"
    assert run["rendered_body"] == "Hello Ada, welcome to the shop."
    assert run["price_cents"] == "50"
    assert run["idempotency_key"]            # minted before any call


def test_the_gate_refuses_the_run_the_balance_cannot_cover(data_dir):
    """THE reason holds exist: the gate is asked at commitment. Three runs
    fit a 120-cent balance twice; the third is refused with the wallet
    still intact and nothing queued."""
    seed_template(data_dir)
    seed_wallet(data_dir, 120)

    assert queue(data_dir)["ok"] is True
    assert queue(data_dir)["ok"] is True
    refused = queue(data_dir)
    assert refused["status"] == 402
    assert "Insufficient wallet balance" in refused["error"]

    assert balance(data_dir) == 20           # two holds, no third
    runs = object_records.read_collection_records("template_runs",
                                                  base_dir=data_dir)
    assert len(runs) == 2


def test_editing_the_template_after_queueing_restates_nothing(data_dir):
    """Doctrine #1, on runs: the stamped terms are the terms."""
    seed_template(data_dir)
    seed_wallet(data_dir, 200)
    queue(data_dir)

    object_records.update_collection_record(
        "templates", "t1",
        {"body": "Completely different {name}", "run_cost_cents": "9000"},
        base_dir=data_dir)

    result = run_object("system_template_runner", {})
    assert result["claimed"] == 1
    run = object_records.read_collection_records("template_runs",
                                                 base_dir=data_dir)[0]
    assert run["status"] == "succeeded"
    assert run["output"] == "Hello Ada, welcome to the shop."
    assert balance(data_dir) == 150          # charged 50, not 9000


def test_a_successful_run_settles_release_plus_debit_and_only_once(data_dir):
    seed_template(data_dir)
    seed_wallet(data_dir, 200)
    queue(data_dir)

    run_object("system_template_runner", {})
    assert balance(data_dir) == 150          # 200 - 50 charged

    entries = object_records.read_collection_records("wallet_entries",
                                                     base_dir=data_dir)
    kinds = sorted(e["kind"] for e in entries)
    assert kinds == ["debit", "hold", "release", "topup"]

    # A second pass finds nothing queued and, crucially, re-settles nothing.
    again = run_object("system_template_runner", {})
    assert again["claimed"] == 0
    assert balance(data_dir) == 150


def test_a_failed_run_costs_the_user_nothing(data_dir):
    seed_template(data_dir, handler="ai_text")   # queue-time check passes...
    seed_wallet(data_dir, 200)
    object_records.create_collection_record(
        "app_settings", {"key": "runner.ai_model",
                         "value": "anthropic:claude-sonnet-4-5"},
        base_dir=data_dir)
    import object_service_keys
    object_service_keys.set_service_key("dan", "anthropic", "sk-test-not-real",
                                        base_dir=data_dir)
    queue(data_dir)
    import object_service_keys as keys
    keys.remove_service_key("dan", "anthropic", base_dir=data_dir)

    result = run_object("system_template_runner", {})   # ...and execution fails
    assert result["results"][0]["status"] == "failed"

    run = object_records.read_collection_records("template_runs",
                                                 base_dir=data_dir)[0]
    assert run["status"] == "failed"
    assert "API key" in run["error"]
    assert balance(data_dir) == 200          # hold released in full
    kinds = sorted(e["kind"] for e in object_records.read_collection_records(
        "wallet_entries", base_dir=data_dir))
    assert "debit" not in kinds


def test_a_free_template_runs_without_a_wallet_existing_at_all(data_dir):
    """Money machinery that runs for free jobs is machinery that fails
    for free jobs -- so for a zero-price template there must be none."""
    seed_template(data_dir, run_cost_cents="0")
    queued = queue(data_dir)
    assert queued["ok"] is True and queued["held"] is False

    run_object("system_template_runner", {})
    run = object_records.read_collection_records("template_runs",
                                                 base_dir=data_dir)[0]
    assert run["status"] == "succeeded"
    assert not object_records.read_collection_records("wallet_entries",
                                                      base_dir=data_dir)


def test_an_ai_template_with_no_provider_is_refused_at_queue_time(data_dir):
    """The capability boundary, refused while the person who can fix it
    is looking at the screen -- not discovered on the daemon an hour
    later. Nothing queued, nothing held."""
    seed_template(data_dir, handler="ai_text")
    seed_wallet(data_dir, 200)

    refused = queue(data_dir)
    assert refused["status"] == 409
    assert "runner.ai_model" in refused["error"]
    assert balance(data_dir) == 200
    assert not object_records.read_collection_records("template_runs",
                                                      base_dir=data_dir)


def test_an_anonymous_run_is_refused_because_runs_cost_money(data_dir):
    seed_template(data_dir)
    refused = run_object("action_run_template",
                         {"template_id": "t1", "form_data": {"name": "x"}})
    assert refused["status"] == 401
    assert "open tap" in refused["error"]


def test_a_template_with_no_handler_is_a_document_not_a_job(data_dir):
    seed_template(data_dir, handler="")
    refused = queue(data_dir)
    assert refused["status"] == 400
    assert "declares no handler" in refused["error"]


# --- the sweeper ----------------------------------------------------------------

def test_a_stale_run_is_abandoned_refunded_and_never_requeued(data_dir):
    seed_template(data_dir)
    seed_wallet(data_dir, 200)
    queue(data_dir)

    run = object_records.read_collection_records("template_runs",
                                                 base_dir=data_dir)[0]
    object_records.update_collection_record(
        "template_runs", run["id"],
        {"status": "claimed", "claimed_by": "a-runner-that-died",
         "claimed_at": "2026-07-26T01:00:00Z",
         "heartbeat_at": "2026-07-26T01:00:00Z"},
        base_dir=data_dir)

    result = run_object("system_run_sweeper", {"now": "2026-07-26T12:00:00Z"})
    assert result["swept"] == 1

    swept = object_records.read_collection_records("template_runs",
                                                   base_dir=data_dir)[0]
    assert swept["status"] == "abandoned"
    assert "NOT retried" in swept["error"]
    assert balance(data_dir) == 200          # the hold came back in full

    # And the runner never picks it up again: abandoned is not queued.
    after = run_object("system_template_runner", {})
    assert after["claimed"] == 0

    # Sweeping again releases nothing twice.
    again = run_object("system_run_sweeper", {"now": "2026-07-26T13:00:00Z"})
    assert again["swept"] == 0 or all(r["hold"] == "already settled"
                                      for r in again["runs"])
    assert balance(data_dir) == 200


def test_abandoned_is_a_dead_end_in_the_declared_state_machine():
    """The transition map is the enforcement: no status may move a run
    out of abandoned, so nothing -- not a bug, not a helpful operator
    endpoint added later -- can quietly re-run a paid provider call."""
    schema = json.loads((PACKAGES / "app-runner" / "schemas"
                         / "template_runs.json").read_text())
    status = next(f for f in schema["fields"] if f["name"] == "status")
    transitions = status["transitions"]
    assert "abandoned" not in transitions      # no exits
    assert "succeeded" not in transitions
    assert "failed" not in transitions
    for moves in transitions.values():
        assert "queued" not in moves           # nothing moves BACK to queued


def test_nothing_in_the_package_retries_a_run():
    """Named as a grep on the whole package, the same way app-notary pins
    its no-delete rule: the risk is not today's code, it is somebody later
    adding retry as an improvement to a paid non-idempotent call."""
    for path in (PACKAGES / "app-runner").rglob("*.py"):
        source = path.read_text()
        assert '"queued"}' not in source.replace("status\": \"queued", ""), path
        # No update may ever set a run BACK to queued.
        assert '{"status": "queued"' not in source, path


def test_the_runner_and_sweeper_declare_EVENT():
    for name in ("template_runner", "run_sweeper"):
        source = (PACKAGES / "app-runner" / "objects" / "system"
                  / f"{name}.py").read_text()
        assert "def EVENT(" in source, name
        assert "POST = EVENT" in source, name


def test_wallet_entries_gained_hold_and_release_and_nothing_was_removed():
    schema = json.loads((PACKAGES / "app-billing" / "schemas"
                         / "wallet_entries.json").read_text())
    kind = next(f for f in schema["fields"] if f["name"] == "kind")
    assert set(kind["enum"]) == {"topup", "auto_topup", "debit", "refund",
                                 "promo", "adjustment", "hold", "release"}
