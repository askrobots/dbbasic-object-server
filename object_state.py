"""Object state storage.

The working prototype stores object state in plain TSV files:

data/state/{object_id}/state.tsv

Rows are either ``key<TAB>value`` or ``key<TAB>value<TAB>timestamp``. The
timestamp column is ignored by read-only callers; it exists for replication and
conflict handling in the full runtime.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from object_namespace import validate_object_id
from object_versions import DEFAULT_DATA_DIR, InvalidObjectIdError

STATE_FILE = "state.tsv"


class ObjectStateManager:
    """Runtime-owned object state manager."""

    def __init__(self, object_id: str, base_dir: Path | str = DEFAULT_DATA_DIR):
        self.object_id = object_id
        self.base_dir = Path(base_dir)
        self.state_file = object_state_file(object_id, base_dir)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state = get_object_state(object_id, base_dir)

    def get(self, key: str, default: Any = None) -> Any:
        """Return one state value."""
        return self._state.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Set one state value and persist the object state file."""
        _validate_state_key(key)
        self._state = get_object_state(self.object_id, self.base_dir)
        self._state[key] = value
        _write_object_state(self.state_file, self._state, timestamp=time.time())

    def delete(self, key: str) -> None:
        """Delete one state value and persist the object state file."""
        _validate_state_key(key)
        self._state = get_object_state(self.object_id, self.base_dir)
        if key in self._state:
            del self._state[key]
            _write_object_state(self.state_file, self._state, timestamp=time.time())

    def delete_many(self, keys: Iterable[str]) -> int:
        """Delete state values in one atomic rewrite and return the removed count."""
        clean_keys = list(dict.fromkeys(keys))
        for key in clean_keys:
            _validate_state_key(key)

        self._state = get_object_state(self.object_id, self.base_dir)
        removed = 0
        for key in clean_keys:
            if key in self._state:
                del self._state[key]
                removed += 1

        if removed:
            _write_object_state(self.state_file, self._state, timestamp=time.time())

        return removed

    def get_all(self) -> dict[str, Any]:
        """Return all loaded state values."""
        return dict(self._state)

    def reload(self) -> None:
        """Reload state from disk."""
        self._state = get_object_state(self.object_id, self.base_dir)


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


def _write_object_state(state_file: Path, state: dict[str, Any], timestamp: float) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_file.with_name(f".{state_file.name}.tmp")

    with temp_path.open("w") as f:
        for key, value in sorted(state.items()):
            f.write(f"{key}\t{value}\t{timestamp}\n")

    temp_path.replace(state_file)


def _validate_state_key(key: str) -> None:
    if not isinstance(key, str) or not key:
        raise ValueError("State key must be a non-empty string")
    if any(char in key for char in "\t\r\n"):
        raise ValueError(f"Invalid state key: {key!r}")


def _coerce_value(value: str) -> Any:
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value
