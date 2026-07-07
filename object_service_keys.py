"""File-backed per-user service API keys (AI providers and similar).

Keys live in their own owner-only TSV under the identity directory, next to
credentials.tsv and with the same posture: never inside record collections,
never in API read responses, never in portable backups or source control.
The write-only contract is deliberate — a caller can set a key, list which
services have one, and delete one, but no surface can read key material
back. The server uses stored keys on the caller's behalf (for example to
call an AI provider) so the key never travels to browsers or agents.

Keys are stored as provided, not hashed — unlike passwords they must be
recoverable to be used. The 0600 file is the trust boundary, the same one
that already protects the deployment admin token in its env file.
"""

from __future__ import annotations

import csv
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from object_versions import DEFAULT_DATA_DIR

IDENTITY_DIR = "identity"
SERVICE_KEYS_FILE = "service_keys.tsv"
SERVICE_KEY_FIELDS = (
    "user_id",
    "service",
    "key",
    "created_at",
    "updated_at",
)
MAX_KEY_LENGTH = 4096
_SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_FILE_LOCKS: dict[Path, threading.Lock] = {}
_FILE_LOCKS_LOCK = threading.Lock()


class InvalidServiceKeyError(ValueError):
    """Raised when a service key payload is not usable."""


@dataclass(frozen=True)
class StoredServiceKey:
    """One user's stored key for one service."""

    user_id: str
    service: str
    key: str
    created_at: str
    updated_at: str


def service_keys_path(base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    root = Path(base_dir) / IDENTITY_DIR
    path = root / SERVICE_KEYS_FILE
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)

    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("Service keys path escapes identity directory") from exc

    return path


def validate_service_name(service: str) -> bool:
    """Return True when a service name is safe for storage and routes."""
    return isinstance(service, str) and bool(_SERVICE_RE.fullmatch(service))


def set_service_key(
    user_id: str,
    service: str,
    key: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    now: datetime | None = None,
) -> dict[str, str]:
    """Create or replace one user's key for one service; return safe metadata."""
    normalized_user_id = _required_text(user_id, "user_id")
    normalized_service = _required_service(service)
    normalized_key = _required_key(key)
    timestamp = _format_timestamp(now or _now())

    path = service_keys_path(base_dir)
    with _file_lock(path):
        entries = _read_entries(path)
        existing = _pop_entry(entries, normalized_user_id, normalized_service)
        entries.append(
            StoredServiceKey(
                user_id=normalized_user_id,
                service=normalized_service,
                key=normalized_key,
                created_at=existing.created_at if existing is not None else timestamp,
                updated_at=timestamp,
            )
        )
        _write_entries(path, entries)

    return {
        "user_id": normalized_user_id,
        "service": normalized_service,
        "operation": "replaced" if existing is not None else "created",
        "updated_at": timestamp,
    }


def get_service_key(
    user_id: str,
    service: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> str | None:
    """Return one stored key for server-side use, or None.

    This is the only way key material comes back out, and it stays inside
    the server process — route handlers must never place it in responses.
    """
    if not isinstance(user_id, str) or not validate_service_name(service):
        return None
    for entry in _read_entries(service_keys_path(base_dir)):
        if entry.user_id == user_id.strip() and entry.service == service:
            return entry.key
    return None


def list_service_key_status(
    user_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> list[dict[str, str]]:
    """Return which services have keys for one user — metadata only."""
    normalized_user_id = _required_text(user_id, "user_id")
    statuses = []
    for entry in _read_entries(service_keys_path(base_dir)):
        if entry.user_id == normalized_user_id:
            statuses.append(
                {
                    "service": entry.service,
                    "created_at": entry.created_at,
                    "updated_at": entry.updated_at,
                }
            )
    return sorted(statuses, key=lambda status: status["service"])


def remove_service_key(
    user_id: str,
    service: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> bool:
    """Delete one user's key for one service; return whether one existed."""
    normalized_user_id = _required_text(user_id, "user_id")
    normalized_service = _required_service(service)
    path = service_keys_path(base_dir)
    with _file_lock(path):
        entries = _read_entries(path)
        existing = _pop_entry(entries, normalized_user_id, normalized_service)
        if existing is None:
            return False
        _write_entries(path, entries)
    return True


def remove_all_service_keys(
    user_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> int:
    """Delete every key for one user (user deletion cleanup); return count."""
    normalized_user_id = _required_text(user_id, "user_id")
    path = service_keys_path(base_dir)
    with _file_lock(path):
        entries = _read_entries(path)
        kept = [entry for entry in entries if entry.user_id != normalized_user_id]
        removed = len(entries) - len(kept)
        if removed:
            _write_entries(path, kept)
    return removed


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise InvalidServiceKeyError(f"{name} must be a string")
    text = value.strip()
    if not text:
        raise InvalidServiceKeyError(f"{name} is required")
    return text


def _required_service(service: str) -> str:
    if not validate_service_name(service):
        raise InvalidServiceKeyError(
            "service must be lowercase letters, digits, hyphens, or underscores"
        )
    return service


def _required_key(key: str) -> str:
    if not isinstance(key, str):
        raise InvalidServiceKeyError("key must be a string")
    text = key.strip()
    if not text:
        raise InvalidServiceKeyError("key is required")
    if len(text) > MAX_KEY_LENGTH:
        raise InvalidServiceKeyError(f"key must be at most {MAX_KEY_LENGTH} characters")
    if any(char in text for char in "\t\r\n"):
        raise InvalidServiceKeyError("key must not contain tabs or newlines")
    return text


def _pop_entry(
    entries: list[StoredServiceKey],
    user_id: str,
    service: str,
) -> StoredServiceKey | None:
    for index, entry in enumerate(entries):
        if entry.user_id == user_id and entry.service == service:
            return entries.pop(index)
    return None


def _read_entries(path: Path) -> list[StoredServiceKey]:
    if not path.exists():
        return []

    entries: list[StoredServiceKey] = []
    with path.open(newline="") as handle:
        rows = csv.DictReader(handle, delimiter="\t")
        for row in rows:
            if not row:
                continue
            entry = _entry_from_row(row)
            if entry is not None:
                entries.append(entry)
    return entries


def _write_entries(path: Path, entries: Iterable[StoredServiceKey]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    with temp_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SERVICE_KEY_FIELDS, delimiter="\t")
        writer.writeheader()
        for entry in entries:
            writer.writerow(
                {
                    "user_id": entry.user_id,
                    "service": entry.service,
                    "key": entry.key,
                    "created_at": entry.created_at,
                    "updated_at": entry.updated_at,
                }
            )
    os.chmod(temp_path, 0o600)
    temp_path.replace(path)


def _entry_from_row(row: Mapping[str, str]) -> StoredServiceKey | None:
    user_id = (row.get("user_id") or "").strip()
    service = (row.get("service") or "").strip()
    key = (row.get("key") or "").strip()
    created_at = (row.get("created_at") or "").strip()
    updated_at = (row.get("updated_at") or "").strip()
    if not user_id or not service or not key or not created_at or not updated_at:
        return None
    return StoredServiceKey(
        user_id=user_id,
        service=service,
        key=key,
        created_at=created_at,
        updated_at=updated_at,
    )


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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
