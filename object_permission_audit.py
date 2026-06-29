"""Permission decision audit log."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any

from object_versions import DEFAULT_DATA_DIR

PERMISSIONS_DIR = "permissions"
AUDIT_FILE = "audit.jsonl"


def audit_path(base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    """Return the permission audit file path."""
    root = Path(base_dir) / PERMISSIONS_DIR
    path = root / AUDIT_FILE
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)

    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("Permission audit path escapes permissions directory") from exc

    return path


def append_permission_audit(
    entry: dict[str, Any],
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> Path:
    """Append one JSON-line permission audit entry."""
    path = audit_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")

    return path


def get_permission_audit(
    base_dir: Path | str = DEFAULT_DATA_DIR,
    *,
    limit: int = 100,
    action: str | None = None,
    object_id: str | None = None,
    collection: str | None = None,
    allowed: bool | None = None,
    enforced: bool | None = None,
) -> list[dict[str, Any]]:
    """Read recent permission audit entries."""
    if limit < 1:
        raise ValueError("Permission audit limit must be at least 1")

    path = audit_path(base_dir)
    if not path.exists():
        return []

    entries: deque[dict[str, Any]] = deque(maxlen=limit)
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if _matches_filters(
                entry,
                action=action,
                object_id=object_id,
                collection=collection,
                allowed=allowed,
                enforced=enforced,
            ):
                entries.append(entry)

    return list(entries)


def _matches_filters(
    entry: dict[str, Any],
    *,
    action: str | None,
    object_id: str | None,
    collection: str | None,
    allowed: bool | None,
    enforced: bool | None,
) -> bool:
    if action is not None and entry.get("action") != action:
        return False
    if object_id is not None and entry.get("object_id") != object_id:
        return False
    if collection is not None and entry.get("collection") != collection:
        return False
    if enforced is not None and entry.get("enforced") is not enforced:
        return False
    if allowed is not None:
        decision = entry.get("decision")
        if not isinstance(decision, dict) or decision.get("allowed") is not allowed:
            return False

    return True
