#!/usr/bin/env python3
"""
Object Primitive Daemon — Scheduler, Queue, Events

Background worker that polls trigger objects and executes target objects.

Runs alongside the HTTP server, sharing file-based state (TSV).
Does not require HTTP or auth — executes objects directly via ObjectRuntime.

Usage:
    python object_daemon.py
    python object_daemon.py --interval 5   # poll every 5 seconds
"""
from __future__ import annotations

import json
import os
import signal
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dbbasic_object_core.runtime.object_runtime import ObjectRuntime

try:
    from croniter import croniter
except ImportError:
    croniter = None

import object_collections
import object_connectors
import object_email
import object_events
import object_materialize
import object_notify
import object_packages
import object_record_changes
import object_records
import object_rollups
from object_execution import ObjectExecutionFailure, ObjectExecutionRequest, execute_object
from object_namespace import find_trigger_file, get_object_roots, resolve_object_id

# Daemon state
_running = True
EVENT_KEEP_COUNT_ENV = "DBBASIC_EVENT_KEEP_COUNT"
EVENT_KEEP_SECONDS_ENV = "DBBASIC_EVENT_KEEP_SECONDS"
EVENT_CLEANUP_INTERVAL_SECONDS = 60.0

# Compaction pass (docs/append-only-storage-design.md Compaction: "run on
# a schedule or when the superseded-row ratio passes a threshold -- never
# inline in a request"). See process_compactions.
COMPACTION_INTERVAL_SECONDS_ENV = "DBBASIC_COMPACTION_INTERVAL_SECONDS"
COMPACTION_BLOAT_RATIO_ENV = "DBBASIC_COMPACTION_BLOAT_RATIO"
_DEFAULT_COMPACTION_INTERVAL_SECONDS = 3600
_DEFAULT_COMPACTION_BLOAT_RATIO = 1.0
COMPACTION_MARKER_NAME = ".compaction_last_run"

# Stale-state auto-transition pass. The predecessor system's "48-hour
# auto-approve" job (waiting_on_client -> approved on tasks) was written
# but never scheduled anywhere, silently stranding 200+ records in a
# waiting state forever. To make that class of mistake impossible here,
# this pass ships ON by default -- see _DEFAULT_AUTO_TRANSITION_RULES --
# and requires no setup beyond running the daemon. See
# process_stale_transitions.
AUTO_TRANSITION_RULES_ENV = "DBBASIC_AUTO_TRANSITION_RULES"
AUTO_TRANSITION_INTERVAL_SECONDS_ENV = "DBBASIC_AUTO_TRANSITION_INTERVAL_SECONDS"
_DEFAULT_AUTO_TRANSITION_INTERVAL_SECONDS = 3600
AUTO_TRANSITION_MARKER_NAME = ".auto_transition_last_run"
AUTO_TRANSITION_ACTOR = "daemon:auto-transition"
_DEFAULT_AUTO_TRANSITION_RULES = json.dumps([
    {"collection": "tasks", "field": "status", "from": "waiting_on_client",
     "to": "approved", "after_hours": 48},
])

# Collections named in a rule but not found on disk (the owning package
# isn't installed) are logged once per process, not once per poll --
# see _apply_auto_transition_rule.
_WARNED_UNKNOWN_AUTO_TRANSITION_COLLECTIONS: set[str] = set()

# Materialize pass (plan/vocabulary/61-materialize-spec.md, object_materialize.py).
# Unlike process_rollups (whose due-gate lives entirely on each
# rollup_definitions row -- see process_rollups' own docstring),
# materialize's Events section explicitly asks for process_stale_
# transitions' marker-file-gated shape instead: "riding the exact
# marker-file-gated, try/except-per-item pattern process_stale_
# transitions already establishes." See process_materializations.
MATERIALIZE_INTERVAL_SECONDS_ENV = "DBBASIC_MATERIALIZE_INTERVAL_SECONDS"
_DEFAULT_MATERIALIZE_INTERVAL_SECONDS = 3600
MATERIALIZE_MARKER_NAME = ".materialize_last_run"
# 12 notify: the cursor file holds the ISO timestamp of the last record-change
# entry process_notifications has already turned into notifications. Unlike the
# other passes' interval markers, this is a POSITION (content), not a clock
# (mtime) -- the pass runs every poll for near-instant delivery and advances
# the cursor past what it processed. First run stamps it at "now" so a fresh
# install never backfills notifications for the entire change history.
NOTIFY_CURSOR_NAME = ".notify_cursor"

# Definitions referencing a source/output/child collection that doesn't
# exist yet (the owning package isn't installed) are logged once per
# process, not once per poll -- mirrors _WARNED_UNKNOWN_AUTO_TRANSITION_
# COLLECTIONS exactly (61's Degradation section names this precedent).
_WARNED_UNKNOWN_MATERIALIZE_COLLECTIONS: set[str] = set()


def log(msg, level='INFO'):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] [{level}] {msg}")


def _object_roots() -> list[Path]:
    """Return object source roots in lookup order."""
    return get_object_roots()


def _find_trigger_file(trigger_name: str) -> Path | None:
    """Find a trigger object in the configured object roots."""
    return find_trigger_file(trigger_name)


# --- Scheduler ---

def process_scheduler(runtime: ObjectRuntime):
    """Check scheduler for due tasks and execute them."""
    scheduler_file = _find_trigger_file('scheduler')
    if scheduler_file is None:
        return

    obj = runtime.load_object(scheduler_file)  # ID = 'scheduler' (filename stem)
    obj.state_manager.reload()  # Re-read from disk (server may have written new tasks)
    state = obj.state_manager.get_all()
    now = int(time.time())

    for key, value in list(state.items()):
        if not key.startswith('task_'):
            continue

        try:
            task = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            continue

        if task.get('status') != 'active':
            continue

        # Calculate next_run if not set
        next_run = task.get('next_run')
        if next_run is None:
            next_run = _calculate_next_run(task)
            if next_run is None:
                continue
            task['next_run'] = next_run
            obj.state_manager.set(key, json.dumps(task))

        if next_run > now:
            continue

        # Task is due — execute target
        target_id = task.get('object_id')
        method = task.get('method', 'POST')
        payload = task.get('payload', {})

        if not target_id:
            continue

        log(f"Scheduler: executing {target_id}.{method} (task {task['id']})")

        try:
            _execute_target(runtime, target_id, method, payload)
            log(f"Scheduler: {target_id}.{method} completed")
        except Exception as e:
            log(f"Scheduler: {target_id}.{method} failed: {e}", 'ERROR')

        # Update task
        task['last_run'] = now
        task['run_count'] = task.get('run_count', 0) + 1

        if task.get('type') == 'onetime':
            task['status'] = 'completed'
            task['next_run'] = None
        else:
            task['next_run'] = _calculate_next_run(task, after=now)

        obj.state_manager.set(key, json.dumps(task))


def _calculate_next_run(task, after=None):
    """Calculate next run time for a task."""
    schedule = task.get('schedule', '')
    task_type = task.get('type', '')

    if after is None:
        after = int(time.time())

    if task_type == 'cron' and croniter:
        try:
            cron = croniter(schedule, after)
            return int(cron.get_next(float))
        except (ValueError, KeyError):
            return None

    elif task_type == 'onetime':
        try:
            dt = datetime.fromisoformat(schedule.replace('Z', '+00:00'))
            return int(dt.timestamp())
        except (ValueError, TypeError):
            return None

    return None


