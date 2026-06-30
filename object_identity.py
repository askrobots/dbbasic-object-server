"""File-backed identity sessions for DBBASIC permission checks."""

from __future__ import annotations

import csv
import hashlib
import hmac
import json
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import object_permissions
from object_versions import DEFAULT_DATA_DIR

IDENTITY_DIR = "identity"
SESSIONS_FILE = "sessions.tsv"
DEFAULT_SESSION_TTL_SECONDS = 24 * 60 * 60
MAX_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
SESSION_FIELDS = (
    "session_id",
    "token_hash",
    "user_id",
    "account_id",
    "roles",
    "subscriptions",
    "label",
    "created_at",
    "expires_at",
    "revoked_at",
)

_SESSION_ID_RE = re.compile(r"^sess_[A-Za-z0-9_-]{12,96}$")
_FILE_LOCKS: dict[Path, threading.Lock] = {}
_FILE_LOCKS_LOCK = threading.Lock()


class SessionNotFoundError(LookupError):
    """Raised when a session id does not exist."""


class InvalidSessionPayloadError(ValueError):
    """Raised when a session payload is not usable."""


@dataclass(frozen=True)
class IdentitySession:
    """A stored identity session without its raw token."""

    session_id: str
    token_hash: str
    user_id: str
    account_id: str | None
    roles: tuple[str, ...]
    subscriptions: tuple[str, ...]
    label: str
    created_at: str
    expires_at: str
    revoked_at: str | None = None

    def is_active(self, now: datetime | None = None) -> bool:
        if self.revoked_at:
            return False
        expires_at = _parse_timestamp(self.expires_at)
        return expires_at > (now or _now())

    def subject(self) -> object_permissions.PermissionSubject:
        return object_permissions.PermissionSubject(
            user_id=self.user_id,
            account_id=self.account_id,
            roles=self.roles,
            subscriptions=self.subscriptions,
        )

    def public_payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "account_id": self.account_id,
            "roles": list(self.roles),
            "subscriptions": list(self.subscriptions),
            "label": self.label,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "revoked_at": self.revoked_at,
            "active": self.is_active(),
        }


def sessions_path(base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    """Return the validated session TSV path."""
    root = Path(base_dir) / IDENTITY_DIR
    path = root / SESSIONS_FILE
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)

    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("Identity session path escapes identity directory") from exc

    return path


