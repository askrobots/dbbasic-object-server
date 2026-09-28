"""Self-service signup, email verification and password reset.

This module owns three things; object_server wires the browser routes
(/signup, /verify, /reset, /reset/confirm) to them.

1. One-time email tokens, in ``identity/email_tokens.tsv`` (mode 0600).
   A token is ``secrets.token_urlsafe(32)`` and only its
   ``object_identity.hash_token`` is stored -- the session-token pattern.
   Each is single use and expires (verification 48 h, reset 1 h). Issuing a
   new link for a user and purpose retires the previous one.
   Bounded: every write drops used and expired rows, so the file holds at
   most one live row per user and purpose.

2. The checks that decide whether signup and reset may run here. A
   capability that is absent refuses rather than pretends:
   - no way to send the email: refuse;
   - no configured public address for the link: refuse. Never the request's
     Host header, which would let anyone point a reset link at their own
     domain;
   - a policy that gives every signed-in user unscoped access: refuse signup.
     That grant is fine for operator-invited users and wrong for strangers.

3. The account itself. A signup creates its own account and a user in the
   ``pending`` status, which cannot log in or hold a session until the
   emailed link is used (object_identity.PENDING_STATUS).

No enumeration: a signup for an address that already has an account, and a
reset for an address that has none, answer the person at the form exactly as
the ordinary case does. Only the mailbox's owner learns which it was.
"""

from __future__ import annotations

import csv
import hmac
import os
import re
import secrets
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import object_credentials
import object_email
import object_identity
import object_permissions
import object_schemas
from object_versions import DEFAULT_DATA_DIR

SIGNUP_ENV = "DBBASIC_ENABLE_SIGNUP"
PASSWORD_RESET_ENV = "DBBASIC_ENABLE_PASSWORD_RESET"
PUBLIC_URL_ENV = "DBBASIC_PUBLIC_URL"

VERIFY_EMAIL = "verify_email"
PASSWORD_RESET = "password_reset"
PURPOSES = frozenset({VERIFY_EMAIL, PASSWORD_RESET})
TOKEN_TTL = {
    VERIFY_EMAIL: timedelta(hours=48),
    PASSWORD_RESET: timedelta(hours=1),
}

TOKENS_FILE = "email_tokens.tsv"
TOKEN_FIELDS = ("token_hash", "purpose", "user_id", "email", "created_at", "expires_at", "used_at")

MAX_EMAIL_LENGTH = 254
MAX_DISPLAY_NAME_LENGTH = 120
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TRUE_VALUES = {"1", "true", "yes", "on"}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

_TOKENS_LOCK = threading.Lock()
# Check-then-create for one email address is not atomic in object_identity
# (it locks per file, per call). One process-wide lock keeps two signups for
# the same address from both passing the "no account yet" check.
_REGISTER_LOCK = threading.Lock()


class SignupError(ValueError):
    """A signup or reset form value the person has to fix (the message says what)."""


# --------------------------------------------------------------------------
# Whether signup and reset may run


