"""Operational event log: execution errors and auth activity.

One append-only JSONL feed for the events operators actually page on —
object execution failures (with correlation ids for tracing back to source
versions and logs) and authentication activity (login success/failure,
logout, session mints). Auth failures are recorded per identifier, which is
the foundation the future login-lockout slice reads from.

Rows never contain passwords, tokens, or hashes.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from object_versions import DEFAULT_DATA_DIR

OPS_DIR = "ops"
EVENTS_FILE = "events.jsonl"
DEFAULT_MAX_ROWS = 5000
EXECUTION_ERROR = "execution_error"
AUTH = "auth"
VALID_KINDS = frozenset({EXECUTION_ERROR, AUTH})

_LOCK = threading.Lock()


def events_path(base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    root = Path(base_dir) / OPS_DIR
    path = root / EVENTS_FILE
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)

    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("Ops events path escapes ops directory") from exc

    return path


def append_event(
    kind: str,
    details: dict[str, Any],
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    max_rows: int = DEFAULT_MAX_ROWS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append one ops event and return the stored entry."""
    if kind not in VALID_KINDS:
        raise ValueError(f"Ops event kind must be one of: {', '.join(sorted(VALID_KINDS))}")

    entry = {
        "timestamp": _format_timestamp(now or _now()),
        "kind": kind,
        **{key: value for key, value in details.items() if value is not None},
    }

    path = events_path(base_dir)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")

        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > max_rows:
            temp_path = path.with_name(f".{path.name}.tmp")
            temp_path.write_text("\n".join(lines[-max_rows:]) + "\n", encoding="utf-8")
            temp_path.replace(path)

    return entry


def read_events(
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    limit: int = 100,
    kind: str | None = None,
    event: str | None = None,
    identifier: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent ops events, newest first, optionally filtered."""
    if limit < 1:
        raise ValueError("Ops events limit must be at least 1")
    if kind is not None and kind not in VALID_KINDS:
        raise ValueError(f"Ops event kind must be one of: {', '.join(sorted(VALID_KINDS))}")

    path = events_path(base_dir)
    if not path.exists():
        return []

    with _LOCK:
        lines = path.read_text(encoding="utf-8").splitlines()

    events: list[dict[str, Any]] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if kind is not None and entry.get("kind") != kind:
            continue
        if event is not None and entry.get("event") != event:
            continue
        if identifier is not None and entry.get("identifier") != identifier:
            continue
        events.append(entry)
        if len(events) >= limit:
            break
    return events


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(timezone.utc)
