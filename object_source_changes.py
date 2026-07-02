"""Append-only change logs for DBBASIC object source updates.

Source versions store recoverable source snapshots. Source changes are the
operator-facing activity facts behind edit, rollback, audit, and Scroll history
screens.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import object_ids
from object_namespace import validate_object_id
from object_versions import DEFAULT_DATA_DIR, InvalidObjectIdError

SOURCE_CHANGES_DIR = "source_changes"
CHANGES_FILE = "changes.jsonl"
DEFAULT_CHANGE_LIMIT = 100
MAX_CHANGE_LIMIT = 1000
VALID_ACTIONS = {"source_create", "source_update", "source_rollback"}

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class InvalidSourceChangeError(ValueError):
    """Raised when a source change entry is not safe to write or read."""


def append_source_change(
    *,
    object_id: str,
    action: str,
    version_id: int,
    from_version_id: int | None = None,
    actor: str = "api",
    message: str = "",
    correlation_id: str | None = None,
    details: Mapping[str, Any] | None = None,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    """Append one source change and return the stored entry."""
    path = source_changes_file(object_id, base_dir=base_dir)
    if action not in VALID_ACTIONS:
        raise InvalidSourceChangeError(f"Invalid source change action: {action}")

    clean_version_id = _positive_int(version_id, field="version_id")
    clean_from_version_id = (
        _positive_int(from_version_id, field="from_version_id")
        if from_version_id is not None
        else None
    )

    timestamp = _utc_timestamp()
    entry = {
        "change_id": _change_id(timestamp, object_id, action),
        "timestamp": timestamp,
        "object_id": object_id,
        "action": action,
        "version_id": clean_version_id,
        "from_version_id": clean_from_version_id,
        "actor": _clean_text(actor, default="api"),
        "message": _clean_text(message, default=_default_message(action)),
        "correlation_id": _clean_optional_text(correlation_id),
        "details": _normalize_details(details),
    }

    with _file_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")))
            handle.write("\n")

    return entry


def list_source_changes(
    object_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    limit: int = DEFAULT_CHANGE_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Return newest-first source changes for one object."""
    path = source_changes_file(object_id, base_dir=base_dir)
    _validate_page(limit=limit, offset=offset)

    changes = _read_changes(path)
    total = len(changes)
    window = changes[offset:offset + limit]
    return {
        "object_id": object_id,
        "changes": window,
        "count": len(window),
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(window) < total,
    }


def source_changes_file(object_id: str, base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    """Return the validated JSONL source change-log path."""
    if not validate_object_id(object_id):
        raise InvalidObjectIdError(f"Invalid object ID: {object_id}")

    root = Path(base_dir) / SOURCE_CHANGES_DIR
    path = root / object_id / CHANGES_FILE
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)

    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise InvalidObjectIdError(
            f"Source change path escapes change directory: {object_id}"
        ) from exc

    return path


def _read_changes(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    changes: list[dict[str, Any]] = []
    with _file_lock(path):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                changes.append(entry)

    changes.reverse()
    return changes


def _normalize_details(details: Mapping[str, Any] | None) -> dict[str, Any]:
    if details is None:
        return {}
    if not isinstance(details, Mapping):
        raise InvalidSourceChangeError("Source change details must be an object")
    return {str(key): _json_safe_value(value) for key, value in details.items()}


def _json_safe_value(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe_value(item) for item in value]
    return str(value)


def _validate_page(*, limit: int, offset: int) -> None:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if limit > MAX_CHANGE_LIMIT:
        raise ValueError(f"limit must be at most {MAX_CHANGE_LIMIT}")
    if offset < 0:
        raise ValueError("offset must be at least 0")


def _positive_int(value: int, *, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidSourceChangeError(f"{field} must be an integer") from exc
    if parsed < 1:
        raise InvalidSourceChangeError(f"{field} must be at least 1")
    return parsed


def _clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_text(value: str, *, default: str) -> str:
    text = str(value).strip()
    return text or default


def _default_message(action: str) -> str:
    return {
        "source_create": "Created object source",
        "source_update": "Updated object source",
        "source_rollback": "Rolled back object source",
    }[action]


def _change_id(timestamp: str, object_id: str, action: str) -> str:
    return object_ids.new_uuid4()


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_lock(path: Path) -> threading.Lock:
    key = str(path.resolve(strict=False))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock
