"""Asynchronous runs, and the one place the sweeper was a money bug.

A Sora job takes minutes and q9 budgets thirty. Executing it inline would
block the pass, stall the batch, and eventually collide with the sweeper
writing off a job that is legitimately still running. So a run gains a
`running` status between claimed and terminal: submit, store the
provider job id, RETURN; later passes poll.

**The money rule is the reason this file exists.** The original sweeper
released the hold on every stale run, which is exactly right when nothing
was submitted -- an unclaimed job that never started cost nothing. It is
exactly wrong once a provider is holding the job: a submitted job may
already have cost everything, and handing the money back on a timer means
paying for it ourselves.

And the mirror is just as wrong. Charging a run whose outcome we never
learned bills a customer for something they may never have received.

The box genuinely cannot tell which, so it refuses to guess: a submitted
run that never resolves goes to `needs_review` with the hold still on,
and a person decides. That is why needs_review is deliberately NOT in
TERMINAL_STATUSES -- that tuple is what drives the automatic release.
"""

import pathlib

import pytest
from conftest import stage_collection

import object_execution
import object_records
import object_template_runs
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER_OBJECTS = REPO_ROOT / "packages" / "app-runner" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

NOW = "2026-07-31T12:00:00Z"


def run_row(**fields):
    row = {"id": "r1", "template_id": "tpl-image", "template_name": "Picture",
           "status": "claimed", "handler": "ai_image",
           "claimed_at": "2026-07-31T11:00:00Z",
           "heartbeat_at": "2026-07-31T11:00:00Z",
           "provider_job_id": "", "price_cents": "600", "wallet_id": "w1",
           "owner_id": "dan"}
    row.update(fields)
    return row


# --- the decision, pure ---------------------------------------------------------

def disposition(run, *, stale=300, max_run=3600):
    return object_template_runs.poll_disposition(
        run, now=NOW, stale_seconds=stale, max_run_seconds=max_run)


def test_a_stale_claim_that_never_submitted_is_abandoned():
    """Unchanged, and still right: nothing reached a provider, so nothing
    was spent and the hold comes back in full."""
    assert disposition(run_row()) == "abandon"


def test_a_stale_claim_that_DID_submit_is_never_abandoned():
    """THE money bug. Same dead worker, same stopped heartbeat -- but a
    provider is holding this job and may already have charged for it.
    Abandoning is what releases the hold, so abandoning here would hand
    back money we had already spent."""
    assert disposition(run_row(provider_job_id="job-abc")) == "poll"


def test_a_running_job_is_polled_not_swept():
    assert disposition(run_row(status="running",
                               provider_job_id="job-abc")) == "poll"


def test_a_running_job_past_the_ceiling_stops_for_a_person():
    """Polling has not resolved it for longer than any job should take.
    It stops being a machine's decision -- and the hold stays put, since
    both automatic answers are wrong."""
    old = run_row(status="running", provider_job_id="job-abc",
                  claimed_at="2026-07-31T10:00:00Z",
                  heartbeat_at="2026-07-31T11:59:00Z")
    assert disposition(old, max_run=3600) == "review"


def test_a_live_claim_is_left_alone():
    assert disposition(run_row(heartbeat_at="2026-07-31T11:59:00Z")) is None


def test_terminal_and_reviewed_runs_are_never_reconsidered():
    for status in ("succeeded", "failed", "abandoned", "needs_review"):
        assert disposition(run_row(status=status,
                                   provider_job_id="job-abc")) is None, status


def test_a_job_id_outranks_a_status_a_crash_prevented():
    """A run still marked `claimed` but carrying a job id was submitted;
    the status write simply never landed. The id is the evidence, and
    evidence outranks a field a crash may have stopped."""
    assert disposition(run_row(status="claimed",
                               provider_job_id="job-abc")) == "poll"


# --- the sweeper, end to end ----------------------------------------------------

def setup_env(tmp_path, monkeypatch, runs, entries=()):
    data_dir = tmp_path / "data"
    for pkg, name in (("app-runner", "template_runs"),
                      ("app-templates", "templates"),
                      ("app-billing", "wallets"),
                      ("app-billing", "wallet_entries"),
                      ("app-settings", "app_settings")):
        stage_collection(data_dir, pkg, name)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    object_records.create_collection_record(
        "templates", {"id": "tpl-image", "name": "Picture", "handler": "ai_image",
                      "body": "a picture of {thing}", "owner_id": "dan"},
        base_dir=data_dir)
    object_records.create_collection_record(
        "wallets", {"id": "w1", "owner_id": "dan", "is_active": "true"},
        base_dir=data_dir)
    for row in runs:
        object_records.create_collection_record("template_runs", row,
                                                base_dir=data_dir)
    for entry in entries:
        object_records.create_collection_record("wallet_entries", entry,
                                                base_dir=data_dir)
    return data_dir


