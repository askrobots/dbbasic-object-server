"""01 email adapter -- outbox enqueue, the pure delivery decision, the
transports, and the daemon drain pass (log mode end-to-end, retry/backoff,
dead-lettering, rate limiting). See plan/vocabulary/01-email-adapter-spec.md.
"""

import smtplib
from datetime import datetime, timezone
from pathlib import Path

import object_daemon
import object_email
import object_packages
import object_records

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"


# ---- config from env ------------------------------------------------------

def test_smtp_config_defaults_and_configured_gate():
    disabled = object_email.smtp_config_from_env({})
    assert disabled.mode == "disabled" and not disabled.configured
    # live needs a host
    assert not object_email.smtp_config_from_env({"DBBASIC_SMTP_MODE": "live"}).configured
    live = object_email.smtp_config_from_env({"DBBASIC_SMTP_MODE": "live", "DBBASIC_SMTP_HOST": "mail.x"})
    assert live.configured and live.host == "mail.x" and live.port == 587
    # log mode is always "configured" (writes a file, no network)
    assert object_email.smtp_config_from_env({"DBBASIC_SMTP_MODE": "log"}).configured
    # bad mode falls back to disabled
    assert object_email.smtp_config_from_env({"DBBASIC_SMTP_MODE": "garbage"}).mode == "disabled"


def test_smtp_config_reads_tunables():
    cfg = object_email.smtp_config_from_env({
        "DBBASIC_SMTP_MODE": "live", "DBBASIC_SMTP_HOST": "h",
        "DBBASIC_SMTP_MAX_ATTEMPTS": "3", "DBBASIC_SMTP_RETRY_BASE_SECONDS": "10",
        "DBBASIC_SMTP_RETRY_MAX_SECONDS": "100", "DBBASIC_SMTP_RATE_LIMIT_PER_MINUTE": "2",
        "DBBASIC_SMTP_BATCH_SIZE": "5", "DBBASIC_SMTP_USE_TLS": "false",
    })
    assert (cfg.max_attempts, cfg.retry_base, cfg.retry_max) == (3, 10, 100)
    assert cfg.rate_limit == 2 and cfg.batch_size == 5 and cfg.use_tls is False


# ---- pure delivery decision ----------------------------------------------

def test_backoff_is_exponential_and_capped():
    cfg = object_email.smtp_config_from_env({"DBBASIC_SMTP_RETRY_BASE_SECONDS": "30", "DBBASIC_SMTP_RETRY_MAX_SECONDS": "3600"})
    assert object_email.backoff_seconds(1, cfg) == 30
    assert object_email.backoff_seconds(2, cfg) == 60
    assert object_email.backoff_seconds(3, cfg) == 120
    assert object_email.backoff_seconds(50, cfg) == 3600  # capped


def test_is_due_respects_status_and_schedule():
    now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    assert object_email.is_due({"status": "queued", "next_attempt_at": "2026-07-21T11:59:00Z"}, now)
    assert not object_email.is_due({"status": "queued", "next_attempt_at": "2026-07-21T12:01:00Z"}, now)
    assert not object_email.is_due({"status": "sent", "next_attempt_at": "2026-01-01T00:00:00Z"}, now)
    assert object_email.is_due({"status": "queued", "next_attempt_at": ""}, now)  # blank = immediately


def test_plan_delivery_success_marks_sent():
    cfg = object_email.smtp_config_from_env({})
    now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    out = object_email.plan_delivery({"attempts": "0"}, cfg, ok=True, now=now)
    assert out["status"] == "sent" and out["sent_at"].startswith("2026-07-21T12:00")


def test_plan_delivery_transient_retries_then_dead_at_ceiling():
    cfg = object_email.smtp_config_from_env({"DBBASIC_SMTP_MAX_ATTEMPTS": "3"})
    now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    # first failure: attempts 0 -> 1, stays queued with a future next_attempt_at
    a = object_email.plan_delivery({"attempts": "0", "max_attempts": "3"}, cfg, ok=False, error="boom", now=now)
    assert a["status"] == "queued" and a["attempts"] == "1" and a["last_error"] == "boom"
    assert a["next_attempt_at"] > "2026-07-21T12:00"
    # final failure: attempts 2 -> 3 == max -> dead
    b = object_email.plan_delivery({"attempts": "2", "max_attempts": "3"}, cfg, ok=False, error="boom", now=now)
    assert b["status"] == "dead" and b["attempts"] == "3"


def test_plan_delivery_permanent_dies_immediately():
    cfg = object_email.smtp_config_from_env({"DBBASIC_SMTP_MAX_ATTEMPTS": "9"})
    out = object_email.plan_delivery({"attempts": "0", "max_attempts": "9"}, cfg, ok=False, permanent=True, error="bad addr")
    assert out["status"] == "dead"  # permanent error skips the retry budget


def test_attempt_delivery_classifies_sender_outcomes():
    cfg = object_email.smtp_config_from_env({"DBBASIC_SMTP_MODE": "live", "DBBASIC_SMTP_HOST": "h", "DBBASIC_SMTP_MAX_ATTEMPTS": "5"})
    rec = {"attempts": "0", "max_attempts": "5", "to": "a@b.c"}

    assert object_email.attempt_delivery(rec, cfg, sender=lambda c, r: None)["status"] == "sent"

    def refuse(c, r):
        raise smtplib.SMTPRecipientsRefused({"a@b.c": (550, b"no such user")})
    assert object_email.attempt_delivery(rec, cfg, sender=refuse)["status"] == "dead"

    def flap(c, r):
        raise ConnectionRefusedError("connection refused")
    transient = object_email.attempt_delivery(rec, cfg, sender=flap)
    assert transient["status"] == "queued" and transient["attempts"] == "1"


