"""Admin controls for daemon-compatible scheduler and queue state."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import object_ids
import object_state
from object_namespace import validate_object_id
from object_versions import DEFAULT_DATA_DIR, InvalidObjectIdError

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
SCHEDULER_OBJECT_ID = "scheduler"
QUEUE_OBJECT_ID = "queue"
SCHEDULER_PREFIX = "task_"
QUEUE_PREFIX = "msg_"
VALID_TASK_STATUSES = {"active", "paused", "completed", "cancelled"}
VALID_TASK_TYPES = {"cron", "onetime"}
VALID_QUEUE_STATUSES = {"pending", "processing", "completed", "failed", "expired", "cancelled"}
VALID_QUEUE_ACTIONS = {"cancel", "retry"}
VALID_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_QUEUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


class DaemonControlError(ValueError):
    """Raised when daemon control input is invalid."""


class DaemonItemNotFoundError(LookupError):
    """Raised when a scheduler task or queue message cannot be found."""


def list_scheduler_tasks(
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    status: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    include_payload: bool = False,
) -> dict[str, Any]:
    """Return scheduler tasks newest/nearest first."""
    clean_status = _clean_status(status, VALID_TASK_STATUSES) if status is not None else None
    clean_limit, clean_offset = _clean_page(limit, offset)
    rows, invalid = _state_rows(
        SCHEDULER_OBJECT_ID,
        SCHEDULER_PREFIX,
        base_dir=base_dir,
    )
    tasks = [row for _key, row in rows if clean_status is None or row.get("status") == clean_status]
    tasks.sort(key=_task_sort_key)
    total = len(tasks)
    window = tasks[clean_offset:clean_offset + clean_limit]

    return {
        "tasks": [_public_task(task, include_payload=include_payload) for task in window],
        "count": len(window),
        "total": total,
        "invalid": invalid,
        "limit": clean_limit,
        "offset": clean_offset,
        "has_more": clean_offset + len(window) < total,
    }


def create_scheduler_task(
    payload: Mapping[str, Any],
    *,
    actor: str = "api",
    base_dir: Path | str = DEFAULT_DATA_DIR,
    now: int | None = None,
    include_payload: bool = False,
) -> dict[str, Any]:
    """Create one daemon-compatible scheduler task."""
    current_time = _now(now)
    object_id = _clean_object_id(payload.get("object_id"))
    method = _clean_method(payload.get("method", "POST"))
    task_type = _clean_choice(payload.get("type", "onetime"), VALID_TASK_TYPES, "type")
    schedule = _clean_schedule(payload.get("schedule"))
    next_run = _optional_epoch(payload.get("next_run"), "next_run")
    if next_run is None and task_type == "onetime":
        next_run = _optional_epoch(schedule, "schedule")

    status = _clean_status(payload.get("status", "active"), VALID_TASK_STATUSES)
    task_id = _clean_existing_id(payload.get("id")) if payload.get("id") else object_ids.new_uuid4()
    task = {
        "id": task_id,
        "object_id": object_id,
        "method": method,
        "payload": _json_safe_payload(payload.get("payload", {})),
        "type": task_type,
        "schedule": schedule,
        "status": status,
        "next_run": next_run,
        "last_run": None,
        "run_count": 0,
        "created_at": current_time,
        "created_at_iso": _utc_timestamp(current_time),
        "created_by": _clean_actor(actor),
        "updated_at": current_time,
        "updated_at_iso": _utc_timestamp(current_time),
        "updated_by": _clean_actor(actor),
    }

    manager = _state_manager(SCHEDULER_OBJECT_ID, base_dir)
    manager.set(_scheduler_key(task_id), _json_dumps(task))
    return _public_task(task, include_payload=include_payload)


def update_scheduler_task(
    task_id: str,
    payload: Mapping[str, Any],
    *,
    actor: str = "api",
    base_dir: Path | str = DEFAULT_DATA_DIR,
    now: int | None = None,
    include_payload: bool = False,
) -> dict[str, Any]:
    """Patch one scheduler task without changing its state key."""
    clean_task_id = _clean_existing_id(task_id)
    manager = _state_manager(SCHEDULER_OBJECT_ID, base_dir)
    key, task = _find_row(manager, SCHEDULER_PREFIX, clean_task_id, "Scheduler task")
    current_time = _now(now)

    if "status" in payload:
        task["status"] = _clean_status(payload["status"], VALID_TASK_STATUSES)
    if "next_run" in payload:
        task["next_run"] = _optional_epoch(payload["next_run"], "next_run")
    if "schedule" in payload:
        schedule = _clean_schedule(payload["schedule"])
        task["schedule"] = schedule
        if task.get("type") == "onetime" and task.get("next_run") is None:
            task["next_run"] = _optional_epoch(schedule, "schedule")
    if "method" in payload:
        task["method"] = _clean_method(payload["method"])
    if "payload" in payload:
        task["payload"] = _json_safe_payload(payload["payload"])
    if "type" in payload:
        task["type"] = _clean_choice(payload["type"], VALID_TASK_TYPES, "type")
    if "object_id" in payload:
        task["object_id"] = _clean_object_id(payload["object_id"])

    task["updated_at"] = current_time
    task["updated_at_iso"] = _utc_timestamp(current_time)
    task["updated_by"] = _clean_actor(actor)
    manager.set(key, _json_dumps(task))
    return _public_task(task, include_payload=include_payload)


def delete_scheduler_task(
    task_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    """Delete one scheduler task."""
    clean_task_id = _clean_existing_id(task_id)
    manager = _state_manager(SCHEDULER_OBJECT_ID, base_dir)
    key, task = _find_row(manager, SCHEDULER_PREFIX, clean_task_id, "Scheduler task")
    manager.delete(key)
    return _public_task(task, include_payload=False)


def list_queue_messages(
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    status: str | None = None,
    queue_name: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    include_payload: bool = False,
) -> dict[str, Any]:
    """Return queue messages newest/most actionable first."""
    clean_status = _clean_status(status, VALID_QUEUE_STATUSES) if status is not None else None
    clean_queue_name = _clean_queue_name(queue_name) if queue_name is not None else None
    clean_limit, clean_offset = _clean_page(limit, offset)
    rows, invalid = _state_rows(QUEUE_OBJECT_ID, QUEUE_PREFIX, base_dir=base_dir)
    messages = [
        row
        for _key, row in rows
        if (clean_status is None or row.get("status") == clean_status)
        and (clean_queue_name is None or row.get("queue_name") == clean_queue_name)
    ]
    messages.sort(key=_message_sort_key)
    total = len(messages)
    window = messages[clean_offset:clean_offset + clean_limit]

    return {
        "messages": [_public_message(message, include_payload=include_payload) for message in window],
        "count": len(window),
        "total": total,
        "invalid": invalid,
        "limit": clean_limit,
        "offset": clean_offset,
        "has_more": clean_offset + len(window) < total,
    }


def enqueue_message(
    payload: Mapping[str, Any],
    *,
    actor: str = "api",
    base_dir: Path | str = DEFAULT_DATA_DIR,
    now: int | None = None,
    include_payload: bool = False,
) -> dict[str, Any]:
    """Create one daemon-compatible pending queue message."""
    current_time = _now(now)
    object_id = _clean_object_id(payload.get("object_id"))
    method = _clean_method(payload.get("method", "POST"))
    queue_name = _clean_queue_name(payload.get("queue_name", "default"))
    priority_level = _int_value(payload.get("priority_level", 2), "priority_level", minimum=0)
    message_id = _clean_existing_id(payload.get("id")) if payload.get("id") else object_ids.new_uuid4()
    visible_after = _optional_epoch(payload.get("visible_after"), "visible_after")
    expires_at = _optional_epoch(payload.get("expires_at"), "expires_at")
    max_attempts = _int_value(payload.get("max_attempts", 3), "max_attempts", minimum=1)

    message = {
        "id": message_id,
        "queue_name": queue_name,
        "message": {
            "object_id": object_id,
            "method": method,
            "payload": _json_safe_payload(payload.get("payload", {})),
        },
        "priority_level": priority_level,
        "status": "pending",
        "created_at": current_time,
        "created_at_iso": _utc_timestamp(current_time),
        "created_by": _clean_actor(actor),
        "visible_after": current_time if visible_after is None else visible_after,
        "expires_at": expires_at,
        "attempts": 0,
        "max_attempts": max_attempts,
        "updated_at": current_time,
        "updated_at_iso": _utc_timestamp(current_time),
        "updated_by": _clean_actor(actor),
    }

    manager = _state_manager(QUEUE_OBJECT_ID, base_dir)
    manager.set(_queue_key(message), _json_dumps(message))
    return _public_message(message, include_payload=include_payload)


def update_queue_message(
    message_id: str,
    payload: Mapping[str, Any],
    *,
    actor: str = "api",
    base_dir: Path | str = DEFAULT_DATA_DIR,
    now: int | None = None,
    include_payload: bool = False,
) -> dict[str, Any]:
    """Patch one queue message, including admin retry/cancel actions."""
    clean_message_id = _clean_existing_id(message_id)
    manager = _state_manager(QUEUE_OBJECT_ID, base_dir)
    key, message = _find_row(manager, QUEUE_PREFIX, clean_message_id, "Queue message")
    current_time = _now(now)

    action = payload.get("action")
    if action is not None:
        clean_action = _clean_choice(action, VALID_QUEUE_ACTIONS, "action")
        if clean_action == "cancel":
            message["status"] = "cancelled"
            message["cancelled_at"] = current_time
            message["cancelled_at_iso"] = _utc_timestamp(current_time)
            message["cancelled_by"] = _clean_actor(actor)
        elif clean_action == "retry":
            message["status"] = "pending"
            message["visible_after"] = current_time
            message["retried_at"] = current_time
            message["retried_at_iso"] = _utc_timestamp(current_time)
            message["retried_by"] = _clean_actor(actor)

    if "status" in payload:
        message["status"] = _clean_status(payload["status"], VALID_QUEUE_STATUSES)
    if "visible_after" in payload:
        visible_after = _optional_epoch(payload["visible_after"], "visible_after")
        message["visible_after"] = current_time if visible_after is None else visible_after
    if "expires_at" in payload:
        message["expires_at"] = _optional_epoch(payload["expires_at"], "expires_at")
    if "priority_level" in payload:
        message["priority_level"] = _int_value(payload["priority_level"], "priority_level", minimum=0)
    if "max_attempts" in payload:
        message["max_attempts"] = _int_value(payload["max_attempts"], "max_attempts", minimum=1)

    message["updated_at"] = current_time
    message["updated_at_iso"] = _utc_timestamp(current_time)
    message["updated_by"] = _clean_actor(actor)
    manager.set(key, _json_dumps(message))
    return _public_message(message, include_payload=include_payload)


def delete_queue_message(
    message_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    """Delete one queue message."""
    clean_message_id = _clean_existing_id(message_id)
    manager = _state_manager(QUEUE_OBJECT_ID, base_dir)
    key, message = _find_row(manager, QUEUE_PREFIX, clean_message_id, "Queue message")
    manager.delete(key)
    return _public_message(message, include_payload=False)


def _state_manager(object_id: str, base_dir: Path | str) -> object_state.ObjectStateManager:
    return object_state.ObjectStateManager(object_id, base_dir=base_dir)


def _state_rows(
    object_id: str,
    prefix: str,
    *,
    base_dir: Path | str,
) -> tuple[list[tuple[str, dict[str, Any]]], int]:
    rows: list[tuple[str, dict[str, Any]]] = []
    invalid = 0
    for key, value in _state_manager(object_id, base_dir).get_all().items():
        if not key.startswith(prefix):
            continue
        row = _load_json_object(value)
        if row is None:
            invalid += 1
            continue
        row.setdefault("id", key.removeprefix(prefix))
        rows.append((key, row))
    return rows, invalid


def _find_row(
    manager: object_state.ObjectStateManager,
    prefix: str,
    item_id: str,
    label: str,
) -> tuple[str, dict[str, Any]]:
    for key, value in manager.get_all().items():
        if not key.startswith(prefix):
            continue
        row = _load_json_object(value)
        if row is None:
            continue
        if str(row.get("id", "")) == item_id or key == f"{prefix}{item_id}":
            return key, row
    raise DaemonItemNotFoundError(f"{label} not found: {item_id}")


def _scheduler_key(task_id: str) -> str:
    return f"{SCHEDULER_PREFIX}{task_id}"


def _queue_key(message: Mapping[str, Any]) -> str:
    queue_name = _clean_queue_name(message.get("queue_name", "default"))
    priority = _int_value(message.get("priority_level", 2), "priority_level", minimum=0)
    created_at = _int_value(message.get("created_at", 0), "created_at", minimum=0)
    message_id = _clean_existing_id(message.get("id"))
    return f"{QUEUE_PREFIX}{queue_name}_{priority}_{created_at}_{message_id}"


def _public_task(task: Mapping[str, Any], *, include_payload: bool) -> dict[str, Any]:
    public = dict(task)
    payload = public.pop("payload", None)
    public["payload_present"] = payload not in ({}, None, "")
    if include_payload:
        public["payload"] = payload
    return public


def _public_message(message: Mapping[str, Any], *, include_payload: bool) -> dict[str, Any]:
    public = dict(message)
    body = public.get("message")
    if isinstance(body, Mapping):
        body_public = dict(body)
        payload = body_public.pop("payload", None)
        body_public["payload_present"] = payload not in ({}, None, "")
        if include_payload:
            body_public["payload"] = payload
        public["message"] = body_public
    return public


def _task_sort_key(task: Mapping[str, Any]) -> tuple[int, int, str]:
    next_run = _coerce_int(task.get("next_run"))
    created_at = _coerce_int(task.get("created_at"))
    return (next_run if next_run is not None else 2**63 - 1, created_at or 0, str(task.get("id", "")))


def _message_sort_key(message: Mapping[str, Any]) -> tuple[int, int, int, str]:
    status_rank = 0 if message.get("status") == "pending" else 1
    visible_after = _coerce_int(message.get("visible_after")) or 0
    priority = _coerce_int(message.get("priority_level")) or 0
    return (status_rank, visible_after, -priority, str(message.get("id", "")))


def _load_json_object(value: Any) -> dict[str, Any] | None:
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


def _json_safe_payload(payload: Any) -> Any:
    try:
        json.dumps(payload)
    except (TypeError, ValueError) as exc:
        raise DaemonControlError("payload must be JSON serializable") from exc
    return payload


def _json_dumps(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _clean_object_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DaemonControlError("object_id is required")
    clean = value.strip()
    if not validate_object_id(clean):
        raise InvalidObjectIdError(f"Invalid object ID: {clean}")
    return clean


def _clean_method(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DaemonControlError("method must be a non-empty string")
    clean = value.strip().upper()
    if clean not in VALID_HTTP_METHODS:
        raise DaemonControlError(f"method must be one of: {', '.join(sorted(VALID_HTTP_METHODS))}")
    return clean


def _clean_schedule(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DaemonControlError("schedule is required")
    return value.strip()


def _clean_status(value: Any, allowed: set[str]) -> str:
    return _clean_choice(value, allowed, "status")


def _clean_choice(value: Any, allowed: set[str], field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DaemonControlError(f"{field} must be a non-empty string")
    clean = value.strip().lower()
    if clean not in allowed:
        raise DaemonControlError(f"{field} must be one of: {', '.join(sorted(allowed))}")
    return clean


def _clean_existing_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DaemonControlError("id must be a non-empty string")
    clean = value.strip()
    if not _SAFE_ID_RE.fullmatch(clean):
        raise DaemonControlError(f"Invalid id: {clean}")
    return clean


def _clean_queue_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DaemonControlError("queue_name must be a non-empty string")
    clean = value.strip()
    if not _SAFE_QUEUE_RE.fullmatch(clean):
        raise DaemonControlError(f"Invalid queue_name: {clean}")
    return clean


def _clean_actor(actor: str) -> str:
    return str(actor or "api").strip()[:128] or "api"


def _clean_page(limit: int, offset: int) -> tuple[int, int]:
    clean_limit = _int_value(limit, "limit", minimum=1)
    if clean_limit > MAX_LIMIT:
        raise DaemonControlError(f"limit must be at most {MAX_LIMIT}")
    clean_offset = _int_value(offset, "offset", minimum=0)
    return clean_limit, clean_offset


def _int_value(value: Any, field: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool):
        raise DaemonControlError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DaemonControlError(f"{field} must be an integer") from exc
    if minimum is not None and parsed < minimum:
        raise DaemonControlError(f"{field} must be at least {minimum}")
    return parsed


def _optional_epoch(value: Any, field: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise DaemonControlError(f"{field} must be an epoch integer or ISO timestamp")
    if isinstance(value, (int, float)):
        if int(value) < 0:
            raise DaemonControlError(f"{field} must be at least 0")
        return int(value)
    if isinstance(value, str):
        clean = value.strip()
        if not clean:
            return None
        try:
            parsed = int(clean)
        except ValueError:
            try:
                dt = datetime.fromisoformat(clean.replace("Z", "+00:00"))
            except ValueError as exc:
                raise DaemonControlError(
                    f"{field} must be an epoch integer or ISO timestamp"
                ) from exc
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        if parsed < 0:
            raise DaemonControlError(f"{field} must be at least 0")
        return parsed
    raise DaemonControlError(f"{field} must be an epoch integer or ISO timestamp")


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _now(now: int | None) -> int:
    return int(time.time() if now is None else now)


def _utc_timestamp(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")
