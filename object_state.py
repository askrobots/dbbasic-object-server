"""Read-only object state storage.

The working prototype stores object state in plain TSV files:

data/state/{object_id}/state.tsv

Rows are either ``key<TAB>value`` or ``key<TAB>value<TAB>timestamp``. The
timestamp column is ignored by read-only callers; it exists for replication and
conflict handling in the full runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from object_namespace import validate_object_id
from object_versions import DEFAULT_DATA_DIR, InvalidObjectIdError


STATE_FILE = "state.tsv"


def get_object_state(object_id: str, base_dir: Path | str = DEFAULT_DATA_DIR) -> dict[str, Any]:
    """Return object state from ``data/state/{object_id}/state.tsv``."""
    state_file = object_state_file(object_id, base_dir)
    if not state_file.exists() or not state_file.is_file():
        return {}

    state: dict[str, Any] = {}
    with state_file.open("r") as f:
        for line in f:
            row = _parse_state_row(line)
            if row is None:
                continue
            key, value = row
            state[key] = _coerce_value(value)

    return state


def object_state_file(object_id: str, base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    """Return a validated object state file path."""
    if not validate_object_id(object_id):
        raise InvalidObjectIdError(f"Invalid object ID: {object_id}")

    state_root = Path(base_dir) / "state"
    state_dir = state_root / object_id
    resolved_root = state_root.resolve(strict=False)
    resolved_dir = state_dir.resolve(strict=False)

    try:
        resolved_dir.relative_to(resolved_root)
    except ValueError as exc:
        raise InvalidObjectIdError(f"Object ID escapes state directory: {object_id}") from exc

    return state_dir / STATE_FILE


def _parse_state_row(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped:
        return None

    parts = stripped.split("\t")
    if parts[0] == "key" or len(parts) < 2:
        return None

    return parts[0], parts[1]


def _coerce_value(value: str) -> Any:
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value