# --- Queue ---

def process_queue(runtime: ObjectRuntime, max_messages=10):
    """Dequeue messages and execute target objects."""
    queue_file = _find_trigger_file('queue')
    if queue_file is None:
        return

    obj = runtime.load_object(queue_file)  # ID = 'queue' (filename stem)
    obj.state_manager.reload()  # Re-read from disk
    state = obj.state_manager.get_all()
    now = int(time.time())

    # Find pending, visible messages
    messages = []
    for key, value in state.items():
        if not key.startswith('msg_'):
            continue

        try:
            msg = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            continue

        if msg.get('status') != 'pending':
            continue

        # Check expiration
        if msg.get('expires_at', float('inf')) < now:
            msg['status'] = 'expired'
            obj.state_manager.set(key, json.dumps(msg))
            continue

        # Check visibility
        if msg.get('visible_after', 0) > now:
            continue

        messages.append((key, msg))

    # Sort by priority (highest first), then timestamp (oldest first)
    messages.sort(key=lambda m: (-m[1].get('priority_level', 2), m[1].get('created_at', 0)))

    # Process up to max_messages
    for key, msg in messages[:max_messages]:
        body = msg.get('message', {})
        if not isinstance(body, dict):
            continue

        target_id = body.get('object_id')
        if not target_id:
            continue

        method = body.get('method', 'POST')
        payload = body.get('payload', {})

        log(f"Queue: executing {target_id}.{method} (msg {msg['id']}, queue {msg.get('queue_name')})")

        # Mark as processing
        msg['status'] = 'processing'
        msg['dequeued_at'] = now
        obj.state_manager.set(key, json.dumps(msg))

        try:
            _execute_target(runtime, target_id, method, payload)
            msg['status'] = 'completed'
            msg['completed_at'] = int(time.time())
            log(f"Queue: {target_id}.{method} completed")
        except Exception as e:
            log(f"Queue: {target_id}.{method} failed: {e}", 'ERROR')
            msg['attempts'] = msg.get('attempts', 0) + 1
            if msg['attempts'] >= msg.get('max_attempts', 3):
                msg['status'] = 'failed'
                msg['failed_at'] = int(time.time())
            else:
                msg['status'] = 'pending'
                msg['visible_after'] = int(time.time()) + (2 ** msg['attempts'])

        obj.state_manager.set(key, json.dumps(msg))


# --- Events ---

