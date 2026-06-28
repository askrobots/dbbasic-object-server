"""Object log storage.

The working prototype stores object logs in TSV files:

data/logs/{object_id}/log.tsv

Rows are written with a header. The default fields are ``entry_id``,
``timestamp``, ``level``, and ``message``; object code may add extra columns.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from object_namespace import validate_object_id
from object_versions import DEFAULT_DATA_DIR, InvalidObjectIdError


LOG_FILE = "log.tsv"
DEFAULT_LOG_FIELDS = [
    "entry_id",
    "timestamp",
    "level",
    "message",
    "method",
    "status",
    "duration_ms",
    "error_type",
    "error",
]


class ObjectLogger:
    """Runtime logger injected into object modules as ``_logger``."""

    def __init__(self, object_id: str, base_dir: Path | str = DEFAULT_DATA_DIR):
        self.object_id = object_id
        self.base_dir = Path(base_dir)

    def log(self, level: str, message: str, **fields: Any) -> dict[str, Any]:
        """Append one object-owned log entry."""
        return append_object_log(
            self.object_id,
            str(level).upper(),
            str(message),
            base_dir=self.base_dir,
            **fields,
        )

    def debug(self, message: str, **fields: Any) -> dict[str, Any]:
        """Log a DEBUG message."""
        return self.log("DEBUG", message, **fields)

    def info(self, message: str, **fields: Any) -> dict[str, Any]:
        """Log an INFO message."""
        return self.log("INFO", message, **fields)

    def warning(self, message: str, **fields: Any) -> dict[str, Any]:
        """Log a WARNING message."""
        return self.log("WARNING", message, **fields)

    def error(self, message: str, **fields: Any) -> dict[str, Any]:
        """Log an ERROR message."""
        return self.log("ERROR", message, **fields)

    def critical(self, message: str, **fields: Any) -> dict[str, Any]:
        """Log a CRITICAL message."""
        return self.log("CRITICAL", message, **fields)

    def get_logs(
        self,
        *,
        level: str | Iterable[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        **filters: str,
    ) -> list[dict[str, Any]]:
        """Read logs for this object."""
        return get_object_logs(
            self.object_id,
            base_dir=self.base_dir,
            level=level,
            limit=limit,
            offset=offset,
            **filters,
        )


def append_object_log(
    object_id: str,
    level: str,
    message: str,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    **fields: Any,
) -> dict[str, Any]:
    """Append one entry to ``data/logs/{object_id}/log.tsv``."""
    log_dir = object_log_dir(object_id, base_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    entry = {
        "entry_id": uuid4().hex[:16],
        "timestamp": _utc_timestamp(),
        "level": level,
        "message": message,
        **fields,
    }
    entry = {key: value for key, value in entry.items() if value is not None}

    _append_log_entry(log_dir / LOG_FILE, entry)
    return entry


def get_object_logs(
    object_id: str,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    *,
    level: str | Iterable[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
    **filters: str,
) -> list[dict[str, Any]]:
    """Return object logs from ``data/logs/{object_id}``."""
    log_dir = object_log_dir(object_id, base_dir)
    log_files = _log_files(log_dir)
    if not log_files:
        return []

    entries: list[dict[str, Any]] = []
    for log_file in log_files:
        entries.extend(_read_log_file(log_file))

    entries = _filter_entries(entries, level=level, filters=filters)

    if offset > 0:
        entries = entries[offset:]

    if limit is not None:
        entries = entries[:limit]

    return entries


def object_log_dir(object_id: str, base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    """Return a validated object log directory path."""
    if not validate_object_id(object_id):
        raise InvalidObjectIdError(f"Invalid object ID: {object_id}")

    logs_root = Path(base_dir) / "logs"
    log_dir = logs_root / object_id
    resolved_root = logs_root.resolve(strict=False)
    resolved_dir = log_dir.resolve(strict=False)

    try:
        resolved_dir.relative_to(resolved_root)
    except ValueError as exc:
        raise InvalidObjectIdError(f"Object ID escapes logs directory: {object_id}") from exc

    return log_dir


def _log_files(log_dir: Path) -> list[Path]:
    current_log = log_dir / LOG_FILE
    files = []
    if current_log.exists() and current_log.is_file():
        files.append(current_log)
    files.extend(path for path in sorted(log_dir.glob("log-*.tsv")) if path.is_file())
    return files


def _read_log_file(log_file: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with log_file.open("r", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            entry = _clean_entry(row)
            if entry:
                entries.append(entry)
    return entries


def _clean_entry(row: dict[str | None, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key is not None}


def _append_log_entry(log_file: Path, entry: dict[str, Any]) -> None:
    fieldnames = _append_fieldnames(log_file, entry)
    is_new_file = not log_file.exists() or log_file.stat().st_size == 0

    with log_file.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t")
        if is_new_file:
            writer.writeheader()
        writer.writerow(entry)


def _append_fieldnames(log_file: Path, entry: dict[str, Any]) -> list[str]:
    existing = _existing_fieldnames(log_file)
    fieldnames = _merged_fieldnames(existing, entry.keys())

    if existing and fieldnames != existing:
        _rewrite_log_file(log_file, fieldnames)

    return fieldnames


def _existing_fieldnames(log_file: Path) -> list[str]:
    if not log_file.exists() or log_file.stat().st_size == 0:
        return []

    with log_file.open("r", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        return list(reader.fieldnames or [])


def _merged_fieldnames(existing: list[str], entry_keys: Iterable[str]) -> list[str]:
    fieldnames: list[str] = []
    for field in [*existing, *DEFAULT_LOG_FIELDS, *entry_keys]:
        if field not in fieldnames:
            fieldnames.append(field)
    return fieldnames


def _rewrite_log_file(log_file: Path, fieldnames: list[str]) -> None:
    entries = _read_log_file(log_file)
    temp_path = log_file.with_name(f".{log_file.name}.tmp")

    with temp_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        writer.writerows(entries)

    temp_path.replace(log_file)


def _filter_entries(
    entries: list[dict[str, Any]],
    *,
    level: str | Iterable[str] | None,
    filters: dict[str, str],
) -> list[dict[str, Any]]:
    if level is not None:
        levels = {level} if isinstance(level, str) else set(level)
        entries = [entry for entry in entries if entry.get("level") in levels]

    for key, value in filters.items():
        entries = [entry for entry in entries if entry.get(key) == value]

    return entries


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
