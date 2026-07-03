"""Append-only change logs for TSV-backed collection records.

Record changes are the durable facts behind generated admin history screens and
record event publication. Events and listener delivery can fail or be retried;
this file records what actually changed.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import object_collections
import object_ids
import object_records
from object_versions import DEFAULT_DATA_DIR

RECORD_CHANGES_DIR = "record_changes"
CHANGES_FILE = "changes.jsonl"
DEFAULT_CHANGE_LIMIT = 100
MAX_CHANGE_LIMIT = 1000
VALID_ACTIONS = {"create", "update", "delete"}

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class InvalidRecordChangeError(ValueError):
    """Raised when a record change entry is not safe to write or read."""


def append_record_change(
    *,
    collection: str,
    record_id: str,
    action: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    actor: str = "api",
    message: str = "",
    correlation_id: str | None = None,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    """Append one record change and return the stored entry."""
    path = record_changes_file(collection, base_dir=base_dir)
    if not object_records.validate_record_id(record_id):
        raise object_records.InvalidRecordIdError(f"Invalid record id: {record_id}")
    if action not in VALID_ACTIONS:
        raise InvalidRecordChangeError(f"Invalid record change action: {action}")

    before_snapshot = _normalize_snapshot(before)
    after_snapshot = _normalize_snapshot(after)
    if before_snapshot is None and after_snapshot is None:
        raise InvalidRecordChangeError("Record change must include before or after")

    timestamp = _utc_timestamp()
    entry = {
        "change_id": _change_id(timestamp, collection, record_id, action),
        "timestamp": timestamp,
        "collection": collection,
        "record_id": record_id,
        "action": action,
        "actor": _clean_text(actor, default="api"),
        "message": _clean_text(message, default=_default_message(action)),
        "correlation_id": correlation_id or None,
        "changed_fields": _changed_fields(before_snapshot, after_snapshot),
        "before": before_snapshot,
        "after": after_snapshot,
    }

    with _file_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")))
            handle.write("\n")

    return entry


def list_record_changes(
    collection: str,
    *,
    record_id: str | None = None,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    limit: int = DEFAULT_CHANGE_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Return newest-first record changes for one collection or record."""
    path = record_changes_file(collection, base_dir=base_dir)
    if record_id is not None and not object_records.validate_record_id(record_id):
        raise object_records.InvalidRecordIdError(f"Invalid record id: {record_id}")
    _validate_page(limit=limit, offset=offset)

    changes = _read_changes(path, record_id=record_id)
    total = len(changes)
    window = changes[offset:offset + limit]
    payload: dict[str, Any] = {
        "collection": collection,
        "changes": window,
        "count": len(window),
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(window) < total,
    }
    if record_id is not None:
        payload["record_id"] = record_id
    return payload


def record_changes_file(collection: str, base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    """Return the validated JSONL change-log path for a collection."""
    if not object_collections.validate_collection_name(collection):
        raise object_collections.InvalidCollectionNameError(
            f"Invalid collection name: {collection}"
        )

    root = Path(base_dir) / RECORD_CHANGES_DIR
    path = root / collection / CHANGES_FILE
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)

    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise object_collections.InvalidCollectionNameError(
            f"Record change path escapes change directory: {collection}"
        ) from exc

    return path


def _read_changes(path: Path, *, record_id: str | None) -> list[dict[str, Any]]:
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
            if not isinstance(entry, dict):
                continue
            if record_id is not None and entry.get("record_id") != record_id:
                continue
            changes.append(entry)

    changes.reverse()
    return changes


def _normalize_snapshot(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    if not isinstance(record, dict):
        raise InvalidRecordChangeError("Record snapshot must be an object")
    return {str(key): _json_safe_value(value) for key, value in record.items()}


def _json_safe_value(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _changed_fields(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> list[str]:
    before_values = before or {}
    after_values = after or {}
    names = set(before_values) | set(after_values)
    return sorted(name for name in names if before_values.get(name) != after_values.get(name))


def _validate_page(*, limit: int, offset: int) -> None:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if limit > MAX_CHANGE_LIMIT:
        raise ValueError(f"limit must be at most {MAX_CHANGE_LIMIT}")
    if offset < 0:
        raise ValueError("offset must be at least 0")


def _clean_text(value: str, *, default: str) -> str:
    text = str(value).strip()
    return text or default


def _default_message(action: str) -> str:
    return {
        "create": "Created record",
        "update": "Updated record",
        "delete": "Deleted record",
    }[action]


def _change_id(timestamp: str, collection: str, record_id: str, action: str) -> str:
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
