"""Analytics -- first-party traffic capture (port of q9's analytics app).

Most platforms forgot about analytics; this one treats it as a native,
inspectable collection. Every non-asset HTTP request appends one row to the
append-mode `page_views` collection -- path, method, status, ip, user-agent,
referrer, session, is_owner. That gives live traffic visibility (including bot
attacks -- unlike q9 we deliberately DO capture 4xx, because a flood of 404s
from one IP is exactly what you want to see) and a rollup source for dashboards.

Two deliberate design notes:

* **append storage.** page_views is write-hot and log-shaped -- the textbook
  append-mode case (docs/storage-modes.md). Every request is one O(1) append.
  This is also the platform's heaviest write path, so it doubles as the stress
  test for massive-file handling and for the retention/rotation pass
  (object_daemon.process_analytics_retention) -- id-fold compaction can't shrink
  a pure event log (nothing is superseded), so retention is a time-windowed
  rewrite, and page_views is what exercises it.

* **off by default.** Capturing a row per request is an operator choice, gated
  by `DBBASIC_ANALYTICS` (env), so a deploy never silently starts writing.

This module is the pure/testable half: config, the skip-path rule, and building
the record. The daemon owns retention; object_server owns the capture hook.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

PAGE_VIEWS_COLLECTION = "page_views"

ANALYTICS_ENABLED_ENV = "DBBASIC_ANALYTICS"
OWNER_IPS_ENV = "DBBASIC_ANALYTICS_OWNER_IPS"
RETENTION_DAYS_ENV = "DBBASIC_ANALYTICS_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 30

# Prefixes that are asset/infra/polling noise, not real page hits. NOTE we do
# NOT skip `/api/` (bots hammer APIs -- that traffic is the point) nor 4xx/5xx
# (a 404 flood is the signal). Only genuinely uninteresting paths are dropped.
SKIP_PREFIXES = (
    "/static/", "/assets/", "/favicon", "/apple-touch-icon",
    "/.well-known/", "/robots.txt", "/sitemap",
    "/metrics", "/healthz", "/health", "/ping",
    "/realtime", "/ws", "/__",
)

_TRUE = {"1", "true", "yes", "on"}
_MAX_UA = 500
_MAX_REFERRER = 255
_MAX_PATH = 500


def analytics_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Off unless DBBASIC_ANALYTICS is truthy -- a deliberate operator opt-in,
    since it adds a write to every request."""
    env = os.environ if env is None else env
    return (env.get(ANALYTICS_ENABLED_ENV) or "").strip().lower() in _TRUE


def owner_ips(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """IPs whose traffic is flagged `is_owner` so reports can exclude it
    (DBBASIC_ANALYTICS_OWNER_IPS, comma-separated)."""
    env = os.environ if env is None else env
    raw = env.get(OWNER_IPS_ENV) or ""
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def retention_days(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    try:
        value = int(str(env.get(RETENTION_DAYS_ENV, "")).strip())
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS
    return value if value > 0 else DEFAULT_RETENTION_DAYS


def should_capture(path: str) -> bool:
    """True for a real page hit -- not an asset/infra/polling path."""
    p = path or "/"
    return not p.startswith(SKIP_PREFIXES)


def _cookie_value(cookie_header: str, name: str) -> str:
    for part in (cookie_header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return ""


def build_page_view(
    *, path: str, method: str, status: int, ip: str,
    headers: Mapping[str, str], owners: frozenset[str],
    user_id: str = "",
) -> dict[str, str]:
    """The page_views record one request implies. Pure -- created_at is stamped
    by the record layer on write. is_owner is IP-based (cheap, no auth lookup in
    the hot path)."""
    ua = (headers.get("user-agent") or "")[:_MAX_UA]
    referrer = (headers.get("referer") or headers.get("referrer") or "")[:_MAX_REFERRER]
    session_id = _cookie_value(headers.get("cookie") or "", "session_id")
    return {
        "path": (path or "/")[:_MAX_PATH],
        "method": (method or "GET").upper(),
        "status": str(int(status)),
        "ip": ip or "",
        "user_agent": ua,
        "referrer": referrer,
        "session_id": session_id,
        "user_id": user_id or "",
        "is_owner": "true" if ip in owners else "false",
    }
