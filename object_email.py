"""01 email adapter -- one outbound-mail queue every block enqueues into.

A block that needs to send mail (submit confirmations, notify's email
channel, invoice delivery, membership invites, rollup digests) calls
``enqueue()`` -- a plain in-process write of one ``queued`` row into the
``email_outbox`` collection -- and returns. It never touches ``smtplib``,
never blocks on the network, and never loses the message on an exception.
The daemon's ``process_email_outbox`` pass drains the queue: it renders
each ``queued`` row that's due, hands it to the configured transport, and
records the outcome back onto the row (``sent`` / retry / ``dead``). See
plan/vocabulary/01-email-adapter-spec.md.

Design decisions pinned here (the spec's Open Questions, resolved):

* **States (spec's "State naming" question).** Exactly three:
  ``queued`` (pending -- including waiting on a backoff between retries),
  ``sent`` (terminal success), ``dead`` (terminal failure: retries
  exhausted, or a permanent send-time error like a refused recipient).
  There is no ``sending`` state (delivery is at-least-once, so a crash
  mid-send simply retries -- an intermediate marker buys nothing) and no
  standalone ``failed`` (a retryable error stays ``queued`` with an
  advanced ``next_attempt_at``; a non-retryable one goes straight to
  ``dead``).
* **Transport modes.** ``DBBASIC_SMTP_MODE`` = ``disabled`` (default --
  everything queues, nothing sends, fully inspectable), ``log`` (write to
  a log file instead of the network -- dev/CI), or ``live`` (real SMTP).
* **max_attempts** is captured onto the row at enqueue time; **from_addr**
  is resolved lazily at send time from ``DBBASIC_SMTP_FROM`` when the row
  left it blank (so fixing a wrong default From doesn't require draining
  the backlog).
* **Packaging.** The schema + permission fragment ship as the ``app-email``
  package (no pages); the daemon pass and the ``smtplib`` call live in
  core (here + object_daemon) -- there is no manifest mechanism for a
  package to register a daemon pass yet.

The module is transport-agnostic and side-effect-injectable: ``enqueue``
and the pure delivery decision (``plan_delivery``) are unit-testable
without a network, and ``attempt_delivery`` takes a ``sender`` callable so
tests drive success/permanent/transient outcomes directly.
"""

from __future__ import annotations

import json
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Callable, Mapping

import object_collections
import object_records

OUTBOX_COLLECTION = "email_outbox"
FEATURE_FLAGS_COLLECTION = "feature_flags"
EMAIL_ENABLED_FLAG = "email_enabled"
DEFAULT_ACTOR = "daemon:email"

STATUS_QUEUED = "queued"
STATUS_SENT = "sent"
STATUS_DEAD = "dead"

# SMTP exceptions that will never succeed on retry -- a refused/invalid
# recipient or sender, or an unsupported command. These skip the retry
# budget and go straight to `dead`; everything else (connection refused,
# timeout, transient 4xx) is retried with backoff up to max_attempts.
_PERMANENT_SMTP_ERRORS = (
    smtplib.SMTPRecipientsRefused,
    smtplib.SMTPSenderRefused,
    smtplib.SMTPNotSupportedError,
)


# --- configuration ---------------------------------------------------------

class SmtpConfig:
    """Deploy-time SMTP configuration, read from ``DBBASIC_SMTP_*`` env vars
    (the operator-owned env file, never a record or backup -- same write-only
    posture object_service_keys uses). Plain attributes so tests can build one
    without the environment."""

    __slots__ = (
        "mode", "host", "port", "username", "password", "use_tls", "from_addr",
        "timeout", "max_attempts", "retry_base", "retry_max", "rate_limit",
        "batch_size", "log_path",
    )

    def __init__(
        self, *, mode="disabled", host="", port=587, username="", password="",
        use_tls=True, from_addr="", timeout=10, max_attempts=5, retry_base=30,
        retry_max=3600, rate_limit=30, batch_size=10,
        log_path="data/logs/email_outbox/log.tsv",
    ):
        self.mode = mode
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.from_addr = from_addr
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.retry_base = retry_base
        self.retry_max = retry_max
        self.rate_limit = rate_limit
        self.batch_size = batch_size
        self.log_path = log_path

    @property
    def configured(self) -> bool:
        """Whether the pass has anywhere to send. ``disabled`` never is;
        ``live`` needs a host; ``log`` is always 'configured' (it writes a
        file, needs no network)."""
        if self.mode == "log":
            return True
        if self.mode == "live":
            return bool(self.host)
        return False


def _env_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in {"off", "false", "0", "no"}


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    try:
        return int(str(env.get(key, "")).strip())
    except (TypeError, ValueError):
        return default


