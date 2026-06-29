"""Permission decision audit log."""

from __future__ import annotations

import json
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
) -> list[dict[str, Any]]:
    """Read recent permission audit entries."""
    if limit < 1:
        raise ValueError("Permission audit limit must be at least 1")

    path = audit_path(base_dir)
    if not path.exists():
        return []

    lines = path.read_text(encoding="utf-8").splitlines()
    entries: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        if not line.strip():
            continue
        entries.append(json.loads(line))
    return entries