def env_enabled(name: str, env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return (env.get(name) or "").strip().lower() in _TRUE_VALUES


def public_url(env: Mapping[str, str] | None = None) -> str | None:
    """The configured public origin for emailed links, or None.

    Must be https, except on a local host (a developer's own machine)."""
    env = os.environ if env is None else env
    value = (env.get(PUBLIC_URL_ENV) or "").strip().rstrip("/")
    if not value:
        return None
    parsed = urllib.parse.urlsplit(value)
    if not parsed.netloc or parsed.query or parsed.fragment:
        return None
    if parsed.scheme == "https":
        return value
    if parsed.scheme == "http" and (parsed.hostname or "") in _LOCAL_HOSTS:
        return value
    return None


def email_blockers(
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """What stops this server from emailing a link, or [] if nothing."""
    blockers: list[str] = []
    if not object_email.smtp_config_from_env(os.environ if env is None else env).configured:
        blockers.append("Outbound email is not configured: set DBBASIC_SMTP_MODE=live and DBBASIC_SMTP_HOST (or DBBASIC_SMTP_MODE=log to test).")
    try:
        object_schemas.get_schema(object_email.OUTBOX_COLLECTION, base_dir=base_dir)
    except (object_schemas.SchemaNotFoundError, OSError, ValueError):
        blockers.append("The app-email package is not installed, so there is no outbox to queue the email in.")
    if not object_email.email_pass_enabled(base_dir=base_dir):
        blockers.append("Outbound email is switched off (feature flag email_enabled).")
    if public_url(env) is None:
        blockers.append(f"{PUBLIC_URL_ENV} is not set to this server's https address, which emailed links must use.")
    return blockers


def unscoped_registered_rules(policy: object_permissions.PermissionPolicy) -> list[str]:
    """Allow rules that give every signed-in user something on everything.

    A rule for the ``registered`` principal with no collection, no object and
    no row filter applies to all data and every object -- the starter policy's
    "signed-in users can read and run objects" is one. With open signup,
    "signed-in" means "anyone with an email address"."""
    found = []
    for rule in policy.rules:
        if rule.effect != "allow" or rule.principal != "registered":
            continue
        if rule.collection or rule.object_id or rule.row_filter:
            continue
        actions = ", ".join(sorted(rule.actions))
        found.append(f"registered: {actions}" + (f" ({rule.reason})" if rule.reason else ""))
    return found


def signup_blockers(
    *,
    policy: object_permissions.PermissionPolicy,
    enforcement_enabled: bool,
    password_login_enabled: bool,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Everything that keeps signup off here, or [] when it may run."""
    blockers: list[str] = []
    if not env_enabled(SIGNUP_ENV, env):
        blockers.append(f"Signup is off: set {SIGNUP_ENV}=true.")
    if not password_login_enabled:
        blockers.append("Password login is off, so a new user could never sign in: set DBBASIC_ENABLE_PASSWORD_LOGIN=true.")
    if not enforcement_enabled:
        blockers.append("Permission enforcement is off, so a stranger's account would not be limited to their own data: set DBBASIC_ENABLE_PERMISSION_ENFORCEMENT=true.")
    for rule in unscoped_registered_rules(policy):
        blockers.append(f"The permission policy gives every signed-in user unscoped access ({rule}); scope or remove it before opening signup.")
    return blockers + email_blockers(base_dir=base_dir, env=env)


def reset_blockers(
    *,
    password_login_enabled: bool,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Everything that keeps password reset off here, or [] when it may run."""
    blockers: list[str] = []
    if not env_enabled(PASSWORD_RESET_ENV, env):
        blockers.append(f"Password reset is off: set {PASSWORD_RESET_ENV}=true.")
    if not password_login_enabled:
        blockers.append("Password login is off: set DBBASIC_ENABLE_PASSWORD_LOGIN=true.")
    return blockers + email_blockers(base_dir=base_dir, env=env)


# --------------------------------------------------------------------------
# One-time tokens


def tokens_path(base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    return Path(base_dir) / object_identity.IDENTITY_DIR / TOKENS_FILE


def issue_token(
    purpose: str,
    user_id: str,
    email: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    now: datetime | None = None,
) -> str:
    """Store a new single-use token and return it raw (the only time it exists raw)."""
    if purpose not in PURPOSES:
        raise ValueError(f"Unknown token purpose: {purpose}")
    now = now or _now()
    token = secrets.token_urlsafe(32)
    row = {
        "token_hash": object_identity.hash_token(token),
        "purpose": purpose,
        "user_id": user_id,
        "email": email,
        "created_at": _stamp(now),
        "expires_at": _stamp(now + TOKEN_TTL[purpose]),
        "used_at": "",
    }
    path = tokens_path(base_dir)
    with _TOKENS_LOCK:
        rows = [
            r for r in _live_rows(_read_tokens(path), now)
            if not (r["user_id"] == user_id and r["purpose"] == purpose)
        ]
        rows.append(row)
        _write_tokens(path, rows)
    return token


def peek_token(
    token: str | None,
    purpose: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    now: datetime | None = None,
) -> dict[str, str] | None:
    """The live row for a raw token, without using it up; None if not live."""
    if not token:
        return None
    return _find_live(_read_tokens(tokens_path(base_dir)), token, purpose, now or _now())


def consume_token(
    token: str | None,
    purpose: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    now: datetime | None = None,
) -> dict[str, str] | None:
    """Use a raw token up and return its row; None if unknown, used, expired or another purpose."""
    if not token:
        return None
    now = now or _now()
    path = tokens_path(base_dir)
    with _TOKENS_LOCK:
        rows = _read_tokens(path)
        found = _find_live(rows, token, purpose, now)
        if found is None:
            return None
        found["used_at"] = _stamp(now)
        # the used row goes too: a used token is never looked at again
        _write_tokens(path, [r for r in _live_rows(rows, now) if r is not found])
    return found


def _find_live(rows: list[dict[str, str]], token: str, purpose: str, now: datetime) -> dict[str, str] | None:
    wanted = object_identity.hash_token(token)
    for row in rows:
        if not hmac.compare_digest(row.get("token_hash", ""), wanted):
            continue
        if row.get("purpose") != purpose or row.get("used_at"):
            return None
        try:
            if _parse(row["expires_at"]) <= now:
                return None
        except (KeyError, ValueError):
            return None
        return row
    return None


def _live_rows(rows: list[dict[str, str]], now: datetime) -> list[dict[str, str]]:
    live = []
    for row in rows:
        if row.get("used_at"):
            continue
        try:
            if _parse(row["expires_at"]) <= now:
                continue
        except (KeyError, ValueError):
            continue
        live.append(row)
    return live


def _read_tokens(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle, delimiter="\t") if row]


def _write_tokens(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TOKEN_FIELDS, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    os.chmod(temp, 0o600)
    temp.replace(path)


# --------------------------------------------------------------------------
# Accounts


def normalize_email(value: str | None) -> str:
    email = (value or "").strip()
    if len(email) > MAX_EMAIL_LENGTH or not _EMAIL_RE.match(email):
        raise SignupError("Enter a valid email address.")
    return email


def check_password(password: str | None) -> str:
    password = password or ""
    if len(password) < object_credentials.MIN_PASSWORD_LENGTH:
        raise SignupError(f"Use at least {object_credentials.MIN_PASSWORD_LENGTH} characters for the password.")
    if len(password) > object_credentials.MAX_PASSWORD_LENGTH:
        raise SignupError("That password is too long.")
    return password


def register(
    email: str,
    password: str,
    display_name: str = "",
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create a pending user in a new account, unless the address already has one.

    Returns ``{"outcome": "created" | "pending" | "exists", "user": {...}}``.
    "pending": the address signed up before and never confirmed (send the
    link again). "exists": it has an account (tell the mailbox, not the form).
    """
    email = normalize_email(email)
    password = check_password(password)
    display_name = (display_name or "").strip()[:MAX_DISPLAY_NAME_LENGTH]
    with _REGISTER_LOCK:
        existing = object_identity.find_users_by_email(email, base_dir=base_dir)
        if existing:
            pending = [u for u in existing if u["status"] == object_identity.PENDING_STATUS]
            if pending and len(existing) == len(pending):
                return {"outcome": "pending", "user": pending[0]}
            return {"outcome": "exists", "user": existing[0]}
        account = object_identity.create_account(
            {"name": email, "status": object_identity.ACTIVE_STATUS},
            base_dir=base_dir,
            now=now,
        )
        user = object_identity.create_user(
            {
                "account_id": account["account_id"],
                "email": email,
                "display_name": display_name,
                "status": object_identity.PENDING_STATUS,
                "roles": [],
            },
            base_dir=base_dir,
            now=now,
        )
        object_credentials.set_password(user["user_id"], password, base_dir=base_dir)
    return {"outcome": "created", "user": user}


def verify_email(
    token: str | None,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Use a verification link: the pending user becomes active. None if the link is no good."""
    row = consume_token(token, VERIFY_EMAIL, base_dir=base_dir, now=now)
    if row is None:
        return None
    return _activate_if_pending(row, base_dir=base_dir, now=now)


def reset_password(
    token: str | None,
    password: str | None,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Use a reset link to set a new password. None if the link is no good.

    Raises SignupError for a password that won't do -- before the link is
    used, so the person can try again with the same link. Every session the
    user had is revoked: whoever knew the old password is signed out. A
    pending user becomes active, since the link proved they hold the mailbox."""
    password = check_password(password)
    if peek_token(token, PASSWORD_RESET, base_dir=base_dir, now=now) is None:
        return None
    row = consume_token(token, PASSWORD_RESET, base_dir=base_dir, now=now)
    if row is None:
        return None
    user = _activate_if_pending(row, base_dir=base_dir, now=now)
    if user is None:
        return None
    object_credentials.set_password(user["user_id"], password, base_dir=base_dir)
    object_identity.revoke_user_sessions(user["user_id"], base_dir=base_dir, now=now)
    return user


def reset_candidate(email: str, *, base_dir: Path | str = DEFAULT_DATA_DIR) -> dict[str, Any] | None:
    """The one user a reset for this address would go to, or None.

    Disabled users get nothing: an operator turned them off, and a reset
    link must not be a way back in."""
    try:
        email = normalize_email(email)
    except SignupError:
        return None
    users = [
        u for u in object_identity.find_users_by_email(email, base_dir=base_dir)
        if u["status"] in (object_identity.ACTIVE_STATUS, object_identity.PENDING_STATUS)
    ]
    return users[0] if len(users) == 1 else None


def _activate_if_pending(row: Mapping[str, str], *, base_dir: Path | str, now: datetime | None) -> dict[str, Any] | None:
    try:
        user = object_identity.get_user(row["user_id"], base_dir=base_dir)
    except (LookupError, ValueError):
        return None
    # the link was sent to one address; if the user's address has changed
    # since, it no longer proves anything about this user
    if (user.get("email") or "").strip().casefold() != (row.get("email") or "").strip().casefold():
        return None
    if user["status"] == object_identity.DISABLED_STATUS:
        return None
    if user["status"] == object_identity.PENDING_STATUS:
        user = object_identity.set_user_status(user["user_id"], object_identity.ACTIVE_STATUS, base_dir=base_dir, now=now)
    return user


# --------------------------------------------------------------------------
# The emails


def link(base_url: str, path: str, token: str | None = None) -> str:
    url = base_url.rstrip("/") + path
    if token is not None:
        url += "?" + urllib.parse.urlencode({"token": token})
    return url


def verification_email(base_url: str, token: str) -> tuple[str, str]:
    hours = int(TOKEN_TTL[VERIFY_EMAIL].total_seconds() // 3600)
    return (
        "Confirm your email address",
        "Someone (hopefully you) created an account with this email address.\n\n"
        f"To confirm it, open this link within {hours} hours:\n\n"
        f"{link(base_url, '/verify', token)}\n\n"
        "If it wasn't you, ignore this email: the account stays unconfirmed and can't be used.\n",
    )


def already_registered_email(base_url: str, reset_available: bool) -> tuple[str, str]:
    body = (
        "Someone (hopefully you) tried to create an account with this email address, "
        "but it already has one.\n\n"
        f"Sign in here: {link(base_url, '/login')}\n"
    )
    if reset_available:
        body += f"Forgot your password? Reset it here: {link(base_url, '/reset')}\n"
    body += "\nIf it wasn't you, ignore this email: nothing has changed.\n"
    return ("You already have an account", body)


def reset_email(base_url: str, token: str) -> tuple[str, str]:
    minutes = int(TOKEN_TTL[PASSWORD_RESET].total_seconds() // 60)
    return (
        "Reset your password",
        "Someone (hopefully you) asked to reset the password for this email address.\n\n"
        f"To choose a new password, open this link within {minutes} minutes:\n\n"
        f"{link(base_url, '/reset/confirm', token)}\n\n"
        "If it wasn't you, ignore this email: your password stays as it is.\n",
    )


def send(to: str, subject_and_body: tuple[str, str], *, base_dir: Path | str = DEFAULT_DATA_DIR) -> str:
    subject, body = subject_and_body
    return object_email.enqueue(to, subject, body, source_object_id="object_signup", base_dir=base_dir)


# --------------------------------------------------------------------------


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _now() -> datetime:
    return datetime.now(timezone.utc)
