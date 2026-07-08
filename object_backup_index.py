"""Read-only inventory and on-demand creation for runtime backups.

Backups are the tar/gzip archives written by ``object_backup`` under the
configured backups directory. This module lists them, creates a new
full-runtime backup on demand, and resolves a backup id to a safe path
for download. It is the data layer behind the admin backup endpoints.

A backup contains the whole runtime data directory — records, identity,
credentials, service keys — so the HTTP surface that uses this module is
strictly admin-gated. Nothing here is public.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import object_backup

MANUAL_LABEL = "manual"
_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,255}\.tar\.gz$")


def backups_dir(data_dir: Path | str | None = None) -> Path:
    """Return the configured backups directory (honors DBBASIC_BACKUPS_DIR)."""
    return object_backup._backups_dir(None, data_dir=data_dir)


def validate_backup_id(backup_id: str) -> bool:
    """Return True when a backup id is a safe archive filename (no traversal)."""
    if not isinstance(backup_id, str) or "/" in backup_id or "\\" in backup_id:
        return False
    return bool(_ID_RE.fullmatch(backup_id))


def backup_path(backup_id: str, *, data_dir: Path | str | None = None) -> Path:
    """Resolve a backup id to its path, refusing anything outside the dir."""
    if not validate_backup_id(backup_id):
        raise ValueError(f"invalid backup id: {backup_id!r}")
    root = backups_dir(data_dir).resolve(strict=False)
    path = (backups_dir(data_dir) / backup_id).resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("backup path escapes the backups directory") from exc
    return path


def list_backups(*, data_dir: Path | str | None = None) -> list[dict[str, object]]:
    """Return backup metadata, newest first — never the archive contents."""
    directory = backups_dir(data_dir)
    if not directory.is_dir():
        return []
    entries = [
        _entry(path)
        for path in directory.glob("*.tar.gz")
        if path.is_file() and validate_backup_id(path.name)
    ]
    entries.sort(key=lambda entry: entry["created_at"], reverse=True)
    return entries


def create_backup(*, data_dir: Path | str | None = None) -> dict[str, object]:
    """Create a full-runtime backup now and return its metadata."""
    summary = object_backup.create_runtime_restore_point(MANUAL_LABEL, data_dir=data_dir)
    return _entry(Path(summary.path))


def _entry(path: Path) -> dict[str, object]:
    stat = path.stat()
    name = path.name
    stem = name[: -len(".tar.gz")] if name.endswith(".tar.gz") else name
    parts = stem.split("-", 1)
    label = parts[1] if len(parts) == 2 else stem
    if label.startswith("package-"):
        kind, scope = "package", label[len("package-"):]
    elif label == MANUAL_LABEL:
        kind, scope = "manual", "runtime"
    else:
        kind, scope = "restore-point", label
    created_at = (
        datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    return {
        "id": name,
        "created_at": created_at,
        "size": stat.st_size,
        "kind": kind,
        "scope": scope,
    }
