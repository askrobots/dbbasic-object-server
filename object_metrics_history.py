"""TSV-backed metrics history snapshots.

The server's request metrics live in process memory and reset on restart.
This module persists small periodic snapshots so dashboards can show trends
across hours and deploys. Sampling is traffic-driven: the server appends at
most one row per interval while requests are flowing, which is exactly when
the numbers change.
"""

from __future__ import annotations

import csv
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from object_versions import DEFAULT_DATA_DIR

METRICS_DIR = "metrics"
HISTORY_FILE = "history.tsv"
DEFAULT_MAX_ROWS = 10080  # one week of minute samples
SNAPSHOT_FIELDS = (
    "timestamp",
    "uptime_seconds",
    "requests",
    "errors",
    "rps",
    "error_rate",
    "p50_ms",
    "p95_ms",
    "cpu_percent",
    "memory_used_percent",
    "disk_used_percent",
)

_FILE_LOCKS: dict[Path, threading.Lock] = {}
_FILE_LOCKS_LOCK = threading.Lock()


def history_path(base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    root = Path(base_dir) / METRICS_DIR
    path = root / HISTORY_FILE
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)

    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("Metrics history path escapes metrics directory") from exc

    return path


def append_snapshot(
    snapshot: Mapping[str, Any],
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    max_rows: int = DEFAULT_MAX_ROWS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append one snapshot row, pruning to max_rows when the file grows past it."""
    row = {"timestamp": _format_timestamp(now or _now())}
    for field in SNAPSHOT_FIELDS[1:]:
        row[field] = _number_text(snapshot.get(field))

    path = history_path(base_dir)
    with _file_lock(path):
        exists = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=SNAPSHOT_FIELDS, delimiter="\t")
            if not exists:
                writer.writeheader()
            writer.writerow(row)

        rows = _read_rows(path)
        if len(rows) > max_rows:
            _write_rows(path, rows[-max_rows:])

    return row


def read_history(
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    limit: int = 360,
) -> list[dict[str, Any]]:
    """Return the most recent snapshot rows, oldest first, numbers parsed."""
    path = history_path(base_dir)
    with _file_lock(path):
        rows = _read_rows(path)

    parsed = []
    for row in rows[-max(limit, 0):]:
        entry: dict[str, Any] = {"timestamp": row.get("timestamp", "")}
        for field in SNAPSHOT_FIELDS[1:]:
            entry[field] = _parse_number(row.get(field))
        parsed.append(entry)
    return parsed


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [row for row in csv.DictReader(handle, delimiter="\t") if row.get("timestamp")]


def _write_rows(path: Path, rows: Iterable[Mapping[str, str]]) -> None:
    temp_path = path.with_name(f".{path.name}.tmp")
    with temp_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SNAPSHOT_FIELDS, delimiter="\t")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in SNAPSHOT_FIELDS})
    temp_path.replace(path)


def _number_text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return f"{value:.4f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)
    return ""


def _parse_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _file_lock(path: Path) -> threading.Lock:
    resolved = path.resolve(strict=False)
    with _FILE_LOCKS_LOCK:
        lock = _FILE_LOCKS.get(resolved)
        if lock is None:
            lock = threading.Lock()
            _FILE_LOCKS[resolved] = lock
        return lock