def hold_for(run_id, amount=600):
    return {"wallet_id": "w1", "amount_minor": str(-amount), "kind": "hold",
            "description": f"Hold: {run_id}",
            "generated_from": f"template_run/{run_id}/hold", "owner_id": "dan"}


def sweep():
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "system_run_sweeper", method="EVENT", payload={"now": NOW}),
        roots=[RUNNER_OBJECTS]).result


def entries_for(data_dir, run_id):
    return [e for e in object_records.read_collection_records(
                "wallet_entries", base_dir=data_dir)
            if f"template_run/{run_id}/" in str(e.get("generated_from"))]


def test_the_sweeper_releases_a_hold_for_a_run_that_never_submitted(
        tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch, [run_row()],
                         entries=[hold_for("r1")])
    result = sweep()
    assert result["runs"] == ["r1"]
    kinds = [e["kind"] for e in entries_for(data_dir, "r1")]
    assert "release" in kinds


def test_the_sweeper_does_not_touch_the_hold_of_a_submitted_run(
        tmp_path, monkeypatch):
    """THE test. The worker died, the heartbeat stopped, and the money
    stays exactly where it is because a provider is holding the job."""
    data_dir = setup_env(tmp_path, monkeypatch,
                         [run_row(provider_job_id="job-abc")],
                         entries=[hold_for("r1")])
    result = sweep()
    assert result["runs"] == []
    assert result["needs_review"] == []

    kinds = [e["kind"] for e in entries_for(data_dir, "r1")]
    assert kinds == ["hold"]          # no release, no debit
    run = object_records.get_collection_record("template_runs", "r1",
                                               base_dir=data_dir)
    assert run["status"] == "claimed"   # untouched, waiting to be polled


def test_an_unresolved_submitted_run_escalates_with_the_hold_intact(
        tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch,
                         [run_row(status="running", provider_job_id="job-abc",
                                  claimed_at="2026-07-31T10:00:00Z")],
                         entries=[hold_for("r1")])
    result = sweep()
    assert result["needs_review"] == ["r1"]

    run = object_records.get_collection_record("template_runs", "r1",
                                               base_dir=data_dir)
    assert run["status"] == "needs_review"
    assert "may already have been charged" in run["error"]

    kinds = [e["kind"] for e in entries_for(data_dir, "r1")]
    assert kinds == ["hold"]          # still held: a person decides


def test_needs_review_is_not_terminal_so_nothing_auto_releases_it():
    """The property the whole design rests on. TERMINAL_STATUSES drives
    the sweeper's automatic hold release; needs_review must never join
    it, or the escalation quietly becomes the release it exists to
    prevent."""
    assert object_template_runs.NEEDS_REVIEW_STATUS \
        not in object_template_runs.TERMINAL_STATUSES


# --- the engine: submit, poll, settle -------------------------------------------