def process_events(runtime: ObjectRuntime):
    """Deliver events to subscribers via callback URLs."""
    events_file = _find_trigger_file('events')
    if events_file is None:
        return

    obj = runtime.load_object(events_file)  # ID = 'events' (filename stem)
    obj.state_manager.reload()  # Re-read from disk
    state = obj.state_manager.get_all()

    # Collect subscriptions and events
    subscriptions = {}
    events = {}

    for key, value in state.items():
        try:
            data = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            continue

        if key.startswith('sub_'):
            callback = data.get('callback_url')
            if callback:
                subscriptions[key] = data
        elif key.startswith('event_'):
            events[key] = data

    if not subscriptions or not events:
        return

    # Sort events by timestamp
    sorted_events = sorted(events.values(), key=lambda e: e.get('timestamp', 0))

    for sub_key, sub in subscriptions.items():
        event_type = sub.get('event_type')
        last_event_id = sub.get('last_event_id')
        callback_url = sub.get('callback_url')

        # Find events for this subscription
        matching = [e for e in sorted_events if e.get('event_type') == event_type]

        # Skip events already delivered
        if last_event_id:
            found = False
            pending = []
            for e in matching:
                if found:
                    pending.append(e)
                elif e.get('id') == last_event_id:
                    found = True
            if not found:
                # last_event_id not found, deliver all
                pending = matching
        else:
            pending = matching

        if not pending:
            continue

        # Deliver events
        for event in pending:
            status_code = None
            try:
                req = urllib.request.Request(
                    callback_url,
                    data=json.dumps(event).encode('utf-8'),
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if hasattr(resp, 'getcode'):
                        status_code = resp.getcode()
                    else:
                        status_code = getattr(resp, 'status', None)
                log(f"Events: delivered {event['event_type']} ({event['id']}) to {sub['id']}")
                sub = object_events.record_subscription_delivery(
                    sub,
                    event,
                    success=True,
                    status_code=status_code,
                )
                obj.state_manager.set(sub_key, json.dumps(sub))
            except (urllib.error.URLError, OSError) as e:
                status_code = getattr(e, 'code', None)
                log(f"Events: failed to deliver to {callback_url}: {e}", 'WARN')
                sub = object_events.record_subscription_delivery(
                    sub,
                    event,
                    success=False,
                    status_code=status_code,
                    error=str(e),
                )
                obj.state_manager.set(sub_key, json.dumps(sub))
                break


# --- Rate Limit Cleanup ---

def cleanup_ratelimit(max_age=120, *, base_dir: Path | str = "data"):
    """Delete rate limit files older than max_age seconds."""
    ratelimit_dir = Path(base_dir) / 'ratelimit'
    if not ratelimit_dir.exists():
        return

    now = time.time()
    cutoff = now - max_age
    cleaned = 0

    for f in ratelimit_dir.iterdir():
        if not f.is_file() or not f.name.endswith('.txt'):
            continue
        try:
            # Check if file has any recent timestamps
            has_recent = False
            for line in f.read_text().strip().split('\n'):
                if line:
                    ts = float(line)
                    if ts > cutoff:
                        has_recent = True
                        break
            if not has_recent:
                f.unlink()
                cleaned += 1
        except (ValueError, IOError, OSError):
            # Corrupt or unreadable — safe to remove
            try:
                f.unlink()
                cleaned += 1
            except OSError:
                pass

    if cleaned:
        log(f"Cleanup: removed {cleaned} expired rate limit file(s)")


def cleanup_events(
    *,
    base_dir: Path | str = "data",
    keep_count: int | None = None,
    keep_seconds: int | None = None,
):
    """Prune old event queue rows while preserving subscriptions."""
    if keep_count is None:
        keep_count = _env_int(EVENT_KEEP_COUNT_ENV, object_events.DEFAULT_EVENT_KEEP_COUNT)
    if keep_seconds is None:
        keep_seconds = _env_int(EVENT_KEEP_SECONDS_ENV, object_events.DEFAULT_EVENT_KEEP_SECONDS)

    result = object_events.prune_events(
        base_dir=base_dir,
        keep_count=keep_count,
        keep_seconds=keep_seconds,
    )
    if result["deleted"]:
        log(f"Cleanup: removed {result['deleted']} expired event row(s)")
    return result


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


# --- Compaction ---

def process_compactions(*, base_dir: Path | str = "data") -> dict | None:
    """Compact over-threshold append-mode collections on a timer.

    Runs at most once per DBBASIC_COMPACTION_INTERVAL_SECONDS (default
    3600 -- an hour), gated by a marker FILE (`.compaction_last_run` under
    `base_dir`) rather than an in-process variable like the event-cleanup
    pass above uses: a file marker survives a daemon restart, and if more
    than one daemon process ever points at the same data dir, whichever
    one's write lands last simply wins that interval's marker -- there is
    no harm in an occasional double-run of a compaction that is itself
    idempotent-per-collection (compact_collection on an already-compacted
    file is a correctly-reported no-op).

    Every collection whose schema currently declares "storage": "append"
    (object_records.list_append_collection_stats) is compacted when BOTH:
      - its physical row count is at or above DBBASIC_APPEND_COMPACT_
        MIN_ROWS (object_records.append_compact_min_rows -- the same
        floor object_records._maybe_flag_auto_compact uses for its own
        inline auto-compact trigger on ordinary writes), AND
      - its bloat_ratio is at or above DBBASIC_COMPACTION_BLOAT_RATIO
        (default 1.0 -- dead rows at least matching live rows).
    A stats entry this call can't get real numbers for (list_append_
    collection_stats defaults to allow_fold=True, so this is not expected
    in practice, but a None/estimated physical_rows or bloat_ratio is
    skipped rather than guessed at) is left alone; it will be reconsidered
    on the next interval.

    Each collection's compaction is wrapped in its own try/except: one
    collection failing (a lock contention, a mid-flight schema change, a
    permissions error) is logged and skipped, never allowed to stop the
    rest of the pass or propagate out of this function -- the daemon is
    an optional process (nothing else may depend on it running), and a
    single bad collection must not take an entire poll interval's worth
    of compaction down with it. The daemon's own main loop wraps this
    call in a try/except too, belt and suspenders.

    Returns None when the interval hasn't elapsed yet (no work attempted
    this call). Otherwise returns {"checked": <int>, "compacted": [{
    "collection", "rows_before", "rows_after", "bytes_before",
    "bytes_after"}, ...]} -- mainly for tests and manual/CLI invocation;
    the daemon's own loop only logs.
    """
    interval = _env_int(COMPACTION_INTERVAL_SECONDS_ENV, _DEFAULT_COMPACTION_INTERVAL_SECONDS)
    marker_path = Path(base_dir) / COMPACTION_MARKER_NAME
    now = time.time()
    try:
        last_run = marker_path.stat().st_mtime
    except OSError:
        last_run = 0.0

    if now - last_run < interval:
        return None

    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(str(now))

    bloat_threshold = _env_float(COMPACTION_BLOAT_RATIO_ENV, _DEFAULT_COMPACTION_BLOAT_RATIO)
    min_rows = object_records.append_compact_min_rows()

    try:
        stats = object_records.list_append_collection_stats(base_dir=base_dir)
    except Exception as e:
        log(f"Compaction: could not list append collections: {e}", "ERROR")
        return {"checked": 0, "compacted": []}

    compacted = []
    for entry in stats:
        collection = entry.get("collection")
        try:
            physical_rows = entry.get("physical_rows")
            bloat_ratio = entry.get("bloat_ratio")
            if physical_rows is None or bloat_ratio is None:
                continue
            if physical_rows < min_rows or bloat_ratio < bloat_threshold:
                continue

            summary = object_records.compact_collection(collection, base_dir=base_dir)
            log(
                f"Compaction: {collection} rows {summary['rows_before']}->"
                f"{summary['rows_after']}, bytes {summary['bytes_before']}->"
                f"{summary['bytes_after']}"
            )
            compacted.append({"collection": collection, **summary})
        except Exception as e:
            log(f"Compaction: {collection} failed: {e}", "ERROR")
            continue

    return {"checked": len(stats), "compacted": compacted}


# --- Stale-state auto-transition ---

def auto_transition_rules_from_env() -> list[dict]:
    """Parse DBBASIC_AUTO_TRANSITION_RULES into a list of clean rule dicts.

    Unset -> _DEFAULT_AUTO_TRANSITION_RULES (tasks' waiting_on_client ->
    approved after 48h). Set to "" or "[]" -> [] (the pass runs every
    interval, finds nothing configured, and logs a zero-work summary
    line rather than silently vanishing -- see process_stale_transitions).
    A malformed entry (missing string field, unparseable after_hours) is
    dropped with a warning rather than aborting the whole list; a
    malformed *list* (bad JSON, not a JSON array) disables every rule and
    is logged loudly, since silently falling back to the default here
    would hide a real configuration mistake.
    """
    raw = os.environ.get(AUTO_TRANSITION_RULES_ENV)
    if raw is None:
        raw = _DEFAULT_AUTO_TRANSITION_RULES
    text = raw.strip()
    if not text:
        return []

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        log(f"Auto-transition: {AUTO_TRANSITION_RULES_ENV} is not valid JSON, disabling: {e}", "ERROR")
        return []
    if not isinstance(parsed, list):
        log(f"Auto-transition: {AUTO_TRANSITION_RULES_ENV} must be a JSON list, disabling", "ERROR")
        return []

    rules = []
    for entry in parsed:
        if not isinstance(entry, dict):
            log(f"Auto-transition: skipping non-object rule: {entry!r}", "WARN")
            continue
        collection = entry.get("collection")
        field = entry.get("field")
        from_value = entry.get("from")
        to_value = entry.get("to")
        if not all(isinstance(v, str) and v for v in (collection, field, from_value, to_value)):
            log(f"Auto-transition: skipping malformed rule (missing collection/field/from/to): {entry!r}", "WARN")
            continue
        try:
            after_hours = float(entry.get("after_hours"))
        except (TypeError, ValueError):
            log(f"Auto-transition: skipping rule with invalid after_hours: {entry!r}", "WARN")
            continue
        rules.append({
            "collection": collection,
            "field": field,
            "from": from_value,
            "to": to_value,
            "after_hours": after_hours,
        })
    return rules


def _parse_iso_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _latest_field_entry_timestamps(
    collection: str, field: str, from_value: str, *, base_dir: Path | str
) -> dict[str, str]:
    """Best-effort record id -> timestamp of the most recent change that
    set `field` to `from_value`, built from ONE sequential read of the
    collection's record-change log (object_record_changes.CHANGES_FILE).

    This is the most accurate signal available for "how long has this
    record been sitting in this state": it answers exactly the question
    the FSM cares about, unlike a record's own `created_at` (when the row
    was first made, not when it entered THIS state) or a generic
    `updated_at` (bumped by any field edit, not just this one). It only
    stays cheap because it is read once per rule per pass -- not once per
    candidate record -- and the log is append-ordered, so a later entry
    for the same id simply overwrites an earlier one as this walks the
    file forward, leaving the most recent one behind.

    Returns {} when the log doesn't exist yet or can't be read; callers
    fall back to the record's own fields in that case (see
    _record_age_seconds).
    """
    try:
        path = object_record_changes.record_changes_file(collection, base_dir=base_dir)
    except object_collections.InvalidCollectionNameError:
        return {}
    if not path.exists():
        return {}

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}

    timestamps: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        after = entry.get("after")
        if not isinstance(after, dict) or after.get(field) != from_value:
            continue
        record_id = entry.get("record_id")
        timestamp = entry.get("timestamp")
        if isinstance(record_id, str) and isinstance(timestamp, str):
            timestamps[record_id] = timestamp
    return timestamps


