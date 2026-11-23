"""First-owner setup for a server installed by someone else's button.

A one-click install (GitStable's "Install on DigitalOcean", 2026-11-23) puts
a fresh object server on the public internet with nobody signed up. Someone
has to become its first admin, and the obvious ways are all wrong:

- a default login and password: the same on every box, so known to everyone;
- the installer generating a password and the box serving it at a URL: the
  box can't tell its owner from the first stranger who asks;
- "SSH in and run create-superuser": right for an operator, not one click.

What it does instead. The installer (GitStable) makes a one-time setup code,
shows it only to the person who pressed the button, on their own install
page, and sends the box only its SHA-256 (as the cloud-init file
/etc/dbbasic-setup-code.sha256, which scripts/one-click-install.sh moves to
``identity/setup_code.sha256`` in the data directory). DigitalOcean's metadata
and the box's disk hold only the hash. The person opens
``https://<box>/setup#code=...`` -- the code after ``#`` never leaves their
browser in a request -- and chooses their own email and password. Their
password never passes through the installer or the cloud provider.

While the code file is there and no admin exists, the server is *waiting*:
every route but ``/health`` and ``/setup`` is refused (object_server gates it),
so nothing on the box can be read or changed before it has an owner. Setup
is a claim, not a write: the code file is renamed away before the admin is
created, so of two setups at once exactly one gets the box (the other finds
the file gone). Then the file is deleted and setup is closed for good.

A code is ``CODE_LENGTH`` characters of Crockford base32 (100 bits), so the
hash needs no salt or slow KDF: there is nothing to guess with a dictionary.
Typing it is forgiving: case, spaces and dashes are ignored, and the letters
base32 leaves out (I, L, O) are read as the digits they look like.

If the code is lost the box stays waiting, harmlessly. Its owner can still
SSH in and run ``object_identity_cli.py create-superuser``: once an admin
exists the box is no longer waiting, whatever file is left.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from pathlib import Path
from typing import Any

import object_credentials
import object_identity
import object_ids
from object_versions import DEFAULT_DATA_DIR

SETUP_PATH = "/setup"
CODE_FILE = "setup_code.sha256"
CODE_LENGTH = 20
ADMIN_ROLES = frozenset({"admin", "superuser"})
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # Crockford base32
_READ_AS = str.maketrans({"I": "1", "L": "1", "O": "0"})
_HASH_RE = re.compile(r"^sha256:([0-9a-f]{64})$")


class SetupError(ValueError):
    """Something the person at the form can fix; the message is shown to them."""


def code_path(base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    return Path(base_dir) / object_identity.IDENTITY_DIR / CODE_FILE


def normalize_code(value: str | None) -> str:
    """The code as it was made: upper case, no spaces or dashes, I/L/O read as 1/1/0."""
    text = re.sub(r"[\s-]+", "", str(value or "")).upper().translate(_READ_AS)
    return text if len(text) == CODE_LENGTH and all(c in _ALPHABET for c in text) else ""


def code_hash(code: str) -> str:
    """What the box keeps: ``sha256:<hex>`` of the normalized code."""
    return "sha256:" + hashlib.sha256(normalize_code(code).encode("ascii")).hexdigest()


def new_code() -> str:
    """A fresh code, for tests and for an operator making one by hand. GitStable makes its own."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))


def display(code: str) -> str:
    """XXXXX-XXXXX-XXXXX-XXXXX, for reading aloud or typing."""
    plain = normalize_code(code)
    return "-".join(plain[i:i + 5] for i in range(0, len(plain), 5))


def _stored_hash(base_dir: Path | str) -> str | None:
    try:
        text = code_path(base_dir).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text if _HASH_RE.match(text) else None


def _admin_exists(base_dir: Path | str) -> bool:
    try:
        users = object_identity.list_users(base_dir=base_dir)
    except ValueError:
        return True      # an identity store we can't read is not an empty one: don't hand the box out
    return any(ADMIN_ROLES.intersection(user.get("roles") or ()) for user in users)


def _readable_id(email: str, base_dir: Path | str) -> str:
    """The email's name part ("owner" for owner@example.org) when it makes a valid, unused id, so pages say
    "signed in as owner" rather than a long random id (seen on the first test box). Else a random one."""
    name = re.sub(r"[^a-z0-9_.-]", "", email.split("@", 1)[0].lower())[:40]
    if name and name[0].isalnum():
        try:
            object_identity.get_user(name, base_dir=base_dir)
        except LookupError:
            return name
        except ValueError:
            pass
    return object_ids.new_uuid4()


def waiting(base_dir: Path | str = DEFAULT_DATA_DIR) -> bool:
    """True while this box has a setup code and no admin: only /health and /setup may answer."""
    if not code_path(base_dir).exists():
        return False
    return not _admin_exists(base_dir)


def complete(
    code: str | None,
    email: str | None,
    password: str | None,
    display_name: str | None = None,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> dict[str, Any] | None:
    """Make the first admin. Returns the user, or None when the code is wrong or setup is already done.

    Raises SetupError for a bad email or password, before anything is claimed, so the person can fix it
    and the code still works."""
    from object_signup import SignupError, check_password, normalize_email   # the same rules as signup

    try:
        email = normalize_email(email)
        password = check_password(password)
    except SignupError as exc:
        raise SetupError(str(exc)) from exc
    name = " ".join(str(display_name or "").split())[:100]

    if not waiting(base_dir):
        return None
    stored = _stored_hash(base_dir)
    given = normalize_code(code)
    if stored is None or not given or not hmac.compare_digest(code_hash(given), stored):
        return None

    path = code_path(base_dir)
    claimed = path.with_name(f"{CODE_FILE}.claimed-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        os.rename(path, claimed)          # the claim: of two setups at once, one rename finds nothing
    except FileNotFoundError:
        return None
    if _admin_exists(base_dir):           # made from the shell meanwhile: setup is over
        claimed.unlink(missing_ok=True)
        return None
    # The password first, then the user: an admin that exists is what ends "waiting", so it must never
    # exist without the password that lets its owner in.
    user_id = _readable_id(email, base_dir)
    try:
        object_credentials.set_password(user_id, password, base_dir=base_dir)
        try:
            user = object_identity.create_user(
                {"user_id": user_id, "email": email, "display_name": name, "roles": ["admin"]},
                base_dir=base_dir)
        except Exception:
            object_credentials.remove_password(user_id, base_dir=base_dir)
            raise
    except Exception:
        os.rename(claimed, path)          # nothing was made: the code still works
        raise
    claimed.unlink(missing_ok=True)
    return user
