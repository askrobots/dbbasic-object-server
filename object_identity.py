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

import object_ids
import object_permissions
from object_versions import DEFAULT_DATA_DIR

IDENTITY_DIR = "identity"
SESSIONS_FILE = "sessions.tsv"
ACCOUNTS_FILE = "accounts.tsv"
USERS_FILE = "users.tsv"
DEFAULT_SESSION_TTL_SECONDS = 24 * 60 * 60
MAX_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
ACTIVE_STATUS = "active"
DISABLED_STATUS = "disabled"
ALLOWED_STATUSES = frozenset({ACTIVE_STATUS, DISABLED_STATUS})
ACCOUNT_FIELDS = (
    "account_id",
    "name",
    "status",
    "subscriptions",
    "created_at",
    "updated_at",
)
USER_FIELDS = (
    "user_id",
    "account_id",
    "email",
    "display_name",
    "status",
    "roles",
    "subscriptions",
    "created_at",
    "updated_at",
)
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

_LEGACY_SESSION_ID_RE = re.compile(r"^sess_[A-Za-z0-9_-]{12,96}$")
_IDENTITY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")
_FILE_LOCKS: dict[Path, threading.Lock] = {}
_FILE_LOCKS_LOCK = threading.Lock()


class SessionNotFoundError(LookupError):
    """Raised when a session id does not exist."""


class AccountNotFoundError(LookupError):
    """Raised when an account id does not exist."""


class UserNotFoundError(LookupError):
    """Raised when a user id does not exist."""


class InvalidSessionPayloadError(ValueError):
    """Raised when a session payload is not usable."""


class InvalidIdentityPayloadError(ValueError):
    """Raised when a user or account payload is not usable."""