def _record_age_seconds(
    record: dict[str, str],
    record_id: str,
    change_timestamps: dict[str, str],
    *,
    now: datetime,
) -> float | None:
    """Return how long `record` has held its current value, in seconds, or
    None when no usable timestamp exists at all (caller skips the record
    rather than guessing).

    Preference order, documented honestly: the record-change log's entry
    for the change that set this record's field to its current value
    (most accurate -- see _latest_field_entry_timestamps), then an
    explicit `updated_at` field on the record itself (cheap, no schema in
    this codebase declares one yet, but a future one might), then
    `created_at` (tasks.json's only timestamp field today -- a real but
    honestly-worse proxy, since it marks row creation, not state entry).
    """
    for source in (
        change_timestamps.get(record_id),
        record.get("updated_at"),
        record.get("created_at"),
    ):
        parsed = _parse_iso_timestamp(source)
        if parsed is not None:
            return (now - parsed).total_seconds()
    return None


def _apply_auto_transition_rule(rule: dict, *, base_dir: Path | str) -> int:
    """Apply one rule and return the count of records actually moved.

    Every failure mode here is a skip, never an abort: an unknown
    collection (the owning package isn't installed) is logged once and
    skipped; a record with no usable timestamp is skipped; a record whose
    live value has already moved on since it was listed is skipped; and a
    write rejected by the schema (e.g. a transitions map that no longer
    allows this move) is logged and skipped, matching process_compactions'
    per-item isolation posture -- one bad record must never stop the rest
    of the rule, and one bad rule must never stop the rest of the pass.
    """
    collection = rule["collection"]
    field = rule["field"]
    from_value = rule["from"]
    to_value = rule["to"]
    after_hours = rule["after_hours"]

    try:
        records = object_records.read_collection_records(collection, base_dir=base_dir)
    except (object_collections.CollectionNotFoundError, object_collections.InvalidCollectionNameError):
        if collection not in _WARNED_UNKNOWN_AUTO_TRANSITION_COLLECTIONS:
            log(
                f"Auto-transition: collection '{collection}' not found (package not "
                f"installed?), skipping rule -- will keep checking, logged once",
                "WARN",
            )
            _WARNED_UNKNOWN_AUTO_TRANSITION_COLLECTIONS.add(collection)
        return 0

    _WARNED_UNKNOWN_AUTO_TRANSITION_COLLECTIONS.discard(collection)

    candidates = [r for r in records if r.get(field) == from_value]
    if not candidates:
        return 0

    change_timestamps = _latest_field_entry_timestamps(collection, field, from_value, base_dir=base_dir)
    cutoff_seconds = after_hours * 3600.0
    now = datetime.now(timezone.utc)

    moved = 0
    for record in candidates:
        record_id = record.get("id")
        if not record_id:
            continue

        age_seconds = _record_age_seconds(record, record_id, change_timestamps, now=now)
        if age_seconds is None or age_seconds < cutoff_seconds:
            continue

        try:
            current = object_records.get_collection_record(collection, record_id, base_dir=base_dir)
        except object_records.RecordNotFoundError:
            continue  # deleted since the listing above
        if current.get(field) != from_value:
            continue  # moved on already -- someone/something got there first

        try:
            object_records.update_collection_record(
                collection, record_id, {field: to_value},
                base_dir=base_dir, actor=AUTO_TRANSITION_ACTOR,
            )
            moved += 1
        except Exception as e:
            log(
                f"Auto-transition: {collection}/{record_id} {field} "
                f"'{from_value}'->'{to_value}' failed: {e}",
                "ERROR",
            )
            continue

    return moved


def process_stale_transitions(*, base_dir: Path | str = "data") -> dict | None:
    """Auto-transition records that have sat in one FSM state too long.

    Runs at most once per DBBASIC_AUTO_TRANSITION_INTERVAL_SECONDS
    (default 3600) gated by a marker FILE (`.auto_transition_last_run`
    under `base_dir`), the same pattern process_compactions above uses
    and for the same reason: a file marker survives a daemon restart, so
    the 48-hour clock this pass exists to enforce is never quietly reset
    by a deploy.

    Rules come from DBBASIC_AUTO_TRANSITION_RULES (see
    auto_transition_rules_from_env), a compact JSON list of
    {"collection", "field", "from", "to", "after_hours"}. This pass ships
    ON by default -- the empty-env case still resolves to a real rule
    (tasks' waiting_on_client -> approved after 48h) -- because the
    predecessor system's version of this job was written but never
    scheduled, and 200+ records sat stranded as a result. Setting the env
    to "" or "[]" is how an operator opts back out.

    Every rule is applied independently and wrapped in its own try/except
    (_apply_auto_transition_rule already isolates per-record failures;
    this is belt and suspenders against a rule failing before it gets
    that far, e.g. a read_collection_records call raising something other
    than the two "not installed" exceptions it explicitly handles) --
    one bad rule is logged and skipped, never allowed to stop the rest of
    the pass.

    Always logs exactly one summary line per run (even a no-op run, with
    zero rules configured or zero records moved) so the pass's activity
    -- or deliberate silence -- is visible in the daemon's own log rather
    than something that has to be inferred. Returns None when the
    interval hasn't elapsed yet (no work attempted this call). Otherwise
    returns {"checked": <rule count>, "moved": <total records moved>,
    "rules": [{"collection", "field", "from", "to", "moved"}, ...]} --
    mainly for tests and manual/CLI invocation, matching
    process_compactions' contract.
    """
    interval = _env_int(AUTO_TRANSITION_INTERVAL_SECONDS_ENV, _DEFAULT_AUTO_TRANSITION_INTERVAL_SECONDS)
    marker_path = Path(base_dir) / AUTO_TRANSITION_MARKER_NAME
    now = time.time()
    try:
        last_run = marker_path.stat().st_mtime
    except OSError:
        last_run = 0.0

    if now - last_run < interval:
        return None

    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(str(now))

    rules = auto_transition_rules_from_env()
    rule_results = []
    total_moved = 0
    for rule in rules:
        try:
            moved = _apply_auto_transition_rule(rule, base_dir=base_dir)
        except Exception as e:
            log(
                f"Auto-transition: rule {rule['collection']}.{rule['field']} "
                f"'{rule['from']}'->'{rule['to']}' failed: {e}",
                "ERROR",
            )
            moved = 0
        total_moved += moved
        rule_results.append({
            "collection": rule["collection"],
            "field": rule["field"],
            "from": rule["from"],
            "to": rule["to"],
            "moved": moved,
        })

    log(f"Auto-transition: checked {len(rules)} rule(s), moved {total_moved} record(s)")
    return {"checked": len(rules), "moved": total_moved, "rules": rule_results}


# --- Rollups ---

