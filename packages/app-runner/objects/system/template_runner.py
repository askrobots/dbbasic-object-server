"""system_template_runner -- claim queued runs, execute them, settle the
money. The engine; every handler is a plug in it.

Runs every minute from the package schedule, and by hand from the same
object, so the button and the pass cannot drift apart.

## One engine, pluggable handlers

The handler owns exactly one thing: turning a claimed run into an output
or an error. Claiming, charging, settling, recording and reporting belong
to the engine, and no handler gets opinions about them. "Screenshot Page"
will be one handler of this engine, not a feature -- the uniform layer
the predecessor hand-built four times.

Slice 1 ships two: `echo` (the rendered body straight back -- free, no
provider, and the handler that proves the money model without spending
anything) and `ai_text` (the rendered body to a configured provider
through object_ai, which already exists).

## What a handler must never do

**Silently degrade.** No "provider unavailable, here is a placeholder".
A run that did not do the thing fails, releases its hold, and says why in
words a person can act on -- which capability is missing and what to
configure.

## Settlement, and the crash it is designed around

Settlement is release-in-full plus an ordinary debit on success, release
alone on failure (object_template_runs.settlement). It is idempotent by
provenance marker, checked against the WALLET ENTRIES rather than the
run's status, because the crash worth designing for is exactly the one
between writing money and updating status: replayed at the next pass, the
marker is found and nothing posts twice (doctrine #7).

Money is written FIRST, status second. If the pass dies between them the
run re-settles as a no-op on the next pass and the status catches up. The
other order would leave a run marked settled whose money never moved.

The settlement writes are in-process and deliberately NOT gated: a
release is money coming back (never gated anyway), and the debit is the
recording of a spend that already happened at the provider. Refusing to
record reality does not undo it.

## What the engine will not do

**Retry.** A run that fails is failed. A claimed run whose heartbeat
stops is the sweeper's, becomes abandoned, and stays abandoned. The
provider call is not idempotent and is not ours -- re-running a job that
may already have been billed is paying twice for one thing, and a retry
loop around a paid non-idempotent call is an unbounded spend wearing a
robustness costume. A retry is a new run, with a new hold, started by a
person who can see what happened.
"""

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

import object_ai
import object_records
import object_service_keys
import object_template_runs

ACTOR = "system_template_runner"
RUNS = "template_runs"

BATCH_SETTING = "runner.batch"
TIMEOUT_SETTING = "runner.timeout_seconds"
DEFAULT_BATCH = 3
DEFAULT_TIMEOUT = 120


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _int(value, default=0):
    try:
        return int(_text(value) or default)
    except (TypeError, ValueError):
        return default


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _setting(base, key, default):
    try:
        for row in object_records.read_collection_records("app_settings",
                                                          base_dir=base):
            if _text(row.get("key")) == key:
                return _text(row.get("value")) or default
    except Exception:
        pass
    return default


# === handlers ================================================================
#
# handler(run, base, timeout) -> {"ok", "output", "error",
#                                 "provider_cost_cents", "provider_job_id"}
# The whole contract. A handler that needs more than this is trying to own
# something the engine owns.
#
# ASYNC handlers are a PAIR in ASYNC_HANDLERS: (submit, poll).
#
#   submit(run, base, timeout) -> {"provider_job_id", "error"}
#   poll(run, base, timeout)   -> {"done": bool, ...the sync shape when done}
#
# The engine, not the handler, owns what that means: a submit that
# returns a job id moves the run to `running` and RETURNS, leaving the
# pass free; later passes poll. A Sora job takes minutes and q9 budgets
# thirty, so executing it inline would block the batch and eventually
# collide with the sweeper writing off a job that is legitimately still
# running. Short handlers (echo, ai_text) stay inline -- the engine
# supports both shapes and the handler declares which it is by which
# registry it appears in.

def _handle_echo(run, base, timeout):
    """The rendered body straight back. Free, instant, and the handler
    slice 1 exists for: it exercises queue, claim, settle and report with
    no provider and no spend, which is how the money model gets proven
    before a dollar rides on it."""
    return {"ok": True, "output": _text(run.get("rendered_body")),
            "error": "", "provider_cost_cents": 0, "provider_job_id": ""}


