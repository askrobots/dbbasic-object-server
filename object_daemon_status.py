"""Read-only status summaries for the DBBASIC daemon primitives.

The daemon owns delivery and scheduling. This module only reads the same TSV
state so Scroll and admin dashboards can see what is due, pending, failing, and
retained without executing objects.
"""

from __future__ import annotations

import importlib.util
import json
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import object_events
import object_state
from object_namespace import find_trigger_file, get_object_roots
from object_versions import DEFAULT_DATA_DIR


def daemon_status(
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    object_roots: Iterable[Path | str] | None = None,
    rate_limit_dir: Path | str | None = None,
    event_keep_count: int | None = object_events.DEFAULT_EVENT_KEEP_COUNT,
    event_keep_seconds: int | None = object_events.DEFAULT_EVENT_KEEP_SECONDS,
    now: int | None = None,
) -> dict[str, Any]:
    """Return a read-only scheduler/queue/event summary for operators."""
    data_dir = Path(base_dir)
    roots = [Path(root) for root in (object_roots if object_roots is not None else get_object_roots())]
    current_time = int(time.time() if now is None else now)

    scheduler_source = find_trigger_file("scheduler", roots) is not None
    queue_source = find_trigger_file("queue", roots) is not None
    events_source = find_trigger_file("events", roots) is not None

    scheduler = _scheduler_status(data_dir, scheduler_source, current_time)
    queue = _queue_status(data_dir, queue_source, current_time)
    events = _events_status(data_dir, events_source)
    cleanup = _cleanup_status(
        rate_limit_dir=rate_limit_dir,
        event_keep_count=event_keep_count,
        event_keep_seconds=event_keep_seconds,
    )

    invalid_rows = (
        scheduler["tasks"]["invalid"]
        + queue["messages"]["invalid"]
        + events["events"]["invalid"]
        + events["subscriptions"]["invalid"]
    )
    failed_deliveries = events["subscriptions"]["by_delivery_status"].get("failed", 0)

    return {
        "status": "degraded" if invalid_rows or failed_deliveries else "ok",
        "timestamp": _utc_timestamp(current_time),
        "daemon": {
            "mode": "polling",
            "croniter_available": importlib.util.find_spec("croniter") is not None,
            "object_roots": {"count": len(roots)},
            "triggers": {
                "scheduler": {"object_id": "scheduler", "source_present": scheduler_source},
                "queue": {"object_id": "queue", "source_present": queue_source},
                "events": {"object_id": object_events.EVENTS_OBJECT_ID, "source_present": events_source},
            },
        },
        "scheduler": scheduler,
        "queue": queue,
        "events": events,
        "cleanup": cleanup,
    }


def _scheduler_status(base_dir: Path, source_present: bool, now: int) -> dict[str, Any]:
    rows, invalid = _prefixed_json_rows(base_dir, "scheduler", "task_")
    by_status = _status_counts(rows)
    active_rows = [row for row in rows if _status_name(row.get("status")) == "active"]
    next_runs = [_coerce_epoch(row.get("next_run")) for row in active_rows]
    next_runs = [value for value in next_runs if value is not None]
    due_runs = [value for value in next_runs if value <= now]
    future_runs = [value for value in next_runs if value > now]
    earliest = min(next_runs) if next_runs else None

    return {
        "object_id": "scheduler",
        "source_present": source_present,
        "state_present": _state_present(base_dir, "scheduler"),
        "tasks": {
            "total": len(rows) + invalid,
            "valid": len(rows),
            "invalid": invalid,
            "by_status": by_status,
            "active": len(active_rows),
            "due": len(due_runs),
            "future": len(future_runs),
            "missing_next_run": len(active_rows) - len(next_runs),
            "next_run": earliest,
            "next_run_iso": _epoch_iso(earliest),
        },
    }


def _queue_status(base_dir: Path, source_present: bool, now: int) -> dict[str, Any]:
    rows, invalid = _prefixed_json_rows(base_dir, "queue", "msg_")
    by_status = _status_counts(rows)
    pending = [row for row in rows if _status_name(row.get("status")) == "pending"]
    visible = []
    delayed = []
    expired = []

    for row in pending:
        expires_at = _coerce_epoch(row.get("expires_at"))
        visible_after = _coerce_epoch(row.get("visible_after")) or 0
        if expires_at is not None and expires_at < now:
            expired.append(row)
        elif visible_after > now:
            delayed.append((row, visible_after))
        else:
            visible.append(row)

    next_visible = min((visible_after for _row, visible_after in delayed), default=None)

    return {
        "object_id": "queue",
        "source_present": source_present,
        "state_present": _state_present(base_dir, "queue"),
        "messages": {
            "total": len(rows) + invalid,
            "valid": len(rows),
            "invalid": invalid,
            "by_status": by_status,
            "pending_visible": len(visible),
            "pending_delayed": len(delayed),
            "expired_pending": len(expired),
            "next_visible_at": next_visible,
            "next_visible_at_iso": _epoch_iso(next_visible),
        },
    }


