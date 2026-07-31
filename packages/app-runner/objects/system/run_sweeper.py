"""system_run_sweeper -- give the money back when a runner dies, and
never, ever run the job again.

A claimed run whose heartbeat has stopped is a run in an UNKNOWN state.
The pass that owned it may have died before calling the provider, or
after calling and before recording -- and in the second case the provider
may have completed the work and billed us. There is no way to know from
here, because the non-idempotent step belongs to somebody else's system.

So the sweep does the only two things that are safe in both worlds:

* mark the run `abandoned` -- a terminal state with no exits, declared so
  in the schema's transition map, so nothing can quietly re-queue it
* release the hold, in full, through the same idempotent settlement the
  runner uses -- the user pays nothing for a job nobody can prove
  happened, and the marker check means a runner that actually did settle
  before dying is not paid back twice

What it pointedly does NOT do is retry. Re-running a job that may already
have been billed is paying twice for one thing. A retry is a NEW run,
with a new hold, started by a person looking at the abandoned one --
`provider_job_id`, recorded before the call where the provider supports
it, is the thread that person pulls to ask "did they charge us".

Staleness comes from `runner.stale_seconds` (default 900). Generous on
purpose: this sweep is for processes that DIED, and a slow provider call
is not a dead process. Sweeping a run that is merely slow abandons work
the provider will finish and bill for -- so err long, and let genuinely
long-running handlers push their own heartbeats when they arrive.
"""

import os
from datetime import datetime, timezone

import object_records
import object_template_runs

ACTOR = "system_run_sweeper"
RUNS = "template_runs"

STALE_SETTING = "runner.stale_seconds"
MAX_RUN_SETTING = "runner.max_run_seconds"
# Deliberately generous: this is not "how long should a job take" but
# "how long before a machine gives up deciding and asks a person". Sora
# budgets thirty minutes; an hour leaves room for a provider queue.
DEFAULT_MAX_RUN_SECONDS = 3600
DEFAULT_STALE_SECONDS = 900


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


def EVENT(request):
    request = request or {}
    base = _base_dir()
    stale_seconds = max(60, _int(_setting(base, STALE_SETTING,
                                          DEFAULT_STALE_SECONDS),
                                 DEFAULT_STALE_SECONDS))
    now = _text(request.get("now")) or _now()

    try:
        runs = object_records.read_collection_records(RUNS, base_dir=base)
    except Exception:
        return {"ok": True, "swept": 0,
                "note": "No template_runs collection on this box."}

    try:
        entries = object_records.read_collection_records("wallet_entries",
                                                         base_dir=base)
    except Exception:
        entries = []

    max_run_seconds = max(stale_seconds, _int(_setting(base, MAX_RUN_SETTING,
                                                       DEFAULT_MAX_RUN_SECONDS),
                                              DEFAULT_MAX_RUN_SECONDS))

    swept, review = [], []
    # STEP 1 -- decide. Writing off a run is a COMPARE-AND-SET, because the
    # sweeper races the runner it is sweeping: a runner that heartbeats or
    # finishes between our staleness read and our write has changed the
    # row, so our rev no longer matches and we correctly lose. Without the
    # precondition this pass could write off a run that was completing and
    # hand back money for work that succeeded.
    #
    # WHICH decision is object_template_runs.poll_disposition's, not this
    # loop's, because the same rule has to bind the runner too. The rule
    # that matters: a run carrying a provider_job_id is NEVER abandoned.
    # Abandoning is what releases the hold, and a job the provider is
    # already holding may already have cost everything -- handing the
    # money back on a timer would be paying for it ourselves. Such a run
    # is polled instead (the provider can simply be asked), and if polling
    # never resolves it, it stops for a person.
    for run in runs:
        disposition = object_template_runs.poll_disposition(
            run, now=now, stale_seconds=stale_seconds,
            max_run_seconds=max_run_seconds)
        if disposition not in ("abandon", "review"):
            continue                  # "poll" belongs to the runner, not here
        run_id = _text(run.get("id"))
        if disposition == "abandon":
            changes = {
                "status": "abandoned",
                "error": (f"Abandoned: no heartbeat for over {stale_seconds}s "
                          f"and nothing was submitted to a provider, so "
                          f"nothing was spent. The hold is released in full. "
                          f"NOT retried -- a retry is a new run, started by a "
                          f"person."),
                "finished_at": now,
            }
        else:
            changes = {
                "status": object_template_runs.NEEDS_REVIEW_STATUS,
                "error": (f"Submitted to the provider (job "
                          f"{_text(run.get('provider_job_id')) or 'unknown'}) "
                          f"and still unresolved after {max_run_seconds}s. "
                          f"The hold is deliberately NOT released: this job "
                          f"may already have been charged, so releasing would "
                          f"give away what we paid and charging would bill for "
                          f"work that may never have arrived. A person decides."),
            }
        try:
            object_records.update_collection_record(
                RUNS, run_id, changes, base_dir=base, actor=ACTOR,
                expected_rev=object_records.compute_record_rev(run))
        except object_records.VersionConflictError:
            # It moved while we were looking at it -- alive after all.
            continue
        (swept if disposition == "abandon" else review).append(run_id)

    # STEP 2 -- pay back, driven by STATE rather than by "did step 1 just
    # run". Every terminal run whose hold has not been released gets it
    # released, so a crash between the two steps is repaired by the next
    # pass instead of leaving money reserved against a run that is over.
    # Idempotent by the release marker, so this is a no-op for the runs
    # the runner already settled itself.
    released = []
    for run in object_records.read_collection_records(RUNS, base_dir=base):
        if _text(run.get("status")) not in object_template_runs.TERMINAL_STATUSES:
            continue
        run_id = _text(run.get("id"))
        if object_template_runs.already_settled(run_id, entries):
            continue
        entries_written = object_template_runs.settlement(run, succeeded=False)
        if not entries_written:
            continue                       # a free run: nothing was held
        for entry in entries_written:
            object_records.create_collection_record(
                "wallet_entries", entry, base_dir=base, actor=ACTOR)
        entries.extend(entries_written)     # so a second hit this pass skips
        released.append(run_id)

    return {"ok": True, "swept": len(swept), "runs": swept,
            "needs_review": review,
            "holds_released": released, "stale_seconds": stale_seconds,
            "max_run_seconds": max_run_seconds}


POST = EVENT