def smtp_config_from_env(env: Mapping[str, str] | None = None) -> SmtpConfig:
    env = os.environ if env is None else env
    mode = (env.get("DBBASIC_SMTP_MODE") or "disabled").strip().lower()
    if mode not in {"disabled", "log", "live"}:
        mode = "disabled"
    return SmtpConfig(
        mode=mode,
        host=(env.get("DBBASIC_SMTP_HOST") or "").strip(),
        port=_env_int(env, "DBBASIC_SMTP_PORT", 587),
        username=(env.get("DBBASIC_SMTP_USERNAME") or "").strip(),
        password=env.get("DBBASIC_SMTP_PASSWORD") or "",
        use_tls=_env_bool(env, "DBBASIC_SMTP_USE_TLS", True),
        from_addr=(env.get("DBBASIC_SMTP_FROM") or "").strip(),
        timeout=_env_int(env, "DBBASIC_SMTP_TIMEOUT_SECONDS", 10),
        max_attempts=_env_int(env, "DBBASIC_SMTP_MAX_ATTEMPTS", 5),
        retry_base=_env_int(env, "DBBASIC_SMTP_RETRY_BASE_SECONDS", 30),
        retry_max=_env_int(env, "DBBASIC_SMTP_RETRY_MAX_SECONDS", 3600),
        rate_limit=_env_int(env, "DBBASIC_SMTP_RATE_LIMIT_PER_MINUTE", 30),
        batch_size=_env_int(env, "DBBASIC_SMTP_BATCH_SIZE", 10),
        log_path=(env.get("DBBASIC_SMTP_LOG_PATH") or "data/logs/email_outbox/log.tsv").strip(),
    )


def email_pass_enabled(*, base_dir: Any) -> bool:
    """Brownout kill switch -- a ``feature_flags`` row named ``email_enabled``,
    default ON. This is the runtime lever an operator flips to stop outbound
    network calls without a redeploy; it is NOT the "is SMTP configured" gate
    (that's the env, checked separately). Mirrors notify_pass_enabled."""
    try:
        rows = object_records.read_collection_records(FEATURE_FLAGS_COLLECTION, base_dir=base_dir)
    except (object_collections.CollectionNotFoundError,
            object_collections.InvalidCollectionNameError, OSError, ValueError):
        return True
    for row in rows:
        if row.get("flag") == EMAIL_ENABLED_FLAG:
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


# --- enqueue (the in-process call surface) ---------------------------------

def enqueue(
    to: str, subject: str, text_body: str, *,
    html_body: str | None = None, from_addr: str | None = None,
    reply_to: str | None = None, source_object_id: str | None = None,
    extra: Any = None, base_dir: Any, config: SmtpConfig | None = None,
    now: datetime | None = None,
) -> str:
    """Write one ``queued`` row and return its id. A pure local write: no
    rendering, no network, no blocking on delivery. ``max_attempts`` is
    captured now (a later env change won't retroactively shrink an in-flight
    message's budget); ``from_addr`` left blank is resolved lazily at send.
    ``extra`` may be a dict/list (JSON-encoded) or a string, stashed verbatim
    for the caller's own correlation id -- the adapter never reads it."""
    config = smtp_config_from_env() if config is None else config
    stamp = _now_iso(now)
    record = {
        "to": to,
        "from_addr": from_addr or "",
        "reply_to": reply_to or "",
        "subject": subject,
        "text_body": text_body,
        "html_body": html_body or "",
        "status": STATUS_QUEUED,
        "attempts": "0",
        "max_attempts": str(config.max_attempts),
        "last_error": "",
        "next_attempt_at": stamp,
        "sent_at": "",
        "source_object_id": source_object_id or "",
    }
    # `extra` is the overflow field (store: extra) -- a JSON object only. Pass a
    # dict, or a JSON-object string; omit it entirely when there's nothing (an
    # empty string is not a valid object and the store would reject it).
    if isinstance(extra, dict):
        record["extra"] = json.dumps(extra, sort_keys=True)
    elif isinstance(extra, str) and extra.strip():
        record["extra"] = extra
    created = object_records.create_collection_record(
        OUTBOX_COLLECTION, record, base_dir=base_dir, actor=DEFAULT_ACTOR,
    )
    return created["id"]


# --- delivery decision (pure) ----------------------------------------------

def is_due(record: Mapping[str, Any], now: datetime | None = None) -> bool:
    """A row the pass should attempt this tick: still ``queued`` and its
    ``next_attempt_at`` (blank = immediately) has arrived."""
    if (record.get("status") or STATUS_QUEUED) != STATUS_QUEUED:
        return False
    when = _parse_iso(record.get("next_attempt_at") or "")
    if when is None:
        return True
    now = datetime.now(timezone.utc) if now is None else now
    return when <= now


def backoff_seconds(attempts: int, config: SmtpConfig) -> int:
    """``retry_base * 2 ** (attempts - 1)`` capped at ``retry_max`` -- the same
    exponential shape process_queue uses. ``attempts`` is the count AFTER this
    failed try (>= 1)."""
    exp = max(0, attempts - 1)
    try:
        delay = config.retry_base * (2 ** exp)
    except OverflowError:
        delay = config.retry_max
    return int(min(delay, config.retry_max))