def process_rollups(*, base_dir: Path | str = "data") -> dict | None:
    """Recompute every due, enabled rollup_definitions row.

    Rides this daemon's existing poll loop as one more pass
    (plan/vocabulary/14-rollup-spec.md's Dependencies: "No new daemon...
    process_rollups, alongside process_compactions"), but its due-gate is
    NOT the marker-file pattern process_compactions/process_stale_
    transitions use above: 14 is explicit that a rollup_definitions row
    already IS the natural place to hold that state (last_computed_at),
    unlike compaction's collection-wide pass, which has no equivalent
    per-collection record. So there's no marker file here and no fixed
    poll interval either -- each ENABLED definition is checked against
    its own last_computed_at/refresh_interval_seconds every call
    (object_rollups.is_definition_due), which is cheap (a timestamp
    comparison) for every definition that isn't due yet.

    Returns None when the block-wide `rollup_enabled` flag is off (14's
    Degradation: "the daemon's rollup pass is skipped entirely; existing
    target collections keep serving their last-computed data") or when
    the rollup_definitions collection doesn't exist yet (package not
    installed). Otherwise returns {"checked": <definitions considered>,
    "computed": [{"definition_id", "target_collection", "groups",
    "suppressed", ...}, ...]} -- computed only lists definitions that
    were actually due and enabled; a skipped/disabled/not-yet-due
    definition doesn't appear.

    Every definition's recompute is wrapped in its own try/except, same
    isolation pattern as process_compactions: one definition with a bad
    filter, a missing source collection, or a lock contention is logged
    and skipped, every other definition's pass proceeds.
    """
    if not object_rollups.rollup_pass_enabled(base_dir=base_dir):
        return None

    try:
        definitions = object_records.read_collection_records(
            object_rollups.ROLLUP_DEFINITIONS_COLLECTION, base_dir=base_dir
        )
    except (object_collections.CollectionNotFoundError, object_collections.InvalidCollectionNameError):
        return None

    now = datetime.now(timezone.utc)
    computed = []
    for definition in definitions:
        definition_id = definition.get("id") or "<unknown>"

        if not object_rollups.is_definition_enabled(definition):
            continue
        if not object_rollups.is_definition_due(definition, now=now):
            continue

        try:
            result = object_rollups.compute_rollup(definition, base_dir=base_dir)
        except Exception as e:
            log(f"Rollup: {definition_id} failed: {e}", "ERROR")
            continue

        try:
            object_records.update_collection_record(
                object_rollups.ROLLUP_DEFINITIONS_COLLECTION,
                definition_id,
                {"last_computed_at": result["computed_at"]},
                base_dir=base_dir,
                actor=object_rollups.ROLLUP_ACTOR,
                preserve_read_only=True,
            )
        except Exception as e:
            # The recompute itself already succeeded and is live in the
            # target collection; failing to stamp last_computed_at only
            # means this definition looks "due" again next call (a
            # harmless, self-correcting re-run), never lost or corrupted
            # data -- logged, not fatal to the rest of the pass.
            log(f"Rollup: {definition_id} computed but failed to stamp last_computed_at: {e}", "ERROR")

        log(
            f"Rollup: {definition_id} -> {result['target_collection']} "
            f"({result['groups']} group(s), {result['suppressed']} suppressed)"
        )
        computed.append(result)

    return {"checked": len(definitions), "computed": computed}


# --- Materialize ---

def process_materializations(*, base_dir: Path | str = "data") -> dict | None:
    """Generate every due, enabled, non-blocked scheduled/scheduled_fixed
    materialize_definitions row's output.

    Rides this daemon's existing poll loop as one more pass
    (plan/vocabulary/61-materialize-spec.md's Dependencies: "No new
    daemon... process_materializations, alongside process_compactions").
    Unlike process_rollups (whose due-gate lives entirely on the
    definition row, no marker file needed), 61's Events section asks for
    process_stale_transitions' marker-file-gated shape instead -- gated by
    DBBASIC_MATERIALIZE_INTERVAL_SECONDS (default 3600), marker file
    `.materialize_last_run` under `base_dir`, surviving a daemon restart
    the same way every other marker-gated pass does.

    Event-mode definitions (trigger.mode == "event") are never driven by
    this pass -- in v1 they run via materialize_run's manual path only
    (degrade-to-manual, per 61's Degradation section). The automatic
    on-create dispatch (materialize_seed's HANDLES) is deliberately NOT
    auto-wired: this pass does not rewrite materialize_seed's source to
    track the definition set. Rewriting an installed object at runtime --
    and doing it every poll -- is the wrong shape (expensive, surprising,
    self-modifying code); if auto-synced HANDLES is ever wanted it belongs
    in a deliberate scheduled job with its own tests, not the poll loop.
    materialize_seed ships inert (HANDLES == []); the event-dispatch logic
    it would call still exists and is tested, ready for that future
    mechanism or a manual wiring.

    For each enabled, non-blocked, due scheduled/scheduled_fixed
    definition: object_materialize.generate_definition reads
    source_collection outside row-filters (daemon posture, same as
    compaction/rollup/auto-transition), computes the due (source row,
    period) set from first principles every call (never trusting a
    row's own next_run/last_run as the correctness mechanism -- see that
    module's docstring), and for each checks the deterministic header id
    in output_collection: missing -> generate, present -> skip, not an
    error.

    Two-level isolation, per 61's Events section (drawn from process_
    stale_transitions' _apply_auto_transition_rule, not process_rollups'
    per-definition-only isolation): one bad DEFINITION (a malformed
    mapping, a missing source/output/child collection) is caught here and
    never stops any other definition; one bad SOURCE ROW within an
    otherwise-good definition is caught inside object_materialize.
    generate_config and surfaces as one entry in that definition's own
    "errors" list, never stopping any other row in the same definition.

    A missing source/output/child collection is logged once per process
    (object_materialize.MissingCollectionError, warned-once via
    _WARNED_UNKNOWN_MATERIALIZE_COLLECTIONS) -- mirrors process_stale_
    transitions' _WARNED_UNKNOWN_AUTO_TRANSITION_COLLECTIONS exactly, to
    avoid re-logging every poll interval.

    Always logs exactly one summary line per run, even a no-op run
    (`"Materialize: checked N definition(s), generated M record(s),
    skipped K already-generated"`) -- every sibling pass's "activity or
    deliberate silence is visible in the daemon log" convention.

    Returns None when the block-wide `materialize_enabled` flag is off,
    the materialize_definitions collection doesn't exist yet (package not
    installed), or the interval hasn't elapsed. Otherwise returns
    {"checked": <definitions considered>, "generated": <total records>,
    "skipped_already_generated": <total>, "results": [...]}.
    """
    if not object_materialize.materialize_pass_enabled(base_dir=base_dir):
        return None

    interval = _env_int(MATERIALIZE_INTERVAL_SECONDS_ENV, _DEFAULT_MATERIALIZE_INTERVAL_SECONDS)
    marker_path = Path(base_dir) / MATERIALIZE_MARKER_NAME
    now_ts = time.time()
    try:
        last_run = marker_path.stat().st_mtime
    except OSError:
        last_run = 0.0

    if now_ts - last_run < interval:
        return None

    try:
        definitions = object_records.read_collection_records(
            object_materialize.MATERIALIZE_DEFINITIONS_COLLECTION, base_dir=base_dir
        )
    except (object_collections.CollectionNotFoundError, object_collections.InvalidCollectionNameError):
        return None

    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(str(now_ts))

    now = datetime.now(timezone.utc)
    total_generated = 0
    total_skipped = 0
    results = []

    for definition in definitions:
        definition_id = definition.get("id") or "<unknown>"

        if object_materialize.is_definition_blocked(definition):
            continue
        if not object_materialize.is_definition_enabled(definition):
            continue
        if not object_materialize.is_definition_due(definition, now=now):
            continue

        try:
            config = object_materialize.parse_definition(definition, base_dir=base_dir)
        except object_materialize.MissingCollectionError as e:
            if definition_id not in _WARNED_UNKNOWN_MATERIALIZE_COLLECTIONS:
                log(
                    f"Materialize: {definition_id} references missing {e.role} collection "
                    f"'{e.collection}' (package not installed?), skipping -- will keep "
                    "checking, logged once",
                    "WARN",
                )
                _WARNED_UNKNOWN_MATERIALIZE_COLLECTIONS.add(definition_id)
            continue
        except Exception as e:
            log(f"Materialize: {definition_id} failed: {e}", "ERROR")
            continue

        if config.trigger_mode == "event":
            continue  # event-mode definitions are driven by dispatch/manual only

        _WARNED_UNKNOWN_MATERIALIZE_COLLECTIONS.discard(definition_id)

        try:
            result = object_materialize.generate_config(config, base_dir=base_dir, now=now)
        except Exception as e:
            log(f"Materialize: {definition_id} failed: {e}", "ERROR")
            continue

        for error in result["errors"]:
            log(
                f"Materialize: {definition_id} source={error['source_id']} "
                f"period={error['period_start']} failed: {error['error']}",
                "ERROR",
            )

        try:
            object_records.update_collection_record(
                object_materialize.MATERIALIZE_DEFINITIONS_COLLECTION,
                definition_id,
                {"last_run_at": _now_iso_utc()},
                base_dir=base_dir,
                actor=config.actor,
                preserve_read_only=True,
            )
        except Exception as e:
            # The generation itself already succeeded (whatever was due is
            # live in output_collection); failing to stamp last_run_at only
            # means this definition looks "due" again next call -- a
            # harmless, self-correcting re-run (the deterministic header id
            # is still the real gate), never lost or corrupted data.
            log(f"Materialize: {definition_id} ran but failed to stamp last_run_at: {e}", "ERROR")

        total_generated += result["generated"]
        total_skipped += result["skipped_already_generated"]
        results.append(result)

    log(
        f"Materialize: checked {len(definitions)} definition(s), generated "
        f"{total_generated} record(s), skipped {total_skipped} already-generated"
    )
    return {
        "checked": len(definitions),
        "generated": total_generated,
        "skipped_already_generated": total_skipped,
        "results": results,
    }


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# --- Object Execution ---

