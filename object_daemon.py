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
import object_events
import object_record_changes
import object_records
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

def cleanup_ratelimit(max_age=120):
    """Delete rate limit files older than max_age seconds."""
    ratelimit_dir = Path('data/ratelimit')
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


# --- Main ---

def shutdown(signum, frame):
    global _running
    log("Shutting down...")
    _running = False


def main():
    import argparse

    from dbbasic_object_core.runtime.object_runtime import ObjectRuntime

    parser = argparse.ArgumentParser(description="Object Primitive Daemon")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="Poll interval in seconds (default: 1.0)")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print("=" * 60)
    print("Object Primitive Daemon")
    print("=" * 60)
    print(f"Poll interval: {args.interval}s")
    print(f"Object roots: {', '.join(str(root) for root in _object_roots())}")
    print(f"Scheduler: {'enabled' if _find_trigger_file('scheduler') else 'no scheduler object'}")
    print(f"Queue: {'enabled' if _find_trigger_file('queue') else 'no queue object'}")
    print(f"Events: {'enabled' if _find_trigger_file('events') else 'no events object'}")
    print(f"Croniter: {'available' if croniter else 'NOT installed (cron tasks disabled)'}")
    print(f"Rate limit cleanup: {'enabled' if Path('data/ratelimit').exists() else 'no ratelimit dir yet'}")
    print(f"Compaction interval: {_env_int(COMPACTION_INTERVAL_SECONDS_ENV, _DEFAULT_COMPACTION_INTERVAL_SECONDS)}s")
    _startup_rules = auto_transition_rules_from_env()
    print(
        f"Auto-transition rules: {len(_startup_rules)} configured "
        f"(interval {_env_int(AUTO_TRANSITION_INTERVAL_SECONDS_ENV, _DEFAULT_AUTO_TRANSITION_INTERVAL_SECONDS)}s)"
    )
    print()
    print("Press Ctrl+C to stop")
    print("=" * 60)
    print()

    runtime = ObjectRuntime(base_dir='./data')
    last_event_cleanup = 0.0

    while _running:
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
            cleanup_ratelimit()
        except Exception as e:
            log(f"Cleanup error: {e}", 'ERROR')

        try:
            process_compactions()
        except Exception as e:
            log(f"Compaction error: {e}", 'ERROR')

        try:
            process_stale_transitions()
        except Exception as e:
            log(f"Auto-transition error: {e}", 'ERROR')

        now = time.time()
        if now - last_event_cleanup >= EVENT_CLEANUP_INTERVAL_SECONDS:
            last_event_cleanup = now
            try:
                cleanup_events()
            except Exception as e:
                log(f"Event cleanup error: {e}", 'ERROR')

        # Sleep in small increments for responsive shutdown
        deadline = now + args.interval
        while _running and time.time() < deadline:
            time.sleep(0.1)

    log("Daemon stopped.")


if __name__ == "__main__":
    main()
