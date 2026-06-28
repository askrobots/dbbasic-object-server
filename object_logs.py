"""Read-only object log storage.

The working prototype stores object logs in TSV files:

data/logs/{object_id}/log.tsv

Rows are written with a header. The default fields are ``entry_id``,
``timestamp``, ``level``, and ``message``; object code may add extra columns.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from object_namespace import validate_object_id
from object_versions import DEFAULT_DATA_DIR, InvalidObjectIdError


LOG_FILE = "log.tsv"


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
