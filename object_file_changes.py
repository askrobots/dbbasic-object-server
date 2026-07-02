"""Append-only change logs for object-owned files.

Object logs are useful while debugging one object. File changes are the durable
operator facts behind upload, overwrite, delete, audit, and Scroll history
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

FILE_CHANGES_DIR = "file_changes"
CHANGES_FILE = "changes.jsonl"
DEFAULT_CHANGE_LIMIT = 100
MAX_CHANGE_LIMIT = 1000
VALID_ACTIONS = {"file_create", "file_update", "file_delete"}

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class InvalidFileChangeError(ValueError):
    """Raised when a file change entry is not safe to write or read."""


def append_file_change(
    *,
    object_id: str,
    action: str,
    file_name: str,
    file_size: int | None = None,
    actor: str = "api",
    message: str = "",
    correlation_id: str | None = None,
    details: Mapping[str, Any] | None = None,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    """Append one object-file change and return the stored entry."""
    path = file_changes_file(object_id, base_dir=base_dir)
    if action not in VALID_ACTIONS:
        raise InvalidFileChangeError(f"Invalid file change action: {action}")

    clean_file_name = _clean_filename(file_name)
    clean_file_size = _optional_non_negative_int(file_size, field="file_size")
    timestamp = _utc_timestamp()
    entry = {
        "change_id": object_ids.new_uuid4(),
        "timestamp": timestamp,
        "object_id": object_id,
        "action": action,
        "file_name": clean_file_name,
        "file_size": clean_file_size,
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


def list_file_changes(
    object_id: str,
    *,
    file_name: str | None = None,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    limit: int = DEFAULT_CHANGE_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Return newest-first file changes for one object or file."""
    path = file_changes_file(object_id, base_dir=base_dir)
    clean_file_name = _clean_filename(file_name) if file_name is not None else None
    _validate_page(limit=limit, offset=offset)

    changes = _read_changes(path, file_name=clean_file_name)
    total = len(changes)
    window = changes[offset:offset + limit]
    payload: dict[str, Any] = {
        "object_id": object_id,
        "changes": window,
        "count": len(window),
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(window) < total,
    }
    if clean_file_name is not None:
        payload["file_name"] = clean_file_name
    return payload


def file_changes_file(object_id: str, base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    """Return the validated JSONL file change-log path."""
    if not validate_object_id(object_id):
        raise InvalidObjectIdError(f"Invalid object ID: {object_id}")

    root = Path(base_dir) / FILE_CHANGES_DIR
    path = root / object_id / CHANGES_FILE
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)

    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise InvalidObjectIdError(
            f"File change path escapes change directory: {object_id}"
        ) from exc

    return path


def _read_changes(path: Path, *, file_name: str | None) -> list[dict[str, Any]]:
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
            if file_name is not None and entry.get("file_name") != file_name:
                continue
            changes.append(entry)

    changes.reverse()
    return changes


def _normalize_details(details: Mapping[str, Any] | None) -> dict[str, Any]:
    if details is None:
        return {}
    if not isinstance(details, Mapping):
        raise InvalidFileChangeError("File change details must be an object")
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


def _clean_filename(value: str) -> str:
    text = str(value).strip()
    if not text or "\x00" in text or text.startswith("/") or ".." in text:
        raise InvalidFileChangeError(f"Invalid file name: {value!r}")
    path = Path(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise InvalidFileChangeError(f"Invalid file name: {value!r}")
    return path.as_posix()


def _optional_non_negative_int(value: int | None, *, field: str) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidFileChangeError(f"{field} must be an integer") from exc
    if parsed < 0:
        raise InvalidFileChangeError(f"{field} must be at least 0")
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
        "file_create": "Created object file",
        "file_update": "Updated object file",
        "file_delete": "Deleted object file",
    }[action]


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
