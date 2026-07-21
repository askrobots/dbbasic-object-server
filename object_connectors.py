"""External connectors -- reconcile a collection against an outside system.

The generic half of plan/vocabulary/03-external-connectors-spec.md. A package
declares that one of its collections is kept in sync with an external system
(a mail server, a payment processor, ...) by pointing at a connector module:

    "connectors": [
      {"collection": "email_mailboxes", "module": "connectors/mailcow.py",
       "entry": "reconcile"}
    ]

Records are DESIRED STATE; the daemon's process_connectors pass converges the
outside world to match them and writes the outcome back onto each row. This is
the 01 outbox drain pointed outward, and it is the same fold-over-a-collection
every other daemon pass is. Open core never statically imports connector code:
the module is loaded dynamically at runtime, only on a deployment where its
package is installed.

This module owns the parts that are NOT connector-specific: the sync lifecycle
vocabulary, the pure retry/backoff planning (so every connector inherits the
same at-least-once, dead-lettering behavior), the dynamic loader, and the
feature flag. A connector itself is just a function:

    reconcile(record, *, base_dir) -> dict
        # {"ok": True}                          external world now matches
        # {"ok": False, "error": "..."}         transient  -> retry w/ backoff
        # {"ok": False, "error": "...",
        #  "permanent": True}                    permanent  -> dead, no retry

The connector never touches the record's sync_* fields or performs its own
write -- the driver owns the lifecycle, so backoff/attempts/dead-lettering live
in ONE place, not re-implemented per connector. Connectors MUST be idempotent
(create-or-adopt; treat an already-gone object as a done delete): delivery is
at-least-once, so a crash between a successful external call and the status
write re-reconciles next tick.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import object_collections
import object_records

# --- sync lifecycle fields + statuses --------------------------------------

SYNC_STATUS_FIELD = "sync_status"
SYNC_ATTEMPTS_FIELD = "sync_attempts"
SYNC_ERROR_FIELD = "sync_error"
SYNC_NEXT_AT_FIELD = "sync_next_at"

# Desired = the external object should EXIST; not yet reconciled. A fresh row or
# a retrying one -- sync_attempts/sync_error tell which. "" is treated as this.
STATUS_PENDING = "pending"
# Desired = the external object should be GONE; not yet removed externally.
STATUS_PENDING_DELETE = "pending_delete"
# Terminal: external world matches (exists).
STATUS_SYNCED = "synced"
# Terminal: external object removed (tombstone -- we keep the row for audit,
# per the spec's lean; a hard purge is a separate, later choice).
STATUS_DELETED = "deleted"
# Terminal: gave up (permanent error, or attempts exhausted). Needs a human.
STATUS_DEAD = "dead"

_ACTIVE_STATUSES = frozenset({"", STATUS_PENDING, STATUS_PENDING_DELETE})

FEATURE_FLAGS_COLLECTION = "feature_flags"
CONNECTORS_ENABLED_FLAG = "connectors_enabled"
DEFAULT_ACTOR = "daemon:connector"


# --- config ----------------------------------------------------------------

class ConnectorConfig:
    """Generic, connector-agnostic tunables (retry/backoff/batch). A connector
    reads its OWN credentials from its own env -- the driver knows nothing
    system-specific."""

    __slots__ = ("max_attempts", "retry_base", "retry_max", "batch_size")

    def __init__(self, *, max_attempts=8, retry_base=60, retry_max=3600, batch_size=20):
        self.max_attempts = max_attempts
        self.retry_base = retry_base
        self.retry_max = retry_max
        self.batch_size = batch_size


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    try:
        return int(str(env.get(key, "")).strip())
    except (TypeError, ValueError):
        return default


def connector_config_from_env(env: Mapping[str, str] | None = None) -> ConnectorConfig:
    import os
    env = os.environ if env is None else env
    return ConnectorConfig(
        max_attempts=_env_int(env, "DBBASIC_CONNECTOR_MAX_ATTEMPTS", 8),
        retry_base=_env_int(env, "DBBASIC_CONNECTOR_RETRY_BASE_SECONDS", 60),
        retry_max=_env_int(env, "DBBASIC_CONNECTOR_RETRY_MAX_SECONDS", 3600),
        batch_size=_env_int(env, "DBBASIC_CONNECTOR_BATCH_SIZE", 20),
    )


def connectors_pass_enabled(*, base_dir: Any) -> bool:
    """Brownout kill switch -- a feature_flags row `connectors_enabled`, default
    ON. Mirrors object_email.email_pass_enabled / object_rollups exactly."""
    try:
        rows = object_records.read_collection_records(FEATURE_FLAGS_COLLECTION, base_dir=base_dir)
    except (object_collections.CollectionNotFoundError,
            object_collections.InvalidCollectionNameError, OSError, ValueError):
        return True
    for row in rows:
        if row.get("flag") == CONNECTORS_ENABLED_FLAG:
            value = (row.get("value") or "").strip().lower()
            return True if not value else value not in {"off", "false", "0", "no"}
    return True


# --- time helpers ----------------------------------------------------------

def _now_iso(now: datetime | None = None) -> str:
    now = datetime.now(timezone.utc) if now is None else now
    return now.isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


# --- selection + planning (pure) -------------------------------------------

def is_due(record: Mapping[str, Any], now: datetime | None = None) -> bool:
    """A row the pass should reconcile this tick: still in an active
    (pending / pending_delete / unset) status, and its `sync_next_at` backoff
    (blank = immediately) has arrived."""
    status = record.get(SYNC_STATUS_FIELD) or ""
    if status not in _ACTIVE_STATUSES:
        return False
    when = _parse_iso(record.get(SYNC_NEXT_AT_FIELD) or "")
    if when is None:
        return True
    now = datetime.now(timezone.utc) if now is None else now
    return when <= now


def backoff_seconds(attempts: int, config: ConnectorConfig) -> int:
    """`retry_base * 2 ** (attempts - 1)` capped at `retry_max` -- the same
    exponential shape as the outbox drain. `attempts` is the count AFTER this
    failed try (>= 1)."""
    exp = max(0, attempts - 1)
    try:
        delay = config.retry_base * (2 ** exp)
    except OverflowError:
        delay = config.retry_max
    return int(min(delay, config.retry_max))


def plan_sync(
    record: Mapping[str, Any], outcome: Mapping[str, Any], config: ConnectorConfig,
    *, now: datetime | None = None,
) -> dict[str, str]:
    """The record update one reconcile outcome implies -- pure, no IO. The
    driver owns the lifecycle vocabulary; the connector only says ok / error /
    permanent. A successful delete tombstones (`deleted`); a successful
    create/edit is `synced`; a transient failure keeps the desired status with
    an advanced `sync_next_at`; a permanent one (or exhausted attempts) is
    `dead`."""
    now = datetime.now(timezone.utc) if now is None else now
    stamp = _now_iso(now)
    deleting = (record.get(SYNC_STATUS_FIELD) or "") == STATUS_PENDING_DELETE

    if outcome.get("ok"):
        return {
            SYNC_STATUS_FIELD: STATUS_DELETED if deleting else STATUS_SYNCED,
            SYNC_ERROR_FIELD: "",
            SYNC_ATTEMPTS_FIELD: "0",
            "updated_at": stamp,
        }

    try:
        attempts = int(str(record.get(SYNC_ATTEMPTS_FIELD) or "0")) + 1
    except (TypeError, ValueError):
        attempts = 1
    update = {
        SYNC_ATTEMPTS_FIELD: str(attempts),
        SYNC_ERROR_FIELD: str(outcome.get("error") or "")[:500],
        "updated_at": stamp,
    }
    if outcome.get("permanent") or attempts >= config.max_attempts:
        update[SYNC_STATUS_FIELD] = STATUS_DEAD
    else:
        # keep the desired action (create/edit -> pending, delete ->
        # pending_delete) so a retry still reconciles the right direction
        update[SYNC_STATUS_FIELD] = STATUS_PENDING_DELETE if deleting else STATUS_PENDING
        update[SYNC_NEXT_AT_FIELD] = _now_iso(now + timedelta(seconds=backoff_seconds(attempts, config)))
    return update


# --- dynamic connector loading ---------------------------------------------

class ConnectorLoadError(Exception):
    """Raised when a connector module cannot be loaded or lacks its entry."""


_MODULE_CACHE: dict[tuple[str, str], Callable[..., Any]] = {}


def load_connector(module_path: Path | str, entry: str = "reconcile") -> Callable[..., Any]:
    """Load a connector's entry function from a file path, at runtime. Cached
    for the process lifetime (a redeploy restarts the daemon, so staleness is a
    non-issue). Open core calls this on a path a package declared; it never
    imports connector code statically."""
    key = (str(Path(module_path).resolve()), entry)
    cached = _MODULE_CACHE.get(key)
    if cached is not None:
        return cached
    path = Path(module_path)
    if not path.is_file():
        raise ConnectorLoadError(f"connector module not found: {path}")
    # A distinct module name per path so two connectors never collide in
    # sys.modules; not registered globally (kept local to this loader).
    mod_name = f"_dbbasic_connector_{abs(hash(key)) & 0xFFFFFFFF:x}"
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    if spec is None or spec.loader is None:
        raise ConnectorLoadError(f"cannot load connector module: {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 -- surface any import-time failure
        raise ConnectorLoadError(f"connector module {path} failed to import: {exc}") from exc
    fn = getattr(module, entry, None)
    if not callable(fn):
        raise ConnectorLoadError(f"connector module {path} has no callable '{entry}'")
    _MODULE_CACHE[key] = fn
    return fn
