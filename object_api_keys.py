"""Per-user API keys -- durable bearer tokens for programmatic / MCP / agent
access to the object server.

This is the access path q9 has (`/userauth/apikeys/`, `Authorization: Token …`)
and the object server lacked: a user mints a long-lived token, points an agent
or script at it, and it authenticates as that user. Distinct from the three
existing token kinds: session tokens (short-lived, from a password login), the
operator admin token (one server-wide superuser, in the env), and BYO
service-keys (authenticate to *providers*, not to us).

Posture -- stronger than q9's plaintext-token-in-DB:

- Keys live in their own owner-only TSV under `identity/` (next to
  credentials.tsv / sessions.tsv), `0600`, **excluded from portable backups**
  like the rest of `identity/`.
- **Only the hash is stored** (`hash_token`, the exact session-token shape:
  `sha256:<hex>`). An API key is verify-only -- like a password it never needs
  to be recovered, so it is never kept in a recoverable form. The raw token
  exists once, in the create response; after that the server can only *check* a
  presented token, never reveal a stored one.
- Verification is constant-time (`hmac.compare_digest`).
- Tokens carry a `dbk_` prefix so the auth layer routes them without a wasted
  session lookup, and so a leaked token is greppable/revocable by shape.

The auth wiring (object_server `_permission_identity`) and the self-service
management routes live in object_server; this module is the pure store +
resolver.
"""

from __future__ import annotations

import csv
import hmac
import os
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import object_identity
from object_versions import DEFAULT_DATA_DIR

IDENTITY_DIR = "identity"
API_KEYS_FILE = "api_keys.tsv"
API_KEY_FIELDS = ("user_id", "key_id", "name", "token_hash", "created_at", "last_used_at")

TOKEN_PREFIX = "dbk_"
_TOKEN_BYTES = 32
MAX_NAME_LENGTH = 120
_KEY_ID_RE = re.compile(r"^[a-z0-9]{8,64}$")

_FILE_LOCKS: dict[Path, threading.Lock] = {}
_FILE_LOCKS_LOCK = threading.Lock()


class InvalidApiKeyError(ValueError):
    """Raised when an API-key request is not usable."""


@dataclass(frozen=True)
class ApiKeyMeta:
    """Safe metadata for one API key -- never carries the token or its hash."""

    user_id: str
    key_id: str
    name: str
    created_at: str
    last_used_at: str

    def public(self) -> dict[str, str]:
        return {
            "key_id": self.key_id,
            "name": self.name,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }


def api_keys_path(base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    root = Path(base_dir) / IDENTITY_DIR
    path = root / API_KEYS_FILE
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("API keys path escapes identity directory") from exc
    return path


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidApiKeyError(f"{name} is required")
    return value.strip()


def _required_name(name: str) -> str:
    text = _required_text(name, "name")
    if len(text) > MAX_NAME_LENGTH:
        raise InvalidApiKeyError(f"name must be at most {MAX_NAME_LENGTH} characters")
    return text


def create_api_key(
    user_id: str,
    name: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    now: datetime | None = None,
) -> tuple[dict[str, str], str]:
    """Mint a key for a user. Returns (safe metadata, RAW TOKEN). The raw token
    is the ONLY time it exists -- it is never stored or recoverable."""
    normalized_user_id = _required_text(user_id, "user_id")
    normalized_name = _required_name(name)
    raw_token = TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)
    key_id = secrets.token_hex(12)
    stamp = _format_timestamp(now or _now())
    entry = ApiKeyMeta(
        user_id=normalized_user_id, key_id=key_id, name=normalized_name,
        created_at=stamp, last_used_at="",
    )
    path = api_keys_path(base_dir)
    with _file_lock(path):
        entries = _read_entries(path)
        entries.append((entry, object_identity.hash_token(raw_token)))
        _write_entries(path, entries)
    return entry.public(), raw_token


def list_api_keys(user_id: str, *, base_dir: Path | str = DEFAULT_DATA_DIR) -> list[dict[str, str]]:
    """A user's keys as safe metadata (never the token or hash)."""
    normalized_user_id = _required_text(user_id, "user_id")
    path = api_keys_path(base_dir)
    with _file_lock(path):
        entries = _read_entries(path)
    return [meta.public() for meta, _hash in entries if meta.user_id == normalized_user_id]


def revoke_api_key(user_id: str, key_id: str, *, base_dir: Path | str = DEFAULT_DATA_DIR) -> bool:
    """Delete one of a user's keys. Returns True when a key was removed."""
    normalized_user_id = _required_text(user_id, "user_id")
    if not _KEY_ID_RE.fullmatch(key_id or ""):
        raise InvalidApiKeyError("invalid key_id")
    path = api_keys_path(base_dir)
    with _file_lock(path):
        entries = _read_entries(path)
        kept = [(m, h) for (m, h) in entries if not (m.user_id == normalized_user_id and m.key_id == key_id)]
        if len(kept) == len(entries):
            return False
        _write_entries(path, kept)
    return True


def resolve_api_key(token: str | None, *, base_dir: Path | str = DEFAULT_DATA_DIR) -> str | None:
    """Return the user_id a raw API-key token authenticates as, or None. Rejects
    anything without the `dbk_` prefix immediately (so a session token never
    reaches this path). Constant-time hash comparison, same as sessions.

    Deliberately does NOT update last_used_at -- that would be a file write on
    every authenticated request. A future daemon/lazy update can add it."""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    presented = object_identity.hash_token(token)
    path = api_keys_path(base_dir)
    try:
        with _file_lock(path):
            entries = _read_entries(path)
    except OSError:
        return None
    for meta, stored_hash in entries:
        if hmac.compare_digest(stored_hash, presented):
            return meta.user_id
    return None


# --- file IO (mirrors object_service_keys' owner-only 0600 posture) --------

def _file_lock(path: Path) -> threading.Lock:
    resolved = path.resolve(strict=False)
    with _FILE_LOCKS_LOCK:
        lock = _FILE_LOCKS.get(resolved)
        if lock is None:
            lock = threading.Lock()
            _FILE_LOCKS[resolved] = lock
        return lock


def _read_entries(path: Path) -> list[tuple[ApiKeyMeta, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    entries: list[tuple[ApiKeyMeta, str]] = []
    for row in csv.DictReader(text.splitlines(), delimiter="\t"):
        token_hash = (row.get("token_hash") or "").strip()
        user_id = (row.get("user_id") or "").strip()
        key_id = (row.get("key_id") or "").strip()
        if not (token_hash and user_id and key_id):
            continue
        entries.append((
            ApiKeyMeta(
                user_id=user_id, key_id=key_id, name=(row.get("name") or "").strip(),
                created_at=(row.get("created_at") or "").strip(),
                last_used_at=(row.get("last_used_at") or "").strip(),
            ),
            token_hash,
        ))
    return entries


def _write_entries(path: Path, entries: Iterable[tuple[ApiKeyMeta, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=API_KEY_FIELDS, delimiter="\t")
        writer.writeheader()
        for meta, token_hash in entries:
            writer.writerow({
                "user_id": meta.user_id, "key_id": meta.key_id, "name": meta.name,
                "token_hash": token_hash, "created_at": meta.created_at,
                "last_used_at": meta.last_used_at,
            })
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, path)
