"""Change-log event dispatch -- the real fix for docs/logic-decisions.md #9.

The problem this closes: HANDLES-declared event handlers only fire from
object_server's synchronous HTTP write path (see object_handlers.py and
object_server._dispatch_event_handlers). A runner or an action that writes
through object_records directly never passes through that path, so its
write never triggers the reactions a handler would give an equivalent HTTP
write. Doctrine #9 tracked two workarounds that paid for this gap by hand
(action_apply_count composing its own inventory-variance journal;
action_resolve_bank_line composing the NSF bounce reversal) and said the
third instance must buy the real fix instead of a third workaround: a
daemon pass that dispatches handlers from the record-change log, so a
write reaches its reactions no matter which path wrote it -- the same
shape object_daemon.process_notifications already uses to avoid depending
on synchronous dispatch for notify_rules.

Payload parity with the HTTP path (read this twice, it bit production
once): object_server's dispatcher builds

    {"event": "<collection>.record.<created|updated|deleted>",
     "collection": collection, "record_id": record_id, "action": action}

where ``action`` is the RAW verb from the write ("create"/"update"/
"delete") and ``event`` uses the past-participle form. A handler that
mixed the two up (checking ``action == "created"`` instead of mapping
"create" -> "created" first) silently matched nothing and skipped every
entry in production -- see the comment in
packages/app-payments/objects/system/books.py's EVENT(). This module
builds the exact same shape from the exact same raw fields a record-change
entry already carries (object_record_changes stores the raw action
untouched), so a handler cannot tell which path dispatched it.

Double-dispatch, honestly: an HTTP write already dispatched its handlers
synchronously before this pass ever sees the resulting change-log entry.
This module has no way to know that happened -- object_record_changes
does not record whether HANDLES already ran for an entry, and this file
is not allowed to touch object_server.py to add that bookkeeping. So this
pass does NOT prevent a handler from running twice across the two paths.
What it promises instead is the same thing doctrine #7 already requires
of every composer here: idempotency by provenance (a generated_from
marker such as "payments/{id}" or "payments/{id}:bounced"), which is
exactly how action_apply_count and action_resolve_bank_line stay correct
under the two-workaround world already. At-least-once is therefore the
right guarantee to promise here, not exactly-once -- a handler that isn't
idempotent under redelivery is a bug in the handler, not in this
dispatcher.

What this module DOES guard against is re-firing the SAME (change_id,
handler_object_id) pair across this pass's own polls -- a cursor file
(same posture as process_notifications: first run stamps "now" and never
backfills history) plus a small marker file recording pairs dispatched
since the cursor last advanced. The marker exists because the cursor only
advances once per successful batch; without it, a crash between
dispatching entry 3 of 10 and persisting the new cursor would replay
entries 1-10 (not just 4-10) on the next poll. The marker file is cleared
every time the cursor successfully advances, so it never grows past one
in-flight batch.

Finally, this whole pass is opt-in: DBBASIC_ENABLE_CHANGE_DISPATCH is
unset (OFF) by default, mirroring object_handlers.HANDLERS_ENABLED_ENV's
posture that a dispatch behavior this new should be a deliberate operator
act, not a silent default.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import object_collections
import object_handlers
import object_record_changes
from object_execution import ObjectExecutionRequest, execute_object
from python_object_runtime import PythonObjectRuntime

ENABLE_ENV = "DBBASIC_ENABLE_CHANGE_DISPATCH"
_TRUE_VALUES = {"1", "true", "yes", "on"}

CURSOR_FILE_NAME = ".change_dispatch_cursor"
MARKER_FILE_NAME = ".change_dispatch_dispatched"

DEFAULT_LIMIT = 200

# One runtime, module-level, mirrors object_server.py's `_runtime =
# PythonObjectRuntime()` and the test fixtures' `RUNTIME =
# python_object_runtime.PythonObjectRuntime()` -- cheap to construct, no
# per-call state worth re-creating on every poll.
_RUNTIME = PythonObjectRuntime()


def change_dispatch_enabled(*, base_dir: Path | str) -> bool:
    """Return True when the change-log dispatch pass may run.

    Env-only and OFF by default -- unlike object_notify's
    notify_pass_enabled (a feature_flags row, default ON: a brownout lever
    for a feature already adopted), this pass makes an installed handler
    start reacting to writes that never used to reach it. That is a
    behavior change for existing objects, not a lever on an existing one,
    so it needs an explicit operator opt-in rather than a data default.
    ``base_dir`` is accepted for signature symmetry with the other
    ``*_enabled(*, base_dir)`` checks in this codebase; this particular
    check does not read anything under it.
    """
    value = os.environ.get(ENABLE_ENV, "")
    return value.strip().lower() in _TRUE_VALUES


def dispatch_pending(
    *,
    base_dir: Path | str,
    roots: Iterable[Path] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Dispatch HANDLES handlers for record-change entries newer than the cursor.

    Returns {"processed", "dispatched", "skipped", "errors", "results"}:

    - processed: change-log entries examined this pass (across every
      collection any installed handler declares interest in).
    - dispatched: handler EVENT calls actually attempted (success or
      failure both count -- this is "how much work happened", not "how
      much succeeded").
    - skipped: entries whose event had no matching handler, plus
      (change_id, handler_id) pairs already recorded as dispatched by an
      earlier, not-yet-committed pass over the same batch.
    - errors: dispatched calls whose handler raised or returned a
      non-ok ObjectExecutionResult.
    - results: one dict per attempted dispatch (change_id, collection,
      record_id, action, event, handler_id, ok, error).

    Gracefully returns the all-zero shape (never raises) when: the pass is
    disabled, no installed object declares a HANDLES entry shaped like
    "<collection>.record.<created|updated|deleted>", or a collection has
    no change log yet. One raising handler, or one unreadable change-log
    file, never stops the rest of the batch -- same posture as
    object_daemon.process_notifications.
    """
    base_dir = Path(base_dir)
    result: dict[str, Any] = {
        "processed": 0,
        "dispatched": 0,
        "skipped": 0,
        "errors": 0,
        "results": [],
    }

    if not change_dispatch_enabled(base_dir=base_dir):
        return result

    try:
        index = object_handlers.build_index(roots)
    except Exception:
        # Source discovery is best-effort here -- a broken package source
        # elsewhere must not stop this pass either.
        return result

    collections = _collections_with_record_handlers(index)
    if not collections:
        return result

    cursor_path = base_dir / CURSOR_FILE_NAME
    try:
        cursor = cursor_path.read_text(encoding="utf-8").strip()
    except OSError:
        cursor = ""

    if not cursor:
        # First run: stamp "now" and dispatch only FUTURE changes. A brand
        # new install (or a flag flipped on long after go-live) must never
        # replay years of history through every handler the moment it
        # turns on.
        now_iso = datetime.now(timezone.utc).isoformat()
        cursor_path.parent.mkdir(parents=True, exist_ok=True)
        cursor_path.write_text(now_iso, encoding="utf-8")
        return result

    fresh: list[dict[str, Any]] = []
    for collection in collections:
        try:
            payload = object_record_changes.list_record_changes(
                collection, base_dir=base_dir, limit=limit
            )
        except (object_collections.InvalidCollectionNameError, OSError, ValueError):
            continue
        for change in payload.get("changes") or []:
            ts = str(change.get("timestamp") or "")
            if ts > cursor:
                fresh.append(change)
    fresh.sort(key=lambda c: str(c.get("timestamp") or ""))

    result["processed"] = len(fresh)
    if not fresh:
        return result

    marker_path = base_dir / MARKER_FILE_NAME
    dispatched_pairs = _load_markers(marker_path)

    max_ts = cursor
    for change in fresh:
        # Per-entry isolation: a malformed change or a handler exception
        # must never stop the rest of the batch or prevent the cursor from
        # advancing past everything that DID work.
        try:
            ts = str(change.get("timestamp") or "")
            if ts > max_ts:
                max_ts = ts

            collection = str(change.get("collection") or "")
            record_id = str(change.get("record_id") or "")
            action = str(change.get("action") or "")
            change_id = str(change.get("change_id") or "")

            event = object_handlers.event_name(collection, action)
            handler_ids = index.get(event) if event else None
            if not handler_ids:
                result["skipped"] += 1
                continue

            # Same signal-shaped payload object_server._dispatch_event_handlers
            # builds: no record body, RAW action, participle event name.
            event_payload = {
                "event": event,
                "collection": collection,
                "record_id": record_id,
                "action": action,
            }

            for handler_id in handler_ids:
                pair_key = f"{change_id}\t{handler_id}"
                if pair_key in dispatched_pairs:
                    result["skipped"] += 1
                    continue

                entry: dict[str, Any] = {
                    "change_id": change_id,
                    "collection": collection,
                    "record_id": record_id,
                    "action": action,
                    "event": event,
                    "handler_id": handler_id,
                    "ok": False,
                    "error": None,
                }
                try:
                    request = ObjectExecutionRequest(
                        object_id=handler_id,
                        method="EVENT",
                        payload=event_payload,
                        correlation_id=change.get("correlation_id") or None,
                    )
                    exec_result = execute_object(_RUNTIME, request, roots)
                    entry["ok"] = bool(exec_result.ok)
                    if not exec_result.ok and exec_result.error is not None:
                        entry["error"] = exec_result.error.message
                except Exception as exc:  # noqa: BLE001 -- one bad handler must not stop others
                    entry["ok"] = False
                    entry["error"] = str(exc)[:200]

                result["dispatched"] += 1
                if not entry["ok"]:
                    result["errors"] += 1
                result["results"].append(entry)
                dispatched_pairs.add(pair_key)
                _append_marker(marker_path, pair_key)
        except Exception:  # noqa: BLE001 -- one bad change must not stop the batch
            continue

    if max_ts != cursor:
        cursor_path.write_text(max_ts, encoding="utf-8")
        # The whole batch between the old and new cursor just committed;
        # nothing in it can ever be re-read (list_record_changes only
        # returns entries newer than the cursor), so the markers guarding
        # against a mid-batch crash are no longer needed.
        _clear_markers(marker_path)

    return result


def _collections_with_record_handlers(index: dict[str, list[str]]) -> list[str]:
    """Return the sorted set of collections any HANDLES entry names.

    Only "<collection>.record.<created|updated|deleted>" shaped events are
    record-change events this pass can act on; anything else in HANDLES
    (a future, non-record event kind) is silently not our concern here.
    """
    participles = {"created", "updated", "deleted"}
    collections: set[str] = set()
    for event in index:
        parts = event.split(".record.", 1)
        if len(parts) != 2 or parts[1] not in participles or not parts[0]:
            continue
        collections.add(parts[0])
    return sorted(collections)


def _load_markers(path: Path) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    return {line for line in text.splitlines() if line.strip()}


def _append_marker(path: Path, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(key + "\n")


def _clear_markers(path: Path) -> None:
    try:
        path.write_text("", encoding="utf-8")
    except OSError:
        pass