def plan_delivery(
    record: Mapping[str, Any], config: SmtpConfig, *,
    ok: bool, permanent: bool = False, error: str = "",
    now: datetime | None = None,
) -> dict[str, str]:
    """The record update a delivery outcome implies -- pure, no IO. ``ok``:
    marked ``sent``. Not ok + ``permanent`` (or retries now exhausted): marked
    ``dead``. Otherwise: stays ``queued`` with an advanced ``next_attempt_at``
    and the error captured."""
    now = datetime.now(timezone.utc) if now is None else now
    stamp = _now_iso(now)
    if ok:
        return {"status": STATUS_SENT, "sent_at": stamp, "last_error": "", "updated_at": stamp}

    attempts = _env_int(record, "attempts", 0) + 1
    try:
        max_attempts = int(str(record.get("max_attempts") or config.max_attempts))
    except (TypeError, ValueError):
        max_attempts = config.max_attempts
    update = {"attempts": str(attempts), "last_error": (error or "")[:500], "updated_at": stamp}
    if permanent or attempts >= max_attempts:
        update["status"] = STATUS_DEAD
    else:
        nxt = now + timedelta(seconds=backoff_seconds(attempts, config))
        update["status"] = STATUS_QUEUED
        update["next_attempt_at"] = _now_iso(nxt)
    return update


# --- transports ------------------------------------------------------------

def build_message(record: Mapping[str, Any], config: SmtpConfig) -> EmailMessage:
    """Render one outbox row into an EmailMessage. ``from_addr`` falls back to
    ``DBBASIC_SMTP_FROM`` lazily here (not captured at enqueue)."""
    msg = EmailMessage()
    msg["To"] = record.get("to") or ""
    msg["From"] = (record.get("from_addr") or "").strip() or config.from_addr
    msg["Subject"] = record.get("subject") or ""
    reply_to = (record.get("reply_to") or "").strip()
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(record.get("text_body") or "")
    html = record.get("html_body") or ""
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def _send_live(config: SmtpConfig, record: Mapping[str, Any]) -> None:
    """Real SMTP send. Raises on any failure (the caller classifies it)."""
    msg = build_message(record, config)
    sender = parseaddr(msg["From"])[1]
    recipients = [addr for _, addr in [parseaddr(r) for r in (record.get("to") or "").split(",")] if addr]
    if not recipients:
        raise smtplib.SMTPRecipientsRefused({})
    with smtplib.SMTP(config.host, config.port, timeout=config.timeout) as server:
        if config.use_tls:
            server.starttls()
        if config.username:
            server.login(config.username, config.password)
        server.send_message(msg, from_addr=sender or None, to_addrs=recipients)


def _send_log(config: SmtpConfig, record: Mapping[str, Any]) -> None:
    """`log` mode: append a one-line record to the log file instead of the
    network -- dev/CI verification that enqueue -> drain works end to end
    without an SMTP server. Tab-separated, newlines stripped from the fields."""
    path = Path(config.log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    from_addr = (record.get("from_addr") or "").strip() or config.from_addr

    def _clean(value: Any) -> str:
        return str(value or "").replace("\t", " ").replace("\n", " ").replace("\r", " ")

    line = "\t".join(_clean(v) for v in (
        _now_iso(), record.get("to"), from_addr, record.get("subject"),
        record.get("text_body"),
    ))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def sender_for(config: SmtpConfig) -> Callable[[SmtpConfig, Mapping[str, Any]], None]:
    """The transport callable for the configured mode."""
    if config.mode == "live":
        return _send_live
    if config.mode == "log":
        return _send_log
    raise ValueError(f"no transport for mode {config.mode!r}")


def attempt_delivery(
    record: Mapping[str, Any], config: SmtpConfig, *,
    sender: Callable[[SmtpConfig, Mapping[str, Any]], None] | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """Try to deliver one row and return the record update its outcome implies.
    ``sender`` is injectable (tests drive success/permanent/transient without a
    network); it defaults to the configured transport. A permanent SMTP error
    (refused recipient/sender) is marked ``dead`` immediately; any other
    exception is a transient failure and retries with backoff."""
    sender = sender_for(config) if sender is None else sender
    try:
        sender(config, record)
    except _PERMANENT_SMTP_ERRORS as exc:
        return plan_delivery(record, config, ok=False, permanent=True, error=str(exc), now=now)
    except Exception as exc:  # noqa: BLE001 -- transient: retry with backoff
        return plan_delivery(record, config, ok=False, error=str(exc), now=now)
    return plan_delivery(record, config, ok=True, now=now)


# --- rate limiter (rolling per-minute window) ------------------------------

def rate_window_reset(window: Mapping[str, Any] | None, now_ts: float) -> dict[str, float]:
    """Fold the persisted ``{window_start, sent_count}`` marker: a window older
    than 60s rolls over to a fresh one. Pure -- the daemon owns the file IO."""
    start = 0.0
    count = 0
    if isinstance(window, Mapping):
        try:
            start = float(window.get("window_start") or 0)
            count = int(window.get("sent_count") or 0)
        except (TypeError, ValueError):
            start, count = 0.0, 0
    if now_ts - start >= 60:
        return {"window_start": now_ts, "sent_count": 0}
    return {"window_start": start, "sent_count": count}