@dataclass(frozen=True)
class IdentityAccount:
    """A local account/tenant identity."""

    account_id: str
    name: str
    status: str
    subscriptions: tuple[str, ...]
    created_at: str
    updated_at: str

    def public_payload(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "name": self.name,
            "status": self.status,
            "subscriptions": list(self.subscriptions),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def is_active(self) -> bool:
        return self.status == ACTIVE_STATUS


@dataclass(frozen=True)
class IdentityUser:
    """A local user identity used to mint permission subjects."""

    user_id: str
    account_id: str | None
    email: str | None
    display_name: str
    status: str
    roles: tuple[str, ...]
    subscriptions: tuple[str, ...]
    created_at: str
    updated_at: str

    def public_payload(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "account_id": self.account_id,
            "email": self.email,
            "display_name": self.display_name,
            "status": self.status,
            "roles": list(self.roles),
            "subscriptions": list(self.subscriptions),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def is_active(self) -> bool:
        return self.status == ACTIVE_STATUS


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
    return _identity_path(SESSIONS_FILE, base_dir=base_dir)


def accounts_path(base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    """Return the validated account TSV path."""
    return _identity_path(ACCOUNTS_FILE, base_dir=base_dir)


def users_path(base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    """Return the validated user TSV path."""
    return _identity_path(USERS_FILE, base_dir=base_dir)


def create_account(
    payload: Mapping[str, Any],
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create an account and return its public payload."""
    created_at = _format_timestamp(now or _now())
    account = IdentityAccount(
        account_id=_identity_id(payload.get("account_id"), "account_id", prefix="acct"),
        name=_optional_text(payload.get("name"), "name") or "",
        status=_status(payload.get("status")),
        subscriptions=_string_tuple(payload.get("subscriptions", ()), "subscriptions"),
        created_at=created_at,
        updated_at=created_at,
    )

    path = accounts_path(base_dir)
    with _file_lock(path):
        accounts = _read_accounts(path)
        if any(existing.account_id == account.account_id for existing in accounts):
            raise InvalidIdentityPayloadError(f"Account already exists: {account.account_id}")
        accounts.append(account)
        _write_accounts(path, accounts)

    return account.public_payload()


def list_accounts(
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> list[dict[str, Any]]:
    """Return all accounts."""
    return [account.public_payload() for account in _read_accounts(accounts_path(base_dir))]


def get_account(
    account_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    """Return one account payload."""
    return _find_account(account_id, base_dir=base_dir).public_payload()


def create_user(
    payload: Mapping[str, Any],
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create a user and return its public payload."""
    account_id = _optional_identity_id(payload.get("account_id"), "account_id")
    if account_id is not None:
        _find_account(account_id, base_dir=base_dir)

    created_at = _format_timestamp(now or _now())
    user = IdentityUser(
        user_id=_identity_id(payload.get("user_id"), "user_id", prefix="usr"),
        account_id=account_id,
        email=_optional_text(payload.get("email"), "email"),
        display_name=_optional_text(payload.get("display_name"), "display_name") or "",
        status=_status(payload.get("status")),
        roles=_string_tuple(payload.get("roles", ()), "roles"),
        subscriptions=_string_tuple(payload.get("subscriptions", ()), "subscriptions"),
        created_at=created_at,
        updated_at=created_at,
    )

    path = users_path(base_dir)
    with _file_lock(path):
        users = _read_users(path)
        if any(existing.user_id == user.user_id for existing in users):
            raise InvalidIdentityPayloadError(f"User already exists: {user.user_id}")
        users.append(user)
        _write_users(path, users)

    return user.public_payload()


def list_users(
    *,
    account_id: str | None = None,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> list[dict[str, Any]]:
    """Return all users, optionally scoped to one account."""
    normalized_account_id = _optional_identity_id(account_id, "account_id")
    users = _read_users(users_path(base_dir))
    if normalized_account_id is not None:
        users = [user for user in users if user.account_id == normalized_account_id]
    return [user.public_payload() for user in users]


def get_user(
    user_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    """Return one user payload."""
    return _find_user(user_id, base_dir=base_dir).public_payload()


def create_session(
    payload: Mapping[str, Any],
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    now: datetime | None = None,
    require_known_user: bool = False,
) -> dict[str, Any]:
    """Create a session and return its public payload plus the one-time token."""
    created_at = now or _now()
    token = secrets.token_urlsafe(32)
    user_id = _required_text(payload.get("user_id"), "user_id")
    user = _lookup_user(user_id, base_dir=base_dir)
    account = _session_account(payload, user, base_dir=base_dir)

    if user is None and require_known_user:
        raise UserNotFoundError(f"User not found: {user_id}")
    if user is not None and not user.is_active():
        raise InvalidSessionPayloadError(f"User is not active: {user_id}")
    if account is not None and not account.is_active():
        raise InvalidSessionPayloadError(f"Account is not active: {account.account_id}")

    default_roles = user.roles if user is not None else ()
    default_subscriptions = _merged_strings(
        user.subscriptions if user is not None else (),
        account.subscriptions if account is not None else (),
    )

    session = IdentitySession(
        session_id=_new_session_id(),
        token_hash=hash_token(token),
        user_id=user_id,
        account_id=_session_account_id(payload, user, account),
        roles=_string_tuple(payload.get("roles", default_roles), "roles"),
        subscriptions=_string_tuple(
            payload.get("subscriptions", default_subscriptions),
            "subscriptions",
        ),
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
    if not isinstance(session_id, str):
        return False
    return object_ids.is_uuid4(session_id) or bool(_LEGACY_SESSION_ID_RE.fullmatch(session_id))


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


def _find_account(
    account_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> IdentityAccount:
    normalized = _identity_id(account_id, "account_id")
    for account in _read_accounts(accounts_path(base_dir)):
        if account.account_id == normalized:
            return account
    raise AccountNotFoundError(f"Account not found: {normalized}")


def _find_user(
    user_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> IdentityUser:
    normalized = _identity_id(user_id, "user_id")
    for user in _read_users(users_path(base_dir)):
        if user.user_id == normalized:
            return user
    raise UserNotFoundError(f"User not found: {normalized}")


def _lookup_user(
    user_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> IdentityUser | None:
    try:
        return _find_user(user_id, base_dir=base_dir)
    except UserNotFoundError:
        return None


def _lookup_account(
    account_id: str | None,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> IdentityAccount | None:
    if account_id is None:
        return None
    try:
        return _find_account(account_id, base_dir=base_dir)
    except AccountNotFoundError:
        return None


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


def _read_accounts(path: Path) -> list[IdentityAccount]:
    if not path.exists():
        return []

    accounts: list[IdentityAccount] = []
    with path.open(newline="") as handle:
        rows = csv.DictReader(handle, delimiter="\t")
        for row in rows:
            if not row:
                continue
            try:
                accounts.append(_account_from_row(row))
            except (InvalidIdentityPayloadError, ValueError):
                continue
    return accounts


def _read_users(path: Path) -> list[IdentityUser]:
    if not path.exists():
        return []

    users: list[IdentityUser] = []
    with path.open(newline="") as handle:
        rows = csv.DictReader(handle, delimiter="\t")
        for row in rows:
            if not row:
                continue
            try:
                users.append(_user_from_row(row))
            except (InvalidIdentityPayloadError, ValueError):
                continue
    return users


def _write_sessions(path: Path, sessions: Iterable[IdentitySession]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    with temp_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SESSION_FIELDS, delimiter="\t")
        writer.writeheader()
        for session in sessions:
            writer.writerow(_session_to_row(session))
    temp_path.replace(path)


def _write_accounts(path: Path, accounts: Iterable[IdentityAccount]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    with temp_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ACCOUNT_FIELDS, delimiter="\t")
        writer.writeheader()
        for account in accounts:
            writer.writerow(_account_to_row(account))
    temp_path.replace(path)


def _write_users(path: Path, users: Iterable[IdentityUser]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    with temp_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=USER_FIELDS, delimiter="\t")
        writer.writeheader()
        for user in users:
            writer.writerow(_user_to_row(user))
    temp_path.replace(path)


def _account_from_row(row: Mapping[str, str]) -> IdentityAccount:
    account_id = _identity_id(row.get("account_id"), "account_id")
    created_at = _required_text(row.get("created_at"), "created_at")
    updated_at = _required_text(row.get("updated_at"), "updated_at")
    _parse_timestamp(created_at)
    _parse_timestamp(updated_at)
    return IdentityAccount(
        account_id=account_id,
        name=str(row.get("name", "")),
        status=_status(row.get("status")),
        subscriptions=_json_string_tuple(row.get("subscriptions", "[]"), "subscriptions"),
        created_at=created_at,
        updated_at=updated_at,
    )


def _user_from_row(row: Mapping[str, str]) -> IdentityUser:
    user_id = _identity_id(row.get("user_id"), "user_id")
    created_at = _required_text(row.get("created_at"), "created_at")
    updated_at = _required_text(row.get("updated_at"), "updated_at")
    _parse_timestamp(created_at)
    _parse_timestamp(updated_at)
    return IdentityUser(
        user_id=user_id,
        account_id=_optional_identity_id(row.get("account_id"), "account_id"),
        email=_empty_to_none(row.get("email")),
        display_name=str(row.get("display_name", "")),
        status=_status(row.get("status")),
        roles=_json_string_tuple(row.get("roles", "[]"), "roles"),
        subscriptions=_json_string_tuple(row.get("subscriptions", "[]"), "subscriptions"),
        created_at=created_at,
        updated_at=updated_at,
    )


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


def _account_to_row(account: IdentityAccount) -> dict[str, str]:
    return {
        "account_id": account.account_id,
        "name": account.name,
        "status": account.status,
        "subscriptions": json.dumps(list(account.subscriptions), separators=(",", ":")),
        "created_at": account.created_at,
        "updated_at": account.updated_at,
    }


def _user_to_row(user: IdentityUser) -> dict[str, str]:
    return {
        "user_id": user.user_id,
        "account_id": user.account_id or "",
        "email": user.email or "",
        "display_name": user.display_name,
        "status": user.status,
        "roles": json.dumps(list(user.roles), separators=(",", ":")),
        "subscriptions": json.dumps(list(user.subscriptions), separators=(",", ":")),
        "created_at": user.created_at,
        "updated_at": user.updated_at,
    }


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
    return object_ids.new_uuid4()


def _identity_path(filename: str, *, base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    root = Path(base_dir) / IDENTITY_DIR
    path = root / filename
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)

    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("Identity path escapes identity directory") from exc

    return path


def _identity_id(value: Any, field: str, *, prefix: str | None = None) -> str:
    if value is None and prefix is not None:
        value = object_ids.new_uuid4()
    text = _required_text(value, field)
    if not _IDENTITY_ID_RE.fullmatch(text):
        raise InvalidIdentityPayloadError(f"{field} contains unsafe identifier")
    return text


def _optional_identity_id(value: Any, field: str) -> str | None:
    text = _optional_text(value, field)
    if text is None:
        return None
    if not _IDENTITY_ID_RE.fullmatch(text):
        raise InvalidIdentityPayloadError(f"{field} contains unsafe identifier")
    return text


def _status(value: Any) -> str:
    status = _optional_text(value, "status") or ACTIVE_STATUS
    if status not in ALLOWED_STATUSES:
        raise InvalidIdentityPayloadError(f"status must be one of: {', '.join(sorted(ALLOWED_STATUSES))}")
    return status


def _session_account(
    payload: Mapping[str, Any],
    user: IdentityUser | None,
    *,
    base_dir: Path | str,
) -> IdentityAccount | None:
    payload_account_id = _optional_identity_id(payload.get("account_id"), "account_id")
    if user is not None and payload_account_id and user.account_id != payload_account_id:
        raise InvalidSessionPayloadError("account_id does not match registered user")

    account_id = payload_account_id or (user.account_id if user is not None else None)
    if account_id is None:
        return None

    account = _lookup_account(account_id, base_dir=base_dir)
    if account is None and user is not None:
        raise AccountNotFoundError(f"Account not found: {account_id}")
    return account


def _session_account_id(
    payload: Mapping[str, Any],
    user: IdentityUser | None,
    account: IdentityAccount | None,
) -> str | None:
    payload_account_id = _optional_identity_id(payload.get("account_id"), "account_id")
    if payload_account_id is not None:
        return payload_account_id
    if account is not None:
        return account.account_id
    return user.account_id if user is not None else None


def _merged_strings(*groups: Iterable[str]) -> tuple[str, ...]:
    merged: list[str] = []
    seen = set()
    for group in groups:
        for item in group:
            if item not in seen:
                merged.append(item)
                seen.add(item)
    return tuple(merged)


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
