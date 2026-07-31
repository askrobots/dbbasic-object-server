"""Resolving what the machine refused to decide.

The sweeper stops a submitted-but-unresolved run at `needs_review` with
its hold intact, because both automatic answers are wrong: releasing
gives away money the provider may already have taken, charging bills for
work that may never have arrived.

Stopping there is only a design if somebody can continue. A queue nobody
can act on is money parked forever, so this is the other half -- and it
is the only hand that moves a run out of needs_review.
"""

import pathlib

from conftest import stage_collection

import object_execution
import object_records
import object_template_runs
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER_OBJECTS = REPO_ROOT / "packages" / "app-runner" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()


def setup_env(tmp_path, monkeypatch, *, status="needs_review", price="600"):
    data_dir = tmp_path / "data"
    for pkg, name in (("app-runner", "template_runs"),
                      ("app-templates", "templates"),
                      ("app-billing", "wallets"),
                      ("app-billing", "wallet_entries")):
        stage_collection(data_dir, pkg, name)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    object_records.create_collection_record(
        "templates", {"id": "tpl", "name": "Picture", "handler": "ai_image",
                      "body": "x", "owner_id": "dan"}, base_dir=data_dir)
    object_records.create_collection_record(
        "wallets", {"id": "w1", "owner_id": "dan", "is_active": "true"},
        base_dir=data_dir)
    object_records.create_collection_record(
        "wallet_entries",
        {"wallet_id": "w1", "amount_minor": "5000", "kind": "topup",
         "owner_id": "dan"}, base_dir=data_dir)
    object_records.create_collection_record(
        "wallet_entries",
        {"wallet_id": "w1", "amount_minor": f"-{price}", "kind": "hold",
         "generated_from": "template_run/r1/hold", "owner_id": "dan"},
        base_dir=data_dir)
    object_records.create_collection_record(
        "template_runs",
        {"id": "r1", "template_id": "tpl", "template_name": "Picture",
         "handler": "ai_image", "status": status, "price_cents": price,
         "wallet_id": "w1", "owner_id": "dan",
         "provider_job_id": "job-abc", "provider_cost_cents": "40",
         "error": "unresolved after 3600s"},
        base_dir=data_dir)
    return data_dir


def resolve(payload, *, user_id="dan", is_admin=False):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "action_resolve_run", method="POST",
            payload={**payload,
                     "_identity": {"user_id": user_id, "is_admin": is_admin}}),
        roots=[RUNNER_OBJECTS]).result


def kinds(data_dir):
    return [e["kind"] for e in object_records.read_collection_records(
                "wallet_entries", base_dir=data_dir)
            if "template_run/r1/" in str(e.get("generated_from"))]


def test_charging_settles_the_run_as_delivered(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    result = resolve({"run_id": "r1", "decision": "charge",
                      "note": "file landed in the bucket"})
    assert result["ok"] and result["settlement"] == "settled"

    assert kinds(data_dir) == ["hold", "release", "debit"]
    run = object_records.get_collection_record("template_runs", "r1",
                                               base_dir=data_dir)
    assert run["status"] == "succeeded"
    assert "file landed in the bucket" in run["error"]
    assert "Resolved by dan as charge" in run["error"]


def test_refunding_releases_the_hold_and_charges_nothing(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    result = resolve({"run_id": "r1", "decision": "refund"})
    assert result["ok"]

    assert kinds(data_dir) == ["hold", "release"]
    run = object_records.get_collection_record("template_runs", "r1",
                                               base_dir=data_dir)
    assert run["status"] == "failed"


def test_what_it_cost_us_stays_visible_after_a_refund(tmp_path, monkeypatch):
    """Eating a provider cost is a number somebody can find, never a
    silent write-off."""
    data_dir = setup_env(tmp_path, monkeypatch)
    resolve({"run_id": "r1", "decision": "refund"})
    run = object_records.get_collection_record("template_runs", "r1",
                                               base_dir=data_dir)
    assert run["provider_cost_cents"] == "40"


def test_a_second_decision_writes_money_once(tmp_path, monkeypatch):
    """Idempotent by the same release marker everything else uses."""
    data_dir = setup_env(tmp_path, monkeypatch)
    resolve({"run_id": "r1", "decision": "charge"})
    before = kinds(data_dir)
    again = resolve({"run_id": "r1", "decision": "charge"})
    assert again["status"] == 409          # no longer needs_review
    assert kinds(data_dir) == before


def test_only_a_needs_review_run_can_be_resolved(tmp_path, monkeypatch):
    """A terminal run is already settled; a running one may still be
    answered, and polling beats a guess."""
    for status in ("running", "succeeded", "failed", "abandoned", "queued"):
        data_dir = setup_env(tmp_path, monkeypatch, status=status)
        result = resolve({"run_id": "r1", "decision": "charge"})
        assert result["status"] == 409, status
        assert kinds(data_dir) == ["hold"], status


def test_a_bad_decision_is_refused_explaining_both(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    result = resolve({"run_id": "r1", "decision": "maybe"})
    assert result["status"] == 400
    assert "charge" in result["error"] and "refund" in result["error"]


def test_a_stranger_cannot_decide_somebody_elses_money(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    result = resolve({"run_id": "r1", "decision": "charge"}, user_id="mallory")
    assert result["status"] == 403
    assert kinds(data_dir) == ["hold"]


def test_an_admin_can_resolve_any_run(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    result = resolve({"run_id": "r1", "decision": "refund"},
                     user_id="ops", is_admin=True)
    assert result["ok"]
    assert kinds(data_dir) == ["hold", "release"]


def test_anonymous_is_refused(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    assert resolve({"run_id": "r1", "decision": "charge"},
                   user_id="")["status"] == 401


def test_an_unknown_run_is_a_404(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    assert resolve({"run_id": "nope", "decision": "charge"})["status"] == 404


def test_resolving_never_contacts_a_provider(tmp_path, monkeypatch):
    """The standing refusal survives: a resolution verb settles MONEY
    against a fate somebody established by other means. It is not a
    retry, and must never become one."""
    source = (RUNNER_OBJECTS / "action" / "resolve_run.py").read_text()
    assert "never re-runs anything" in source
    for forbidden in ("HANDLERS", "send_http", "urlopen", "object_ai"):
        assert forbidden not in source, forbidden


# --- the queue has to be visible ------------------------------------------------

def count_review():
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "system_review_attention", method="COUNT", payload={}),
        roots=[RUNNER_OBJECTS]).result


def test_a_parked_run_shows_up_with_the_money_it_is_holding(
        tmp_path, monkeypatch):
    """A queue nobody can see is the same as no queue -- and this is the
    one where doing nothing costs somebody money continuously, so the
    detail leads with the amount held rather than the age."""
    setup_env(tmp_path, monkeypatch)
    result = count_review()
    assert result["count"] == 1
    assert "$6.00 held" in result["detail"]


def test_resolving_clears_it_from_the_queue(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    resolve({"run_id": "r1", "decision": "refund"})
    assert count_review()["count"] == 0


def test_only_needs_review_counts(tmp_path, monkeypatch):
    for status in ("running", "succeeded", "failed", "abandoned", "queued"):
        setup_env(tmp_path, monkeypatch, status=status)
        assert count_review()["count"] == 0, status


def test_it_degrades_to_zero_without_the_collection(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "empty"))
    assert count_review() == {"count": 0}