def _execute_target(runtime: ObjectRuntime, object_id: str, method: str, payload: dict):
    """Load and execute a target object."""
    request = ObjectExecutionRequest(object_id=object_id, method=method, payload=payload)
    result = execute_object(runtime, request)
    if not result.ok:
        if result.error and result.error.type == "ObjectNotFoundError":
            raise FileNotFoundError(result.error.message)
        raise ObjectExecutionFailure(result)
    return result.result


def _find_object_file(object_id: str) -> Path | None:
    """Find the .py file for an object ID."""
    return resolve_object_id(object_id)


def process_notifications(*, base_dir: Path | str = "data") -> dict | None:
    """12 notify: turn new record-change events into notifications.

    Polls the record-change log rather than riding synchronous HANDLES
    dispatch -- HANDLES is gated behind DBBASIC_ENABLE_EVENT_HANDLERS (off in
    prod) and we deliberately don't rewrite installed objects to track dynamic
    event sets (see object_notify's docstring). Reads each watched collection's
    change entries newer than the cursor, matches them against enabled
    notify_rules (object_notify), and appends a notifications row per resolved
    recipient. At-least-once: a crash between writing a notification and
    advancing the cursor re-notifies, the delivery bar 12/01 accept.

    Returns None when notify is disabled or there are no rules; otherwise
    {"changes": <processed>, "notifications": <written>}.
    """
    if not object_notify.notify_pass_enabled(base_dir=base_dir):
        return None
    try:
        rules = object_records.read_collection_records(
            object_notify.NOTIFY_RULES_COLLECTION, base_dir=base_dir
        )
    except (object_collections.CollectionNotFoundError, object_collections.InvalidCollectionNameError):
        return None
    enabled = [r for r in rules if str(r.get("enabled", "true")).strip().lower() not in {"off", "false", "0", "no"}]
    if not enabled:
        return None

    cursor_path = Path(base_dir) / NOTIFY_CURSOR_NAME
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        cursor = cursor_path.read_text().strip()
    except OSError:
        cursor = ""
    if not cursor:
        # First run: stamp at now, notify only on FUTURE changes (never
        # backfill the entire history).
        cursor_path.parent.mkdir(parents=True, exist_ok=True)
        cursor_path.write_text(now_iso)
        return None

    try:
        known = {c.get("name") for c in object_collections.list_collections(base_dir=base_dir) if c.get("name")}
    except (OSError, ValueError):
        known = set()
    watched = object_notify.watched_collections(enabled, known)

    # Gather every change newer than the cursor, across watched collections.
    fresh: list[dict] = []
    for collection in watched:
        try:
            payload = object_record_changes.list_record_changes(collection, base_dir=base_dir, limit=1000)
        except (object_collections.CollectionNotFoundError, object_collections.InvalidCollectionNameError, OSError, ValueError):
            continue
        for change in payload.get("changes") or []:
            ts = str(change.get("timestamp") or "")
            if ts > cursor:
                fresh.append(change)
    fresh.sort(key=lambda c: str(c.get("timestamp") or ""))

    written = 0
    max_ts = cursor
    for change in fresh:
        # Per-change try/except: one malformed rule or change never stops the
        # rest, and the cursor still advances past it (never re-storming).
        try:
            for rule in enabled:
                for note in object_notify.notifications_for_change(rule, change, base_dir=base_dir):
                    object_records.create_collection_record(
                        object_notify.NOTIFICATIONS_COLLECTION, note,
                        base_dir=base_dir, actor=object_notify.DEFAULT_ACTOR,
                    )
                    written += 1
        except Exception as exc:  # noqa: BLE001 -- isolate one bad change
            log(f"Notify: change {change.get('change_id')} failed: {exc}", "ERROR")
        ts = str(change.get("timestamp") or "")
        if ts > max_ts:
            max_ts = ts

    if max_ts != cursor:
        cursor_path.write_text(max_ts)
    if fresh or written:
        log(f"Notify: processed {len(fresh)} change(s), wrote {written} notification(s)")
    return {"changes": len(fresh), "notifications": written}


# 01 email adapter: rolling per-minute send-rate window, persisted as JSON
# ({window_start, sent_count}) so it survives a daemon restart -- the same
# "cheap state outside the collection" shape process_compactions' marker uses.
EMAIL_RATE_MARKER_NAME = ".email_rate_window"
# Whether we've already logged "SMTP not configured" this process. The pass
# runs every tick (it's one cheap collection read) but must not repeat the
# line every poll -- matching process_scheduler/process_queue when their own
# trigger object is absent.
_EMAIL_UNCONFIGURED_WARNED = False


def _read_email_rate_window(base_dir: Path | str) -> dict | None:
    try:
        return json.loads((Path(base_dir) / EMAIL_RATE_MARKER_NAME).read_text())
    except (OSError, ValueError):
        return None


def _write_email_rate_window(base_dir: Path | str, window: dict) -> None:
    path = Path(base_dir) / EMAIL_RATE_MARKER_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(window))