def _handle_ai_text(run, base, timeout):
    """The rendered body to the stamped model, through object_ai.

    The model comes off the RUN, not the settings -- stamped at queue
    time, so a model swap in settings changes the next run and never
    restates what produced this one. The key is re-read now because keys
    get deleted between queue and run; that failure is the rare race, and
    it fails with the same words the queue-time check uses.
    """
    model = _text(run.get("model"))
    if ":" not in model:
        return {"ok": False, "output": "",
                "error": "No model was stamped on this run; it should have "
                         "been refused at queue time.",
                "provider_cost_cents": 0, "provider_job_id": ""}
    service, model_name = object_ai.split_model(model)
    key = object_service_keys.get_service_key(
        _text(run.get("owner_id")), service, base_dir=base)
    if not key:
        return {"ok": False, "output": "",
                "error": f"No {service} API key is stored for "
                         f"{_text(run.get('owner_id'))} -- it existed at queue "
                         f"time and is gone now. Store one and start a new "
                         f"run; this one was not charged.",
                "provider_cost_cents": 0, "provider_job_id": ""}

    def send_http(url, headers, body):
        request = urllib.request.Request(url, data=body, headers=dict(headers),
                                         method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def refuse_tool(name, arguments):
        # No tools are offered, so this cannot be reached; refusing rather
        # than passing silently keeps that a fact instead of an assumption.
        return {"http_status": 400,
                "response": {"error": "Template runs offer no tools."}}

    try:
        result = object_ai.run_chat(
            send_http=send_http,
            dispatch_tool=refuse_tool,
            service=service,
            model=model_name,
            key=key,
            message=_text(run.get("rendered_body")),
            system=_text(run.get("instructions")) or None,
        )
    except object_ai.AIProviderError as exc:
        return {"ok": False, "output": "",
                "error": f"{service} refused the request: {str(exc)[:300]}",
                "provider_cost_cents": 0, "provider_job_id": ""}
    except Exception as exc:
        return {"ok": False, "output": "",
                "error": f"The provider call failed: {str(exc)[:300]}",
                "provider_cost_cents": 0, "provider_job_id": ""}

    usage = result.get("usage") or {}
    cost = 0
    try:
        prices = object_records.read_collection_records("ai_prices",
                                                        base_dir=base)
        row = object_ai.select_price_row(prices, provider=service,
                                         model=model_name)
        computed = object_ai.compute_cost_cents(
            _int(usage.get("input_tokens")), _int(usage.get("output_tokens")),
            row)
        cost = computed if computed is not None else 0
    except Exception:
        cost = 0  # no price table is expected, not an error

    return {"ok": True, "output": _text(result.get("reply")),
            "error": "", "provider_cost_cents": cost, "provider_job_id": ""}


HANDLERS = {
    "echo": _handle_echo,
    "ai_text": _handle_ai_text,
}

#: name -> (submit, poll). Empty until a media handler lands; the engine
#: below is what that handler will plug into, and it is tested with a
#: stand-in so the money rule is proven before a provider rides on it.
ASYNC_HANDLERS = {}


# === the engine ==============================================================

def _settle(base, run, *, succeeded):
    """Write the run's settlement, exactly once, money before status.

    Returns a note for the pass report. Reads the entries fresh and checks
    the release marker -- never run.status -- because the crash this
    protects is the one between money and status.
    """
    try:
        entries = object_records.read_collection_records("wallet_entries",
                                                         base_dir=base)
    except Exception:
        entries = []
    if object_template_runs.already_settled(run.get("id"), entries):
        return "already settled"

    for entry in object_template_runs.settlement(run, succeeded=succeeded):
        object_records.create_collection_record(
            "wallet_entries", entry, base_dir=base, actor=ACTOR)
    return "settled"


def _claim(base, run):
    """Take exclusive ownership of a queued run, or return None if
    somebody else got there first.

    THE claim is a compare-and-set (63, plan/vocabulary/63-concurrency-spec.md),
    not a plain write, and this is the difference between one provider
    call and two. Without the precondition: two passes -- the minutely
    schedule and an operator pressing Run Now -- both read the same row as
    `queued`, both write `claimed` (the transition check is a no-op when
    old == new), and both execute. Settlement is idempotent so the USER is
    charged once, which is exactly what makes it dangerous: the duplicate
    lands on OUR side of the ledger, as a second provider bill nothing in
    the money model was watching.

    `expected_rev` closes it inside the write lock: the second pass's rev
    no longer matches the row the first pass just claimed, so it 409s and
    never runs the handler. Same primitive, same idiom as
    app-timers/objects/site/timer_actions.py, and the same one an agent
    uses to claim a task.
    """
    return object_records.update_collection_record(
        RUNS, _text(run.get("id")),
        {"status": "claimed", "claimed_by": ACTOR,
         "claimed_at": _now(), "heartbeat_at": _now()},
        base_dir=base, actor=ACTOR,
        expected_rev=object_records.compute_record_rev(run))


def _submit_one(base, run, timeout, pair):
    """Claim a run, hand it to the provider, and leave it `running`.

    The pass ends here. Nothing is settled, because nothing has finished
    -- and the hold placed at queue time stays exactly where it is, which
    is the whole reason a hold exists rather than a charge on completion.
    """
    run_id = _text(run.get("id"))
    submit, _poll = pair
    try:
        _claim(base, run)
    except object_records.VersionConflictError:
        return {"run_id": run_id, "handler": _text(run.get("handler")),
                "status": "lost_claim", "settlement": "not ours", "error": ""}

    try:
        outcome = submit(run, base, timeout)
    except Exception as exc:
        outcome = {"provider_job_id": "",
                   "error": f"Submission crashed: {str(exc)[:300]}"}

    job_id = _text(outcome.get("provider_job_id"))
    if not job_id:
        # Nothing reached the provider, so nothing was spent: this settles
        # as an ordinary failure and the hold comes back in full.
        settled = _settle(base, run, succeeded=False)
        object_records.update_collection_record(
            RUNS, run_id,
            {"status": "failed",
             "error": _text(outcome.get("error")) or "Submission returned no job id.",
             "finished_at": _now(), "heartbeat_at": _now()},
            base_dir=base, actor=ACTOR)
        return {"run_id": run_id, "handler": _text(run.get("handler")),
                "status": "failed", "settlement": settled,
                "error": _text(outcome.get("error"))[:160]}

    # THE ORDERING THAT MATTERS: the job id is written before anything
    # else can happen to this run. Once a provider is holding the job,
    # the id is the only evidence that money may have been spent, and
    # poll_disposition reads it to refuse to abandon the run. A crash
    # between the call and this write is the one case that loses the
    # evidence, which is why submit returns the id rather than writing
    # it -- the window is as small as it can be made.
    object_records.update_collection_record(
        RUNS, run_id,
        {"status": object_template_runs.RUNNING_STATUS,
         "provider_job_id": job_id, "heartbeat_at": _now()},
        base_dir=base, actor=ACTOR)
    return {"run_id": run_id, "handler": _text(run.get("handler")),
            "status": "running", "settlement": "held", "error": "",
            "provider_job_id": job_id}


def _poll_one(base, run, timeout, pair):
    """Ask the provider whether a running job is done.

    Not done is not an error: it touches the heartbeat and returns, which
    is what stops the sweeper writing off a job that is legitimately
    still running. That heartbeat is the entire reason
    runner.stale_seconds must exceed the POLL interval rather than the
    job duration.
    """
    run_id = _text(run.get("id"))
    _submit, poll = pair
    try:
        outcome = poll(run, base, timeout)
    except Exception as exc:
        # A failed poll is not a failed job. The provider still has it,
        # so touch the heartbeat and try again next pass; only
        # max_run_seconds ends this loop, and it ends it at a human.
        object_records.update_collection_record(
            RUNS, run_id, {"heartbeat_at": _now()}, base_dir=base, actor=ACTOR)
        return {"run_id": run_id, "handler": _text(run.get("handler")),
                "status": "running", "settlement": "held",
                "error": f"Poll failed, still running: {str(exc)[:160]}"}

    if not outcome.get("done"):
        object_records.update_collection_record(
            RUNS, run_id, {"heartbeat_at": _now()}, base_dir=base, actor=ACTOR)
        return {"run_id": run_id, "handler": _text(run.get("handler")),
                "status": "running", "settlement": "held", "error": ""}

    settled = _settle(base, run, succeeded=bool(outcome.get("ok")))
    final = "succeeded" if outcome.get("ok") else "failed"
    object_records.update_collection_record(
        RUNS, run_id,
        {"status": final,
         "output": _text(outcome.get("output"))[:20000],
         "error": _text(outcome.get("error"))[:2000],
         "provider_cost_cents": str(_int(outcome.get("provider_cost_cents"))),
         "finished_at": _now(), "heartbeat_at": _now()},
        base_dir=base, actor=ACTOR)
    return {"run_id": run_id, "handler": _text(run.get("handler")),
            "status": final, "settlement": settled,
            "error": _text(outcome.get("error"))[:160]}


def _execute_one(base, run, timeout):
    run_id = _text(run.get("id"))

    try:
        _claim(base, run)
    except object_records.VersionConflictError:
        # Another pass claimed it between our read and our write. Not an
        # error and not worth a retry: the run is somebody else's now, and
        # the whole point is that we did NOT call the provider.
        return {"run_id": run_id, "handler": _text(run.get("handler")),
                "status": "lost_claim", "settlement": "not ours",
                "error": ""}

    handler = HANDLERS.get(_text(run.get("handler")))
    if handler is None:
        outcome = {"ok": False, "output": "",
                   "error": f"No handler named {_text(run.get('handler'))!r} "
                            f"is installed on this server.",
                   "provider_cost_cents": 0, "provider_job_id": ""}
    else:
        try:
            outcome = handler(run, base, timeout)
        except Exception as exc:
            outcome = {"ok": False, "output": "",
                       "error": f"The handler crashed: {str(exc)[:300]}",
                       "provider_cost_cents": 0, "provider_job_id": ""}

    settled = _settle(base, run, succeeded=bool(outcome["ok"]))

    final = "succeeded" if outcome["ok"] else "failed"
    try:
        object_records.update_collection_record(
            RUNS, run_id,
            {"status": final,
             "output": _text(outcome.get("output"))[:20000],
             "error": _text(outcome.get("error"))[:2000],
             "provider_cost_cents": str(_int(outcome.get("provider_cost_cents"))),
             "provider_job_id": _text(outcome.get("provider_job_id")),
             "finished_at": _now(), "heartbeat_at": _now()},
            base_dir=base, actor=ACTOR)
    except Exception as exc:
        # The one way this write legitimately fails: the sweeper decided
        # our heartbeat had stopped and marked the run `abandoned`, which
        # the transition map declares terminal -- so there is no move from
        # it to `succeeded`, by design. The work IS done and the money is
        # already right (the sweeper released the hold; `_settle` found
        # that marker and charged nothing), so this must not crash the
        # pass and take the rest of the batch with it. Report it: a run
        # that completed after being written off is a signal that
        # runner.stale_seconds is too short for this handler.
        return {"run_id": run_id, "handler": _text(run.get("handler")),
                "status": "finished_after_sweep", "settlement": settled,
                "error": (f"Completed, but the run had already been swept as "
                          f"abandoned, so its result could not be recorded "
                          f"({str(exc)[:120]}). Raise runner.stale_seconds.")}

    return {"run_id": run_id, "handler": _text(run.get("handler")),
            "status": final, "settlement": settled,
            "error": _text(outcome.get("error"))[:160]}


def EVENT(request):
    """The daemon entry point. EVENT with POST aliased, because the
    scheduler calls EVENT and declaring only POST is a pass that silently
    never runs -- a real production bug here, twice."""
    request = request or {}
    base = _base_dir()

    batch = max(1, _int(_setting(base, BATCH_SETTING, DEFAULT_BATCH),
                        DEFAULT_BATCH))
    timeout = max(5, _int(_setting(base, TIMEOUT_SETTING, DEFAULT_TIMEOUT),
                          DEFAULT_TIMEOUT))

    try:
        runs = object_records.read_collection_records(RUNS, base_dir=base)
    except Exception:
        return {"ok": True, "claimed": 0,
                "note": "No template_runs collection on this box."}

    queued = sorted((row for row in runs
                     if _text(row.get("status")) == "queued"),
                    key=lambda row: _text(row.get("created_at")))

    results = []
    for run in queued[:batch]:
        pair = ASYNC_HANDLERS.get(_text(run.get("handler")))
        results.append(_submit_one(base, run, timeout, pair) if pair
                       else _execute_one(base, run, timeout))

    # Then poll everything already at the provider. These are NOT new
    # claims -- the run is already ours -- so they are reported
    # separately and never counted as work this pass started.
    polled = []
    for run in runs:
        if _text(run.get("status")) != object_template_runs.RUNNING_STATUS:
            continue
        pair = ASYNC_HANDLERS.get(_text(run.get("handler")))
        if pair is None:
            continue          # handler uninstalled; the sweeper will escalate
        polled.append(_poll_one(base, run, timeout, pair))
    # `claimed` counts runs this pass actually OWNED and executed. A lost
    # claim is another pass's run, and counting it here would report two
    # passes as having done the same work -- the exact confusion the
    # compare-and-set exists to make impossible.
    executed = [r for r in results if r["status"] != "lost_claim"]
    lost = len(results) - len(executed)
    return {"ok": True, "claimed": len(executed), "lost_claims": lost,
            "queued_remaining": max(0, len(queued) - len(results)),
            "polled": len(polled), "poll_results": polled,
            "results": results}


POST = EVENT
