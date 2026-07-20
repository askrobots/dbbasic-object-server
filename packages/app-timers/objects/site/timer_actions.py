"""timer_actions -- 62 (Timer): server-enforced start/stop over time_logs.

plan/vocabulary/62-timer-spec.md. Turns the previously-inert `is_running`
flag into a real invariant: at most one running `time_logs` row per
`owner_id`, and a `duration_seconds` derived (never client-supplied) by
the stop verb. One small object serves three sub-paths -- same "route +
action param" shape app-views' view_render.py and this package's own
site_routes seed row use (`/timers/{action}` -> this object id,
`action` merged into the request payload by object_site_routes.py):

  POST /timers/start   {task_id?, notes?}    -> stop caller's running
                                                 timer if any, start a new one
  POST /timers/stop     {time_log_id?}       -> stop caller's running timer
                                                 (or the named one, if theirs)
  GET  /timers/running                       -> caller's running row, or null

Permissions posture: `permissions/rules.json` grants "public execute" on
this object id, same convention as every other site object in the suite
(app-theme's site_style, app-catalog's site_stock -- see
00-doctrine-and-contract.md's Block Contract) -- authorization happens
INSIDE the handler, not at the route: every action requires a signed-in
identity (401 otherwise) and every read/write is scoped to
`$user_id`/`_identity.user_id`, mirroring what the row-filtered
`time_logs` collection API (`{"owner_id": "$user_id"}` in this package's
own rules.json) would already enforce for a direct PUT. `owner_id` is
always server-set from `_identity`, never accepted from the request body
-- a caller cannot start or stop a timer "as" another owner.

Concurrency (63, plan/vocabulary/63-concurrency-spec.md): the write that
stops a timer always carries `expected_rev` (`object_records.
compute_record_rev` of the row just read), so a concurrent stop of the
same row fails closed (`VersionConflictError`) instead of double-stopping
-- "fails closed", not silently clobbering. What this does NOT fully
close (62's Storage section names this as the accepted v1 posture, not a
silent gap): two concurrent `POST /timers/start` calls from the SAME
owner could each read "no running timer" before the other's stop-write
commits, and both create a running row -- the classic read-check-write
TOCTOU. Per this repo's own build order, this file does not widen the
collection lock to close that gap (object_records.py's per-call
`.lock` scope is deliberately left alone); it is acceptable at
one-human/one-browser-tab concurrency and is exactly the kind of gap
`record_changes` makes fully visible and correctable if it ever happens,
not a corrupted or vanished record.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import object_records

DATA_DIR_ENV = "DBBASIC_DATA_DIR"
COLLECTION = "time_logs"
FEATURE_FLAGS_COLLECTION = "feature_flags"
TIMERS_ENABLED_FLAG = "timers_enabled"
ACTOR = "timer_actions"


def _data_dir() -> str:
    # Mirrors app-catalog's stock.py / app-invoices' invoice_totals.py own
    # _data_dir(): standalone, reads os.environ directly rather than
    # depending on object_server.
    return os.environ.get(DATA_DIR_ENV, object_records.DEFAULT_DATA_DIR)


def timers_enabled(base_dir) -> bool:
    """The block-wide kill switch, `<block>_enabled` convention (00-doctrine-
    and-contract.md), a `feature_flags` row named `timers_enabled`.

    Mirrors object_rollups.rollup_pass_enabled's exact posture: default ON
    (brownout kill switch, not an adoption gate). A missing row, a
    missing/unreadable `feature_flags` collection, and a blank value all
    resolve to "on"; only an explicit off/false/0/no value turns it off.
    """
    try:
        rows = object_records.read_collection_records(FEATURE_FLAGS_COLLECTION, base_dir=base_dir)
    except (LookupError, OSError, ValueError):
        return True
    for row in rows:
        if row.get("flag") == TIMERS_ENABLED_FLAG:
            value = (row.get("value") or "").strip().lower()
            if not value:
                return True
            return value not in {"off", "false", "0", "no"}
    return True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_dt(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _find_running(owner_id: str, base_dir: str) -> dict | None:
    """Return one owner's running row, or None.

    Expected scale is trivially small (a handful of rows per user per
    day, per 62's Storage section) -- a full read + filter, no index. If
    more than one running row exists for the owner (the documented
    start-race TOCTOU), pick the most recently started one
    deterministically rather than file order.
    """
    try:
        rows = object_records.read_collection_records(COLLECTION, base_dir=base_dir)
    except (LookupError, OSError, ValueError):
        return None
    running = [
        row for row in rows
        if row.get("owner_id") == owner_id
        and (row.get("is_running") or "").strip().lower() == "true"
    ]
    if not running:
        return None
    running.sort(key=lambda row: row.get("started_at") or "", reverse=True)
    return running[0]


def _stop_row(row: dict, *, base_dir: str) -> dict:
    """Stop one already-fetched running row: stamp ended_at/duration_seconds,
    compare-and-set against the row's own rev (63) so a concurrent stop of
    the same row raises VersionConflictError instead of double-stopping.
    """
    started = _parse_dt(row.get("started_at"))
    ended_dt = datetime.now(timezone.utc)
    duration_seconds = 0
    if started is not None:
        duration_seconds = max(0, int((ended_dt - started).total_seconds()))
    return object_records.update_collection_record(
        COLLECTION,
        row["id"],
        {
            "is_running": "false",
            "ended_at": ended_dt.isoformat().replace("+00:00", "Z"),
            "duration_seconds": str(duration_seconds),
        },
        base_dir=base_dir,
        actor=ACTOR,
        expected_rev=object_records.compute_record_rev(row),
    )


def _start(request: dict, user_id: str, base_dir: str) -> dict:
    task_id = str(request.get("task_id") or "").strip()
    notes = str(request.get("notes") or "").strip()

    running = _find_running(user_id, base_dir)
    if running is not None:
        # Open Question resolved per the task brief: starting auto-stops
        # the caller's existing timer rather than refusing. Swallow a
        # VersionConflictError here -- if a concurrent stop/start already
        # changed this row, the outcome this call wanted (the old row no
        # longer running) is either already true or this call is racing
        # something that will resolve it; either way it must not block
        # the new timer from starting.
        try:
            _stop_row(running, base_dir=base_dir)
        except object_records.VersionConflictError:
            pass

    record = object_records.create_collection_record(
        COLLECTION,
        {
            "owner_id": user_id,
            "task_id": task_id,
            "started_at": _now_iso(),
            "ended_at": "",
            "is_running": "true",
            "notes": notes,
        },
        base_dir=base_dir,
        actor=ACTOR,
    )
    _logger.info("timer started", owner_id=user_id, time_log_id=record.get("id", ""))
    return _ok({"time_log": record})


def _stop(request: dict, user_id: str, base_dir: str) -> dict:
    time_log_id = str(request.get("time_log_id") or "").strip()

    if time_log_id:
        try:
            row = object_records.get_collection_record(COLLECTION, time_log_id, base_dir=base_dir)
        except (object_records.RecordNotFoundError, object_records.InvalidRecordIdError):
            return _error(404, f"Time log not found: {time_log_id}")
        if row.get("owner_id") != user_id:
            return _error(403, "That timer belongs to another owner.")
        if (row.get("is_running") or "").strip().lower() != "true":
            return _error(409, "That timer is not running.")
    else:
        row = _find_running(user_id, base_dir)
        if row is None:
            return _error(409, "No running timer to stop.")

    try:
        updated = _stop_row(row, base_dir=base_dir)
    except object_records.VersionConflictError:
        return _error(409, "Timer changed before it could be stopped; try again.")

    _logger.info("timer stopped", owner_id=user_id, time_log_id=updated.get("id", ""))
    return _ok({"time_log": updated})


def _ok(data: dict) -> dict:
    body = {"status": "ok"}
    body.update(data)
    return {"content_type": "application/json", "body": json.dumps(body)}


def _error(status: int, message: str, *, code: str | None = None) -> dict:
    body = {"status": "error", "error": message}
    if code:
        body["error_code"] = code
    return {"content_type": "application/json", "status": status, "body": json.dumps(body)}


def GET(request):
    identity = request.get("_identity") or {}
    user_id = identity.get("user_id")
    if not user_id:
        return _error(401, "Sign in to use timers.")

    base_dir = _data_dir()
    if not timers_enabled(base_dir):
        return _error(400, "Timers are disabled.", code="timers_disabled")

    action = str(request.get("action") or "").strip()
    if action != "running":
        return _error(404, f"Unknown timer route: /timers/{action or ''}")

    return _ok({"time_log": _find_running(user_id, base_dir)})


def POST(request):
    identity = request.get("_identity") or {}
    user_id = identity.get("user_id")
    if not user_id:
        return _error(401, "Sign in to use timers.")

    base_dir = _data_dir()
    if not timers_enabled(base_dir):
        return _error(400, "Timers are disabled.", code="timers_disabled")

    action = str(request.get("action") or "").strip()
    if action == "start":
        return _start(request, user_id, base_dir)
    if action == "stop":
        return _stop(request, user_id, base_dir)
    return _error(404, f"Unknown timer route: /timers/{action or ''}")