def process_email_outbox(*, base_dir: Path | str = "data") -> dict | None:
    """01 email adapter: drain the email_outbox queue.

    Every tick, delivers the `queued` rows that are due (`next_attempt_at`
    arrived) up to DBBASIC_SMTP_BATCH_SIZE, bounded by a rolling per-minute
    rate window. Each send's outcome is written back onto its row: `sent`, or
    `queued` again with a backed-off `next_attempt_at` and `last_error` (a
    transient failure), or `dead` (a permanent SMTP error, or retries
    exhausted). Per-message try/except -- one bad send never blocks the batch,
    matching process_compactions' per-item isolation.

    Returns None when email is disabled (feature flag), unconfigured (no SMTP
    mode / host -- everything just keeps queuing, fully inspectable), or the
    email_outbox collection isn't installed. Otherwise a small summary dict.
    """
    global _EMAIL_UNCONFIGURED_WARNED
    if not object_email.email_pass_enabled(base_dir=base_dir):
        return None

    config = object_email.smtp_config_from_env()
    if not config.configured:
        if not _EMAIL_UNCONFIGURED_WARNED:
            log("Email: SMTP not configured (DBBASIC_SMTP_MODE) -- outbound mail is queuing only")
            _EMAIL_UNCONFIGURED_WARNED = True
        return None
    _EMAIL_UNCONFIGURED_WARNED = False  # configured again -> re-warn if it flips back

    try:
        records = object_records.read_collection_records(
            object_email.OUTBOX_COLLECTION, base_dir=base_dir
        )
    except (object_collections.CollectionNotFoundError,
            object_collections.InvalidCollectionNameError, OSError, ValueError):
        return None

    now = datetime.now(timezone.utc)
    due = [r for r in records if object_email.is_due(r, now)]
    if not due:
        return {"attempted": 0, "sent": 0, "dead": 0}
    # Oldest-scheduled first, so a backlog drains fairly rather than starving
    # the earliest-queued behind newer arrivals.
    due.sort(key=lambda r: (str(r.get("next_attempt_at") or ""), str(r.get("created_at") or "")))

    window = object_email.rate_window_reset(_read_email_rate_window(base_dir), time.time())
    sender = object_email.sender_for(config)
    attempted = sent = dead = rate_limited = 0

    for record in due[:config.batch_size]:
        if window["sent_count"] >= config.rate_limit:
            rate_limited += 1
            continue  # left `queued`; a scheduling skip is not a delivery attempt
        try:
            update = object_email.attempt_delivery(record, config, sender=sender, now=now)
        except Exception as exc:  # noqa: BLE001 -- isolate one bad message
            log(f"Email: message {record.get('id')} delivery raised: {exc}", "ERROR")
            continue
        window["sent_count"] += 1  # a network attempt was made (success or not)
        attempted += 1
        try:
            object_records.update_collection_record(
                object_email.OUTBOX_COLLECTION, record["id"], update,
                base_dir=base_dir, actor=object_email.DEFAULT_ACTOR,
                preserve_read_only=True,
            )
        except Exception as exc:  # noqa: BLE001
            # The send already happened; failing to record the outcome only
            # risks an at-least-once re-send next tick -- the delivery bar 01
            # accepts, never lost data.
            log(f"Email: message {record.get('id')} sent but status write failed: {exc}", "ERROR")
        if update.get("status") == object_email.STATUS_SENT:
            sent += 1
        elif update.get("status") == object_email.STATUS_DEAD:
            dead += 1

    _write_email_rate_window(base_dir, window)
    if attempted or rate_limited:
        extra = f", {rate_limited} rate-limited" if rate_limited else ""
        log(f"Email: attempted {attempted} ({sent} sent, {dead} dead){extra}")
    return {"attempted": attempted, "sent": sent, "dead": dead, "rate_limited": rate_limited}


def _connector_package_roots() -> list[str]:
    """Package source roots the reconcile pass loads connectors from, private
    overlay first -- mirrors object_server's resolution so a private connector
    (e.g. Mailcow, in packages-private/) is found on the deployment where it is
    installed, and a private declaration shadows an open one for a collection."""
    roots: list[str] = []
    packages = os.environ.get("DBBASIC_PACKAGES_DIR", object_packages.PACKAGES_DIR)
    # Private overlay defaults to a sibling of the active packages dir (same
    # rule as object_server._private_packages_dir), so overriding the packages
    # dir isolates the overlay too.
    private = os.environ.get("DBBASIC_PRIVATE_PACKAGES_DIR") or str(Path(packages).parent / "packages-private")
    if private and Path(private).is_dir():
        roots.append(private)
    roots.append(packages)
    return roots


def _connector_declarations() -> list[dict]:
    """All connector declarations across roots, deduped by collection with the
    private overlay winning (first root wins)."""
    decls: list[dict] = []
    seen: set[str] = set()
    for root in _connector_package_roots():
        for decl in object_packages.iter_connectors(root=root):
            if decl["collection"] in seen:
                continue
            seen.add(decl["collection"])
            decls.append(decl)
    return decls


def process_connectors(*, base_dir: Path | str = "data") -> dict | None:
    """03 external connectors: reconcile each declared collection against its
    outside system.

    For every collection a package declares a connector for, selects the rows
    whose desired state the external world doesn't match yet (pending /
    pending_delete, backoff due), calls the connector's `reconcile(record)`, and
    writes the outcome back via object_connectors.plan_sync -- synced/deleted on
    success, retry-with-backoff on a transient error, dead on a permanent one or
    exhausted attempts. The driver owns the lifecycle; the connector only
    reports ok/error/permanent. Per-row try/except isolates one bad row, and
    per-collection isolation keeps one bad connector from stopping the rest.

    Returns None when disabled or nothing declares a connector; else a summary.
    """
    if not object_connectors.connectors_pass_enabled(base_dir=base_dir):
        return None
    decls = _connector_declarations()
    if not decls:
        return None

    config = object_connectors.connector_config_from_env()
    now = datetime.now(timezone.utc)
    reconciled = synced = dead = skipped = 0

    for decl in decls:
        try:
            records = object_records.read_collection_records(decl["collection"], base_dir=base_dir)
        except (object_collections.CollectionNotFoundError,
                object_collections.InvalidCollectionNameError, OSError, ValueError):
            continue  # collection not installed here -> nothing to reconcile
        due = [r for r in records if object_connectors.is_due(r, now)]
        if not due:
            continue
        due.sort(key=lambda r: (str(r.get("sync_next_at") or ""), str(r.get("created_at") or "")))
        try:
            reconcile = object_connectors.load_connector(decl["module"], decl["entry"])
        except object_connectors.ConnectorLoadError as exc:
            log(f"Connector: {decl['package_id']}/{decl['collection']} not loaded: {exc}", "ERROR")
            continue

        for record in due[:config.batch_size]:
            try:
                outcome = reconcile(record, base_dir=base_dir)
                if not isinstance(outcome, dict):
                    outcome = {"ok": False, "error": f"connector returned {type(outcome).__name__}, expected dict"}
            except Exception as exc:  # noqa: BLE001 -- transient by default; isolate one bad row
                outcome = {"ok": False, "error": str(exc)}
            if outcome.get("skip"):
                # The connector declined this tick (e.g. unconfigured): leave the
                # row exactly as-is -- not an attempt, not a failure, so it waits
                # inspectably instead of backing off toward `dead`. Mirrors the
                # outbox's "unconfigured = queue only" posture.
                skipped += 1
                continue
            update = object_connectors.plan_sync(record, outcome, config, now=now)
            try:
                object_records.update_collection_record(
                    decl["collection"], record["id"], update,
                    base_dir=base_dir, actor=object_connectors.DEFAULT_ACTOR,
                    preserve_read_only=True,
                )
            except Exception as exc:  # noqa: BLE001
                log(f"Connector: {decl['collection']} {record.get('id')} status write failed: {exc}", "ERROR")
                continue
            reconciled += 1
            status = update.get(object_connectors.SYNC_STATUS_FIELD)
            if status in (object_connectors.STATUS_SYNCED, object_connectors.STATUS_DELETED):
                synced += 1
            elif status == object_connectors.STATUS_DEAD:
                dead += 1

    if reconciled:
        log(f"Connectors: reconciled {reconciled} ({synced} synced, {dead} dead)")
    return {"reconciled": reconciled, "synced": synced, "dead": dead, "skipped": skipped}