@pytest.fixture
def async_handler(monkeypatch):
    """A stand-in provider, so the money rule is proven before a real one
    rides on it. `state` lets a test decide what the provider says."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "template_runner_mod",
        RUNNER_OBJECTS / "system" / "template_runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    state = {"submitted": [], "done": False, "ok": True, "fail_submit": False}

    def submit(run, base, timeout):
        if state["fail_submit"]:
            return {"provider_job_id": "", "error": "provider refused"}
        state["submitted"].append(run["id"])
        return {"provider_job_id": f"job-{run['id']}", "error": ""}

    def poll(run, base, timeout):
        if not state["done"]:
            return {"done": False}
        return {"done": True, "ok": state["ok"], "output": "a picture",
                "error": "" if state["ok"] else "provider failed",
                "provider_cost_cents": 12}

    module.ASYNC_HANDLERS["ai_image"] = (submit, poll)
    return module, state


def pass_once(module):
    return module.EVENT({})


def test_a_submit_leaves_the_run_running_and_the_money_held(
        tmp_path, monkeypatch, async_handler):
    module, state = async_handler
    data_dir = setup_env(tmp_path, monkeypatch,
                         [run_row(status="queued", claimed_at="",
                                  heartbeat_at="")],
                         entries=[hold_for("r1")])
    result = pass_once(module)
    assert result["results"][0]["status"] == "running"

    run = object_records.get_collection_record("template_runs", "r1",
                                               base_dir=data_dir)
    assert run["status"] == "running"
    assert run["provider_job_id"] == "job-r1"
    assert [e["kind"] for e in entries_for(data_dir, "r1")] == ["hold"]


def test_polling_an_unfinished_job_only_touches_the_heartbeat(
        tmp_path, monkeypatch, async_handler):
    """What stops the sweeper writing off a five-minute job."""
    module, state = async_handler
    data_dir = setup_env(tmp_path, monkeypatch,
                         [run_row(status="queued", claimed_at="",
                                  heartbeat_at="")],
                         entries=[hold_for("r1")])
    pass_once(module)
    before = object_records.get_collection_record(
        "template_runs", "r1", base_dir=data_dir)["heartbeat_at"]

    second = pass_once(module)
    assert second["polled"] == 1
    after = object_records.get_collection_record("template_runs", "r1",
                                                 base_dir=data_dir)
    assert after["status"] == "running"
    assert after["heartbeat_at"] >= before
    assert [e["kind"] for e in entries_for(data_dir, "r1")] == ["hold"]


def test_a_finished_job_settles_on_a_later_pass(
        tmp_path, monkeypatch, async_handler):
    module, state = async_handler
    data_dir = setup_env(tmp_path, monkeypatch,
                         [run_row(status="queued", claimed_at="",
                                  heartbeat_at="")],
                         entries=[hold_for("r1")])
    pass_once(module)
    state["done"] = True
    pass_once(module)

    run = object_records.get_collection_record("template_runs", "r1",
                                               base_dir=data_dir)
    assert run["status"] == "succeeded"
    assert run["output"] == "a picture"
    assert run["provider_cost_cents"] == "12"

    kinds = [e["kind"] for e in entries_for(data_dir, "r1")]
    assert kinds == ["hold", "release", "debit"]


def test_a_job_that_fails_at_the_provider_costs_the_user_nothing(
        tmp_path, monkeypatch, async_handler):
    module, state = async_handler
    data_dir = setup_env(tmp_path, monkeypatch,
                         [run_row(status="queued", claimed_at="",
                                  heartbeat_at="")],
                         entries=[hold_for("r1")])
    pass_once(module)
    state["done"], state["ok"] = True, False
    pass_once(module)

    kinds = [e["kind"] for e in entries_for(data_dir, "r1")]
    assert kinds == ["hold", "release"]      # released, never debited


def test_a_submission_that_never_reached_the_provider_refunds_in_full(
        tmp_path, monkeypatch, async_handler):
    """No job id means nothing was spent, so this is an ordinary failure
    and the hold comes straight back."""
    module, state = async_handler
    state["fail_submit"] = True
    data_dir = setup_env(tmp_path, monkeypatch,
                         [run_row(status="queued", claimed_at="",
                                  heartbeat_at="")],
                         entries=[hold_for("r1")])
    pass_once(module)

    run = object_records.get_collection_record("template_runs", "r1",
                                               base_dir=data_dir)
    assert run["status"] == "failed"
    assert [e["kind"] for e in entries_for(data_dir, "r1")] == ["hold", "release"]


def test_a_failing_poll_is_not_a_failing_job(
        tmp_path, monkeypatch, async_handler):
    """A network blip while asking must not fail a job the provider is
    still working on -- and must not settle anything."""
    module, state = async_handler
    data_dir = setup_env(tmp_path, monkeypatch,
                         [run_row(status="queued", claimed_at="",
                                  heartbeat_at="")],
                         entries=[hold_for("r1")])
    pass_once(module)

    def exploding_poll(run, base, timeout):
        raise RuntimeError("connection reset")

    module.ASYNC_HANDLERS["ai_image"] = (
        module.ASYNC_HANDLERS["ai_image"][0], exploding_poll)
    result = pass_once(module)

    assert result["poll_results"][0]["status"] == "running"
    run = object_records.get_collection_record("template_runs", "r1",
                                               base_dir=data_dir)
    assert run["status"] == "running"
    assert [e["kind"] for e in entries_for(data_dir, "r1")] == ["hold"]


def test_a_sync_handler_still_runs_inline(tmp_path, monkeypatch, async_handler):
    """Both shapes coexist: echo has no async pair and must not be
    dragged through submit/poll."""
    module, state = async_handler
    data_dir = setup_env(tmp_path, monkeypatch,
                         [run_row(id="r2", status="queued", handler="echo",
                                  price_cents="0", claimed_at="",
                                  heartbeat_at="", rendered_body="hello")])
    result = pass_once(module)
    assert result["results"][0]["status"] == "succeeded"
    assert object_records.get_collection_record(
        "template_runs", "r2", base_dir=data_dir)["output"] == "hello"
