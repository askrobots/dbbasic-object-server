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

    swept = []
    for run in runs:
        if not object_template_runs.is_stale(run, now=now,
                                             stale_seconds=stale_seconds):
            continue
        run_id = _text(run.get("id"))

        released = "already settled"
        if not object_template_runs.already_settled(run_id, entries):
            for entry in object_template_runs.settlement(run, succeeded=False):
                object_records.create_collection_record(
                    "wallet_entries", entry, base_dir=base, actor=ACTOR)
            released = "released"

        object_records.update_collection_record(
            RUNS, run_id,
            {"status": "abandoned",
             "error": (f"Abandoned: no heartbeat for over {stale_seconds}s. "
                       f"The hold was {released}. NOT retried -- the provider "
                       f"call may already have been made; check "
                       f"provider_job_id before starting a new run."),
             "finished_at": now},
            base_dir=base, actor=ACTOR)
        swept.append({"run_id": run_id, "hold": released})

    return {"ok": True, "swept": len(swept), "runs": swept,
            "stale_seconds": stale_seconds}


POST = EVENT