def _events_status(base_dir: Path, source_present: bool) -> dict[str, Any]:
    event_rows, invalid_events = _prefixed_json_rows(base_dir, object_events.EVENTS_OBJECT_ID, "event_")
    subscription_rows, invalid_subscriptions = _prefixed_json_rows(
        base_dir,
        object_events.EVENTS_OBJECT_ID,
        "sub_",
    )
    sorted_events = sorted(
        event_rows,
        key=lambda item: (_coerce_epoch(item.get("timestamp")) or 0, str(item.get("id", ""))),
        reverse=True,
    )
    latest = _sanitize_event(sorted_events[0]) if sorted_events else None
    by_event_type = _counts_by_field(event_rows, "event_type")
    by_delivery_status = _delivery_status_counts(subscription_rows)

    return {
        "object_id": object_events.EVENTS_OBJECT_ID,
        "source_present": source_present,
        "state_present": _state_present(base_dir, object_events.EVENTS_OBJECT_ID),
        "events": {
            "total": len(event_rows) + invalid_events,
            "valid": len(event_rows),
            "invalid": invalid_events,
            "by_event_type": by_event_type,
            "latest": latest,
        },
        "subscriptions": {
            "total": len(subscription_rows) + invalid_subscriptions,
            "valid": len(subscription_rows),
            "invalid": invalid_subscriptions,
            "by_event_type": _counts_by_field(subscription_rows, "event_type"),
            "by_delivery_status": by_delivery_status,
            "pending_deliveries": sum(
                _pending_delivery_count(subscription, event_rows)
                for subscription in subscription_rows
            ),
        },
    }


def _cleanup_status(
    *,
    rate_limit_dir: Path | str | None,
    event_keep_count: int | None,
    event_keep_seconds: int | None,
) -> dict[str, Any]:
    return {
        "event_retention": {
            "keep_count": event_keep_count,
            "keep_seconds": event_keep_seconds,
        },
        "rate_limit_files": _rate_limit_file_count(rate_limit_dir),
    }


def _prefixed_json_rows(
    base_dir: Path,
    object_id: str,
    prefix: str,
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    invalid = 0

    for key, value in object_state.get_object_state(object_id, base_dir).items():
        if not key.startswith(prefix):
            continue
        item = _json_mapping(value)
        if item is None:
            invalid += 1
        else:
            rows.append(item)

    return rows, invalid


def _json_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        return None
    try:
        loaded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, Mapping):
        return None
    return dict(loaded)


def _status_counts(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(_status_name(row.get("status")) for row in rows).items()))


def _counts_by_field(rows: Iterable[Mapping[str, Any]], field: str) -> dict[str, int]:
    return dict(sorted(Counter(_text_or_unknown(row.get(field)) for row in rows).items()))


def _delivery_status_counts(subscriptions: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts = Counter()
    for subscription in subscriptions:
        delivery = subscription.get("delivery")
        if isinstance(delivery, Mapping):
            counts[_status_name(delivery.get("status"), default="idle")] += 1
        else:
            counts["idle"] += 1
    return dict(sorted(counts.items()))


def _pending_delivery_count(
    subscription: Mapping[str, Any],
    events: Iterable[Mapping[str, Any]],
) -> int:
    event_type = subscription.get("event_type")
    matching = [
        event for event in events
        if event.get("event_type") == event_type
    ]
    matching.sort(
        key=lambda item: (_coerce_epoch(item.get("timestamp")) or 0, str(item.get("id", "")))
    )
    if not matching:
        return 0

    last_event_id = subscription.get("last_event_id")
    if not last_event_id:
        return len(matching)

    for index, event in enumerate(matching):
        if event.get("id") == last_event_id:
            return max(0, len(matching) - index - 1)
    return len(matching)


def _sanitize_event(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": event.get("id"),
        "event_type": event.get("event_type"),
        "source": event.get("source"),
        "actor": event.get("actor"),
        "timestamp": _coerce_epoch(event.get("timestamp")),
        "created_at": event.get("created_at"),
    }


def _status_name(value: Any, *, default: str = "unknown") -> str:
    if not isinstance(value, str):
        return default
    stripped = value.strip().lower()
    return stripped or default


def _text_or_unknown(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    stripped = value.strip()
    return stripped or "unknown"


def _coerce_epoch(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _epoch_iso(value: int | None) -> str | None:
    if value is None:
        return None
    return _utc_timestamp(value)


def _utc_timestamp(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _state_present(base_dir: Path, object_id: str) -> bool:
    return object_state.object_state_file(object_id, base_dir).exists()


def _rate_limit_file_count(rate_limit_dir: Path | str | None) -> int:
    if rate_limit_dir is None:
        return 0
    directory = Path(rate_limit_dir)
    if not directory.exists():
        return 0
    return sum(1 for path in directory.glob("*.txt") if path.is_file())