def test_rate_window_reset_rolls_over_after_a_minute():
    fresh = object_email.rate_window_reset(None, 1000.0)
    assert fresh == {"window_start": 1000.0, "sent_count": 0}
    # same window (<60s later): preserved
    same = object_email.rate_window_reset({"window_start": 1000.0, "sent_count": 4}, 1030.0)
    assert same["sent_count"] == 4 and same["window_start"] == 1000.0
    # >=60s later: rolls to a new window
    rolled = object_email.rate_window_reset({"window_start": 1000.0, "sent_count": 4}, 1061.0)
    assert rolled == {"window_start": 1061.0, "sent_count": 0}


def test_build_message_falls_back_to_env_from():
    cfg = object_email.smtp_config_from_env({"DBBASIC_SMTP_FROM": "noreply@x.com"})
    msg = object_email.build_message({"to": "u@x.com", "subject": "hi", "text_body": "body", "from_addr": ""}, cfg)
    assert msg["From"] == "noreply@x.com" and msg["To"] == "u@x.com"
    # an explicit from on the row wins over the env default
    msg2 = object_email.build_message({"to": "u@x.com", "subject": "s", "text_body": "b", "from_addr": "me@y.com"}, cfg)
    assert msg2["From"] == "me@y.com"


# ---- enqueue + daemon drain (end to end) ----------------------------------

def _install(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    object_packages.install_package("app-email", root=PACKAGES_ROOT, base_dir=data_dir,
                                    object_roots=[object_root], allow_replace=True)
    return data_dir


def _outbox(data_dir):
    return object_records.read_collection_records("email_outbox", base_dir=data_dir)


def test_enqueue_writes_one_queued_row(tmp_path):
    data_dir = _install(tmp_path)
    cfg = object_email.smtp_config_from_env({"DBBASIC_SMTP_MAX_ATTEMPTS": "7"})
    rid = object_email.enqueue("u@x.com", "Welcome", "Thanks for joining\n",
                               source_object_id="submit_signup", extra={"submission_id": "c1"},
                               base_dir=data_dir, config=cfg)
    rows = _outbox(data_dir)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == rid and row["status"] == "queued" and row["attempts"] == "0"
    assert row["max_attempts"] == "7" and row["to"] == "u@x.com" and row["subject"] == "Welcome"
    assert row["source_object_id"] == "submit_signup" and "c1" in row["extra"]
    assert row["next_attempt_at"]  # scheduled immediately


def test_daemon_pass_delivers_in_log_mode(tmp_path, monkeypatch):
    data_dir = _install(tmp_path)
    log_path = tmp_path / "mail.log"
    monkeypatch.setenv("DBBASIC_SMTP_MODE", "log")
    monkeypatch.setenv("DBBASIC_SMTP_LOG_PATH", str(log_path))
    object_email.enqueue("a@x.com", "One", "first", base_dir=data_dir)
    object_email.enqueue("b@x.com", "Two", "second", base_dir=data_dir)

    result = object_daemon.process_email_outbox(base_dir=data_dir)
    assert result == {"attempted": 2, "sent": 2, "dead": 0, "rate_limited": 0}
    assert all(r["status"] == "sent" and r["sent_at"] for r in _outbox(data_dir))
    assert len(log_path.read_text().strip().splitlines()) == 2
    # re-run: nothing is due anymore
    assert object_daemon.process_email_outbox(base_dir=data_dir) == {"attempted": 0, "sent": 0, "dead": 0}


def test_daemon_pass_queues_only_when_unconfigured(tmp_path, monkeypatch):
    data_dir = _install(tmp_path)
    monkeypatch.delenv("DBBASIC_SMTP_MODE", raising=False)  # disabled
    object_email.enqueue("a@x.com", "Held", "queues forever", base_dir=data_dir)
    assert object_daemon.process_email_outbox(base_dir=data_dir) is None
    assert _outbox(data_dir)[0]["status"] == "queued"  # untouched, fully inspectable


def test_daemon_pass_retries_transient_failure(tmp_path, monkeypatch):
    data_dir = _install(tmp_path)
    monkeypatch.setenv("DBBASIC_SMTP_MODE", "log")
    monkeypatch.setattr(object_email, "sender_for", lambda cfg: _boom)
    object_email.enqueue("a@x.com", "Flaky", "body", base_dir=data_dir)
    result = object_daemon.process_email_outbox(base_dir=data_dir)
    assert result["attempted"] == 1 and result["sent"] == 0 and result["dead"] == 0
    row = _outbox(data_dir)[0]
    assert row["status"] == "queued" and row["attempts"] == "1" and "boom" in row["last_error"]


def test_daemon_pass_rate_limits_the_batch(tmp_path, monkeypatch):
    data_dir = _install(tmp_path)
    monkeypatch.setenv("DBBASIC_SMTP_MODE", "log")
    monkeypatch.setenv("DBBASIC_SMTP_LOG_PATH", str(tmp_path / "m.log"))
    monkeypatch.setenv("DBBASIC_SMTP_RATE_LIMIT_PER_MINUTE", "1")
    for i in range(3):
        object_email.enqueue(f"u{i}@x.com", "S", "b", base_dir=data_dir)
    result = object_daemon.process_email_outbox(base_dir=data_dir)
    assert result["sent"] == 1 and result["rate_limited"] == 2
    statuses = sorted(r["status"] for r in _outbox(data_dir))
    assert statuses == ["queued", "queued", "sent"]  # two held for the next window


def _boom(config, record):
    raise ConnectionRefusedError("boom: connection refused")
