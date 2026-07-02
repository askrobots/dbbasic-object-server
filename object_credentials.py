"""File-backed password credentials for DBBASIC identity users.

Credential hashes live in their own TSV under the identity directory, separate
from users.tsv, so user API payloads can never include credential material.
Hashes use scrypt from the standard library and verification is constant-time.
The credentials file is written with owner-only permissions and belongs to the
runtime data directory, which stays out of source control.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from object_versions import DEFAULT_DATA_DIR

IDENTITY_DIR = "identity"
CREDENTIALS_FILE = "credentials.tsv"
CREDENTIAL_FIELDS = (
    "user_id",
    "password_hash",
    "created_at",
    "updated_at",
)
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 1024
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_SALT_BYTES = 16
SCRYPT_KEY_BYTES = 32
HASH_PREFIX = "scrypt"

_FILE_LOCKS: dict[Path, threading.Lock] = {}
_FILE_LOCKS_LOCK = threading.Lock()


class InvalidPasswordError(ValueError):
    """Raised when a password payload is not usable."""


@dataclass(frozen=True)
class StoredCredential:
    """One user's stored password hash."""

    user_id: str
    password_hash: str
    created_at: str
    updated_at: str


def credentials_path(base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    root = Path(base_dir) / IDENTITY_DIR
    path = root / CREDENTIALS_FILE
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)

    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("Credentials path escapes identity directory") from exc

    return path


def hash_password(password: str) -> str:
    """Hash one password into the stored scrypt format."""
    _validate_password(password)
    salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    derived = _derive_key(password, salt)
    return (
        f"{HASH_PREFIX}:{SCRYPT_N}:{SCRYPT_R}:{SCRYPT_P}"
        f":{salt.hex()}:{derived.hex()}"
    )


def set_password(
    user_id: str,
    password: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    now: datetime | None = None,
) -> dict[str, str]:
    """Create or replace one user's password hash and return safe metadata."""
    normalized_user_id = _required_user_id(user_id)
    password_hash = hash_password(password)
    timestamp = _format_timestamp(now or _now())

    path = credentials_path(base_dir)
    with _file_lock(path):
        credentials = _read_credentials(path)
        existing = _pop_credential(credentials, normalized_user_id)
        credentials.append(
            StoredCredential(
                user_id=normalized_user_id,
                password_hash=password_hash,
                created_at=existing.created_at if existing is not None else timestamp,
                updated_at=timestamp,
            )
        )
        _write_credentials(path, credentials)

    return {
        "user_id": normalized_user_id,
        "operation": "replaced" if existing is not None else "created",
        "updated_at": timestamp,
    }


def verify_password(
    user_id: str,
    password: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> bool:
    """Verify one password in constant time; unknown users burn one dummy hash."""
    if not isinstance(password, str) or not isinstance(user_id, str):
        return False

    credential = _find_credential(user_id.strip(), base_dir=base_dir)
    if credential is None:
        _burn_dummy_hash(password)
        return False

    try:
        salt, expected = _parse_hash(credential.password_hash)
    except InvalidPasswordError:
        _burn_dummy_hash(password)
        return False

    try:
        derived = _derive_key(password, salt)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, expected)


def remove_password(
    user_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> bool:
    """Delete one user's stored hash; return whether one existed."""
    normalized_user_id = _required_user_id(user_id)
    path = credentials_path(base_dir)
    with _file_lock(path):
        credentials = _read_credentials(path)
        existing = _pop_credential(credentials, normalized_user_id)
        if existing is None:
            return False
        _write_credentials(path, credentials)
    return True


def has_password(
    user_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> bool:
    """Return whether one user has a stored password hash."""
    if not isinstance(user_id, str):
        return False
    return _find_credential(user_id.strip(), base_dir=base_dir) is not None


def _validate_password(password: str) -> None:
    if not isinstance(password, str):
        raise InvalidPasswordError("password must be a string")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise InvalidPasswordError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise InvalidPasswordError(
            f"password must be at most {MAX_PASSWORD_LENGTH} characters"
        )


def _derive_key(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_KEY_BYTES,
    )


def _burn_dummy_hash(password: str) -> None:
    try:
        _derive_key(password if isinstance(password, str) else "", b"\x00" * SCRYPT_SALT_BYTES)
    except (ValueError, TypeError):
        pass


def _parse_hash(stored: str) -> tuple[bytes, bytes]:
    parts = (stored or "").split(":")
    if len(parts) != 6 or parts[0] != HASH_PREFIX:
        raise InvalidPasswordError("stored password hash is invalid")
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = bytes.fromhex(parts[4])
        expected = bytes.fromhex(parts[5])
    except ValueError as exc:
        raise InvalidPasswordError("stored password hash is invalid") from exc
    if (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P) or not salt or not expected:
        raise InvalidPasswordError("stored password hash is invalid")
    return salt, expected


def _required_user_id(user_id: str) -> str:
    if not isinstance(user_id, str):
        raise InvalidPasswordError("user_id must be a string")
    text = user_id.strip()
    if not text:
        raise InvalidPasswordError("user_id is required")
    return text


def _find_credential(
    user_id: str,
    *,
    base_dir: Path | str,
) -> StoredCredential | None:
    if not user_id:
        return None
    for credential in _read_credentials(credentials_path(base_dir)):
        if credential.user_id == user_id:
            return credential
    return None


def _pop_credential(
    credentials: list[StoredCredential],
    user_id: str,
) -> StoredCredential | None:
    for index, credential in enumerate(credentials):
        if credential.user_id == user_id:
            return credentials.pop(index)
    return None


def _read_credentials(path: Path) -> list[StoredCredential]:
    if not path.exists():
        return []

    credentials: list[StoredCredential] = []
    with path.open(newline="") as handle:
        rows = csv.DictReader(handle, delimiter="\t")
        for row in rows:
            if not row:
                continue
            credential = _credential_from_row(row)
            if credential is not None:
                credentials.append(credential)
    return credentials


def _write_credentials(path: Path, credentials: Iterable[StoredCredential]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    with temp_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CREDENTIAL_FIELDS, delimiter="\t")
        writer.writeheader()
        for credential in credentials:
            writer.writerow(
                {
                    "user_id": credential.user_id,
                    "password_hash": credential.password_hash,
                    "created_at": credential.created_at,
                    "updated_at": credential.updated_at,
                }
            )
    os.chmod(temp_path, 0o600)
    temp_path.replace(path)


def _credential_from_row(row: Mapping[str, str]) -> StoredCredential | None:
    user_id = (row.get("user_id") or "").strip()
    password_hash = (row.get("password_hash") or "").strip()
    created_at = (row.get("created_at") or "").strip()
    updated_at = (row.get("updated_at") or "").strip()
    if not user_id or not password_hash or not created_at or not updated_at:
        return None
    return StoredCredential(
        user_id=user_id,
        password_hash=password_hash,
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