def create_session(
    payload: Mapping[str, Any],
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create a session and return its public payload plus the one-time token."""
    created_at = now or _now()
    token = secrets.token_urlsafe(32)
    session = IdentitySession(
        session_id=_new_session_id(),
        token_hash=hash_token(token),
        user_id=_required_text(payload.get("user_id"), "user_id"),
        account_id=_optional_text(payload.get("account_id"), "account_id"),
        roles=_string_tuple(payload.get("roles", ()), "roles"),
        subscriptions=_string_tuple(payload.get("subscriptions", ()), "subscriptions"),
        label=_optional_text(payload.get("label"), "label") or "",
        created_at=_format_timestamp(created_at),
        expires_at=_format_timestamp(
            created_at + timedelta(seconds=_ttl_seconds(payload.get("ttl_seconds")))
        ),
    )

    path = sessions_path(base_dir)
    with _file_lock(path):
        sessions = _read_sessions(path)
        sessions.append(session)
        _write_sessions(path, sessions)

    return {"session": session.public_payload(), "token": token}


def list_sessions(
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> list[dict[str, Any]]:
    """Return all sessions without raw tokens or token hashes."""
    return [session.public_payload() for session in _read_sessions(sessions_path(base_dir))]


def get_session(
    session_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    """Return one public session payload."""
    session = _find_session(session_id, base_dir=base_dir)
    return session.public_payload()


def revoke_session(
    session_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Mark one session revoked and return its public payload."""
    if not validate_session_id(session_id):
        raise InvalidSessionPayloadError(f"Invalid session id: {session_id}")

    path = sessions_path(base_dir)
    revoked_at = _format_timestamp(now or _now())
    with _file_lock(path):
        sessions = _read_sessions(path)
        updated: list[IdentitySession] = []
        found: IdentitySession | None = None
        for session in sessions:
            if session.session_id == session_id:
                found = IdentitySession(
                    session_id=session.session_id,
                    token_hash=session.token_hash,
                    user_id=session.user_id,
                    account_id=session.account_id,
                    roles=session.roles,
                    subscriptions=session.subscriptions,
                    label=session.label,
                    created_at=session.created_at,
                    expires_at=session.expires_at,
                    revoked_at=session.revoked_at or revoked_at,
                )
                updated.append(found)
            else:
                updated.append(session)

        if found is None:
            raise SessionNotFoundError(f"Session not found: {session_id}")

        _write_sessions(path, updated)
        return found.public_payload()


def resolve_session_token(
    token: str | None,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    now: datetime | None = None,
) -> IdentitySession | None:
    """Return the active session matching a raw token, if any."""
    if not token:
        return None

    token_hash = hash_token(token)
    checked_at = now or _now()
    for session in _read_sessions(sessions_path(base_dir)):
        if not hmac.compare_digest(session.token_hash, token_hash):
            continue
        if session.is_active(checked_at):
            return session
        return None

    return None


def hash_token(token: str) -> str:
    """Return the stored hash for a raw session token."""
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_session_id(session_id: str) -> bool:
    """Return True when a session id is route-safe."""
    return isinstance(session_id, str) and bool(_SESSION_ID_RE.fullmatch(session_id))


def _find_session(
    session_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> IdentitySession:
    if not validate_session_id(session_id):
        raise InvalidSessionPayloadError(f"Invalid session id: {session_id}")

    for session in _read_sessions(sessions_path(base_dir)):
        if session.session_id == session_id:
            return session

    raise SessionNotFoundError(f"Session not found: {session_id}")


def _read_sessions(path: Path) -> list[IdentitySession]:
    if not path.exists():
        return []

    sessions: list[IdentitySession] = []
    with path.open(newline="") as handle:
        rows = csv.DictReader(handle, delimiter="\t")
        for row in rows:
            if not row:
                continue
            try:
                sessions.append(_session_from_row(row))
            except (InvalidSessionPayloadError, ValueError):
                continue
    return sessions


def _write_sessions(path: Path, sessions: Iterable[IdentitySession]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    with temp_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SESSION_FIELDS, delimiter="\t")
        writer.writeheader()
        for session in sessions:
            writer.writerow(_session_to_row(session))
    temp_path.replace(path)


def _session_from_row(row: Mapping[str, str]) -> IdentitySession:
    session_id = str(row.get("session_id", ""))
    if not validate_session_id(session_id):
        raise InvalidSessionPayloadError(f"Invalid session id: {session_id}")

    token_hash = _stored_token_hash(row.get("token_hash"))
    created_at = _required_text(row.get("created_at"), "created_at")
    expires_at = _required_text(row.get("expires_at"), "expires_at")
    _parse_timestamp(created_at)
    _parse_timestamp(expires_at)

    return IdentitySession(
        session_id=session_id,
        token_hash=token_hash,
        user_id=_required_text(row.get("user_id"), "user_id"),
        account_id=_empty_to_none(row.get("account_id")),
        roles=_json_string_tuple(row.get("roles", "[]"), "roles"),
        subscriptions=_json_string_tuple(row.get("subscriptions", "[]"), "subscriptions"),
        label=str(row.get("label", "")),
        created_at=created_at,
        expires_at=expires_at,
        revoked_at=_empty_to_none(row.get("revoked_at")),
    )


def _session_to_row(session: IdentitySession) -> dict[str, str]:
    return {
        "session_id": session.session_id,
        "token_hash": session.token_hash,
        "user_id": session.user_id,
        "account_id": session.account_id or "",
        "roles": json.dumps(list(session.roles), separators=(",", ":")),
        "subscriptions": json.dumps(list(session.subscriptions), separators=(",", ":")),
        "label": session.label,
        "created_at": session.created_at,
        "expires_at": session.expires_at,
        "revoked_at": session.revoked_at or "",
    }


def _new_session_id() -> str:
    return "sess_" + secrets.token_urlsafe(18).replace("-", "_")


def _ttl_seconds(value: Any) -> int:
    if value is None:
        return DEFAULT_SESSION_TTL_SECONDS
    if isinstance(value, bool):
        raise InvalidSessionPayloadError("ttl_seconds must be an integer")
    try:
        ttl = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidSessionPayloadError("ttl_seconds must be an integer") from exc

    if ttl <= 0:
        raise InvalidSessionPayloadError("ttl_seconds must be greater than zero")
    return min(ttl, MAX_SESSION_TTL_SECONDS)


def _required_text(value: Any, field: str) -> str:
    text = _optional_text(value, field)
    if text is None:
        raise InvalidSessionPayloadError(f"{field} is required")
    return text


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidSessionPayloadError(f"{field} must be a string")
    text = value.strip()
    if not text:
        return None
    if len(text) > 256 or any(ord(char) < 32 for char in text):
        raise InvalidSessionPayloadError(f"{field} contains unsafe text")
    return text


def _string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        raise InvalidSessionPayloadError(f"{field} must be a list of strings")

    output: list[str] = []
    seen = set()
    for item in values:
        text = _required_text(item, field)
        if text not in seen:
            output.append(text)
            seen.add(text)
    return tuple(output)


def _json_string_tuple(value: str, field: str) -> tuple[str, ...]:
    try:
        payload = json.loads(value or "[]")
    except json.JSONDecodeError as exc:
        raise InvalidSessionPayloadError(f"{field} is invalid JSON") from exc
    return _string_tuple(payload, field)


def _stored_token_hash(value: str | None) -> str:
    text = (value or "").strip()
    if not text.startswith("sha256:") or len(text) != len("sha256:") + 64:
        raise InvalidSessionPayloadError("token_hash is invalid")
    return text


def _empty_to_none(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed


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
