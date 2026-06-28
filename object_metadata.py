"""Read-only object metadata summaries."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import object_logs
import object_source
import object_state
import object_versions
from object_namespace import (
    ObjectSource,
    iter_object_sources,
    parse_user_object_id,
    validate_object_id,
)
from object_versions import DEFAULT_DATA_DIR, InvalidObjectIdError


def get_object_metadata(
    object_id: str,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Return a conservative metadata summary for an existing object."""
    source = _find_object_source(object_id, roots=roots)
    state = object_state.get_object_state(object_id, base_dir=base_dir)
    logs = object_logs.get_object_logs(object_id, base_dir=base_dir)
    versions = object_versions.VersionManager(base_dir).get_history(object_id)

    return {
        "object_id": object_id,
        "source_path": source.relative_path.as_posix(),
        "owner": _object_owner(object_id),
        "kind": source.kind,
        "last_modified": source.path.stat().st_mtime,
        "state_count": len(state),
        "state_keys": list(state.keys()),
        "log_count": len(logs),
        "version_count": len(versions),
    }


def _find_object_source(object_id: str, roots: Iterable[Path] | None = None) -> ObjectSource:
    if not validate_object_id(object_id):
        raise InvalidObjectIdError(f"Invalid object ID: {object_id}")

    for source in iter_object_sources(roots):
        if source.object_id == object_id:
            return source

    raise object_source.ObjectSourceNotFoundError(f"Object source not found: {object_id}")


def _object_owner(object_id: str) -> str:
    parsed = parse_user_object_id(object_id)
    if parsed is None:
        return "system"
    user_id, _ = parsed
    return str(user_id)
