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
import signal
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dbbasic_object_core.runtime.object_runtime import ObjectRuntime

try:
    from croniter import croniter
except ImportError:
    croniter = None

from object_execution import ObjectExecutionFailure, ObjectExecutionRequest, execute_object
from object_namespace import find_trigger_file, get_object_roots, resolve_object_id


# Daemon state
_running = True


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
            try:
                req = urllib.request.Request(
                    callback_url,
                    data=json.dumps(event).encode('utf-8'),
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    pass  # Best effort
                log(f"Events: delivered {event['event_type']} ({event['id']}) to {sub['id']}")
            except (urllib.error.URLError, OSError) as e:
                log(f"Events: failed to deliver to {callback_url}: {e}", 'WARN')

            # Update last_event_id after each delivery
            sub['last_event_id'] = event['id']
            obj.state_manager.set(sub_key, json.dumps(sub))


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
    print()
    print("Press Ctrl+C to stop")
    print("=" * 60)
    print()

    runtime = ObjectRuntime(base_dir='./data')

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

        # Sleep in small increments for responsive shutdown
        deadline = time.time() + args.interval
        while _running and time.time() < deadline:
            time.sleep(0.1)

    log("Daemon stopped.")


if __name__ == "__main__":
    main()