# --- Main ---

def shutdown(signum, frame):
    global _running
    log("Shutting down...")
    _running = False


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Object Primitive Daemon")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="Poll interval in seconds (default: 1.0)")
    args = parser.parse_args()

    # The primitive-runtime passes (scheduler/queue/events trigger objects)
    # need the optional dbbasic_object_core runtime. The storage passes
    # (compaction, auto-transitions, cleanups) are stdlib and must run
    # regardless -- a deployment without the runtime still gets them.
    try:
        from dbbasic_object_core.runtime.object_runtime import ObjectRuntime
    except ImportError:
        ObjectRuntime = None

    # Same data-dir resolution as the server: env first, ./data fallback.
    base_dir = os.environ.get("DBBASIC_DATA_DIR", "data")

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print("=" * 60)
    print("Object Primitive Daemon")
    print("=" * 60)
    print(f"Poll interval: {args.interval}s")
    print(f"Data dir: {base_dir}")
    print(f"Object roots: {', '.join(str(root) for root in _object_roots())}")
    if ObjectRuntime is None:
        print("Object runtime: NOT installed (scheduler/queue/events passes disabled)")
    else:
        print(f"Scheduler: {'enabled' if _find_trigger_file('scheduler') else 'no scheduler object'}")
        print(f"Queue: {'enabled' if _find_trigger_file('queue') else 'no queue object'}")
        print(f"Events: {'enabled' if _find_trigger_file('events') else 'no events object'}")
    print(f"Croniter: {'available' if croniter else 'NOT installed (cron tasks disabled)'}")
    print(f"Rate limit cleanup: {'enabled' if (Path(base_dir) / 'ratelimit').exists() else 'no ratelimit dir yet'}")
    print(f"Compaction interval: {_env_int(COMPACTION_INTERVAL_SECONDS_ENV, _DEFAULT_COMPACTION_INTERVAL_SECONDS)}s")
    _startup_rules = auto_transition_rules_from_env()
    print(
        f"Auto-transition rules: {len(_startup_rules)} configured "
        f"(interval {_env_int(AUTO_TRANSITION_INTERVAL_SECONDS_ENV, _DEFAULT_AUTO_TRANSITION_INTERVAL_SECONDS)}s)"
    )
    print(f"Rollups: {'enabled' if object_rollups.rollup_pass_enabled(base_dir=base_dir) else 'disabled (rollup_enabled flag off)'}")
    print(
        f"Materialize: {'enabled' if object_materialize.materialize_pass_enabled(base_dir=base_dir) else 'disabled (materialize_enabled flag off)'} "
        f"(interval {_env_int(MATERIALIZE_INTERVAL_SECONDS_ENV, _DEFAULT_MATERIALIZE_INTERVAL_SECONDS)}s)"
    )
    print(f"Notify: {'enabled' if object_notify.notify_pass_enabled(base_dir=base_dir) else 'disabled (notify_enabled flag off)'} (every poll)")
    _smtp_config = object_email.smtp_config_from_env()
    if not object_email.email_pass_enabled(base_dir=base_dir):
        _email_state = "disabled (email_enabled flag off)"
    elif not _smtp_config.configured:
        _email_state = f"queuing only (DBBASIC_SMTP_MODE={_smtp_config.mode!r}, not configured)"
    else:
        _email_state = f"enabled (mode={_smtp_config.mode})"
    print(f"Email: {_email_state}")
    _connector_decls = _connector_declarations()
    if not object_connectors.connectors_pass_enabled(base_dir=base_dir):
        _connectors_state = "disabled (connectors_enabled flag off)"
    elif not _connector_decls:
        _connectors_state = "no connectors declared"
    else:
        _connectors_state = f"{len(_connector_decls)} connector(s): " + ", ".join(
            f"{d['package_id']}/{d['collection']}" for d in _connector_decls)
    print(f"Connectors: {_connectors_state}")
    print()
    print("Press Ctrl+C to stop")
    print("=" * 60)
    print()

    runtime = ObjectRuntime(base_dir=str(base_dir)) if ObjectRuntime is not None else None
    last_event_cleanup = 0.0

    while _running:
        if runtime is not None:
            try:
                process_scheduler(runtime)
            except Exception as e:
                log(f"Scheduler error: {e}", 'ERROR')

            try:
                process_queue(runtime)
            except Exception as e:
                log(f"Queue error: {e}", 'ERROR')

            try:
                process_events(runtime)
            except Exception as e:
                log(f"Events error: {e}", 'ERROR')

        try:
            cleanup_ratelimit(base_dir=base_dir)
        except Exception as e:
            log(f"Cleanup error: {e}", 'ERROR')

        try:
            process_compactions(base_dir=base_dir)
        except Exception as e:
            log(f"Compaction error: {e}", 'ERROR')

        try:
            process_stale_transitions(base_dir=base_dir)
        except Exception as e:
            log(f"Auto-transition error: {e}", 'ERROR')

        try:
            process_rollups(base_dir=base_dir)
        except Exception as e:
            log(f"Rollup error: {e}", 'ERROR')

        try:
            process_materializations(base_dir=base_dir)
        except Exception as e:
            log(f"Materialize error: {e}", 'ERROR')

        try:
            process_notifications(base_dir=base_dir)
        except Exception as e:
            log(f"Notify error: {e}", 'ERROR')

        try:
            process_email_outbox(base_dir=base_dir)
        except Exception as e:
            log(f"Email error: {e}", 'ERROR')

        try:
            process_connectors(base_dir=base_dir)
        except Exception as e:
            log(f"Connectors error: {e}", 'ERROR')

        now = time.time()
        if now - last_event_cleanup >= EVENT_CLEANUP_INTERVAL_SECONDS:
            last_event_cleanup = now
            try:
                cleanup_events(base_dir=base_dir)
            except Exception as e:
                log(f"Event cleanup error: {e}", 'ERROR')

        # Sleep in small increments for responsive shutdown
        deadline = now + args.interval
        while _running and time.time() < deadline:
            time.sleep(0.1)

    log("Daemon stopped.")


if __name__ == "__main__":
    main()
