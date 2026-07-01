"""Append-only change logs for DBBASIC packages.

Package changes are the durable package manager facts behind dry-run, install,
failure, restore, and rollback screens. The public server records dry-run plans
and gated package operations, including failed operations.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import object_ids
import object_packages
from object_versions import DEFAULT_DATA_DIR

PACKAGE_CHANGES_DIR = "package_changes"
CHANGES_FILE = "changes.jsonl"
DEFAULT_CHANGE_LIMIT = 100
MAX_CHANGE_LIMIT = 1000
VALID_ACTIONS = {
    "dry_run",
    "install_requested",
    "installed",
    "restore_requested",
    "failed",
    "rolled_back",
}

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class InvalidPackageChangeError(ValueError):
    """Raised when a package change entry is not safe to write or read."""


def append_package_change(
    *,
    package_id: str,
    action: str,
    package_version: str | None = None,
    actor: str = "api",
    message: str = "",
    details: Mapping[str, Any] | None = None,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    """Append one package change and return the stored entry."""
    path = package_changes_file(package_id, base_dir=base_dir)
    if action not in VALID_ACTIONS:
        raise InvalidPackageChangeError(f"Invalid package change action: {action}")

    timestamp = _utc_timestamp()
    entry = {
        "change_id": _change_id(timestamp, package_id, action),
        "timestamp": timestamp,
        "package_id": package_id,
        "package_version": _clean_optional_text(package_version),
        "action": action,
        "actor": _clean_text(actor, default="api"),
        "message": _clean_text(message, default=_default_message(action)),
        "details": _normalize_details(details),
    }

    with _file_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")))
            handle.write("\n")

    return entry


def list_package_changes(
    package_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    limit: int = DEFAULT_CHANGE_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Return newest-first package changes."""
    path = package_changes_file(package_id, base_dir=base_dir)
    _validate_page(limit=limit, offset=offset)

    changes = _read_changes(path)
    total = len(changes)
    window = changes[offset:offset + limit]
    return {
        "package_id": package_id,
        "changes": window,
        "count": len(window),
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(window) < total,
    }


def get_package_change(
    package_id: str,
    change_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> dict[str, Any] | None:
    """Return one package change by id, or None when it is not in the log."""
    clean_change_id = _clean_change_id(change_id)
    path = package_changes_file(package_id, base_dir=base_dir)
    for change in _read_changes(path):
        if change.get("change_id") == clean_change_id:
            return change
    return None


def package_changes_file(package_id: str, base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    """Return the validated JSONL package change-log path."""
    if not object_packages.validate_package_id(package_id):
        raise object_packages.InvalidPackageIdError(f"Invalid package id: {package_id}")

    root = Path(base_dir) / PACKAGE_CHANGES_DIR
    path = root / package_id / CHANGES_FILE
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)

    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise object_packages.InvalidPackageIdError(
            f"Package change path escapes change directory: {package_id}"
        ) from exc

    return path


def dry_run_change_details(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Return compact dry-run details safe for package change history."""
    package = plan.get("package")
    summary = package if isinstance(package, Mapping) else {}
    return {
        "package": {
            "id": summary.get("id"),
            "name": summary.get("name"),
            "version": summary.get("version"),
        },
        "safe_to_install": bool(plan.get("safe_to_install")),
        "install_enabled": bool(plan.get("install_enabled")),
        "objects": _action_counts(plan.get("objects")),
        "schemas": _action_counts(plan.get("schemas")),
        "permissions": _action_counts(plan.get("permissions")),
        "seed": _action_counts(plan.get("seed")),
        "migrations": _action_counts(plan.get("migrations")),
        "warnings": _string_list(plan.get("warnings")),
    }


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


def _action_counts(entries: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(entries, list):
        return counts
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        action = entry.get("action")
        if not isinstance(action, str) or not action:
            continue
        counts[action] = counts.get(action, 0) + 1
    return counts


def _normalize_details(details: Mapping[str, Any] | None) -> dict[str, Any]:
    if details is None:
        return {}
    if not isinstance(details, Mapping):
        raise InvalidPackageChangeError("Package change details must be an object")
    return {str(key): _json_safe_value(value) for key, value in details.items()}


def _json_safe_value(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe_value(item) for item in value]
    return str(value)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _validate_page(*, limit: int, offset: int) -> None:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if limit > MAX_CHANGE_LIMIT:
        raise ValueError(f"limit must be at most {MAX_CHANGE_LIMIT}")
    if offset < 0:
        raise ValueError("offset must be at least 0")


def _clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_text(value: str, *, default: str) -> str:
    text = str(value).strip()
    return text or default


def _clean_change_id(value: str) -> str:
    text = str(value).strip()
    if not text:
        raise InvalidPackageChangeError("Package change id must not be empty")
    if len(text) > 256 or any(ord(character) < 32 for character in text):
        raise InvalidPackageChangeError("Package change id is not safe")
    return text


def _default_message(action: str) -> str:
    return {
        "dry_run": "Dry run package install",
        "install_requested": "Requested package install",
        "installed": "Installed package",
        "restore_requested": "Requested package restore",
        "failed": "Package operation failed",
        "rolled_back": "Rolled back package",
    }[action]


def _change_id(timestamp: str, package_id: str, action: str) -> str:
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
