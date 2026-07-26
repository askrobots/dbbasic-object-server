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

* **the visitor cookie is off by default too, and separately.**
  `DBBASIC_ANALYTICS_VISITOR_COOKIE` gates the one thing here that touches a
  visitor's device. Unset -- the default -- this module never asks a browser
  to remember anything, which is the posture that needs no consent banner
  anywhere in the world. Two switches rather than one because a log and a
  device identifier are different acts: an operator who wants traffic numbers
  should not have to take on a consent obligation to get them.

This module is the pure/testable half: config, the skip-path rule, the visitor
cookie decision, and building the record. The daemon owns retention;
object_server owns the capture hook and the one line that puts the cookie on a
response.
"""

from __future__ import annotations

import os
import secrets
from typing import Any, Mapping

import object_visitors

PAGE_VIEWS_COLLECTION = "page_views"

ANALYTICS_ENABLED_ENV = "DBBASIC_ANALYTICS"
OWNER_IPS_ENV = "DBBASIC_ANALYTICS_OWNER_IPS"
RETENTION_DAYS_ENV = "DBBASIC_ANALYTICS_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 30
# A second bound, because time alone does not bound a log. The retention
# window says how far BACK to keep; it says nothing about how much can
# arrive inside it, so a bot storm or a busy deploy day can fill a small
# machine's disk and memory without a single row aging out. This is the
# cap that was missing when a demo box hit 675MB resident and started
# swapping: 30 days of retention, faithfully applied, on a log nobody had
# told how big it was allowed to get.
MAX_ROWS_ENV = "DBBASIC_ANALYTICS_MAX_ROWS"
DEFAULT_MAX_ROWS = 200_000

# Prefixes that are asset/infra/polling noise, not real page hits. NOTE we do
# NOT skip `/api/` (bots hammer APIs -- that traffic is the point) nor 4xx/5xx
# (a 404 flood is the signal). Only genuinely uninteresting paths are dropped.
SKIP_PREFIXES = (
    "/static/", "/assets/", "/favicon", "/apple-touch-icon",
    "/.well-known/", "/robots.txt", "/sitemap",
    "/metrics", "/healthz", "/health", "/ping",
    "/realtime", "/ws", "/__",
)

# --- the visitor cookie ------------------------------------------------------
#
# The one thing this server asks a browser to remember. Named like the
# identity cookie's sibling (`dbbasic_session`) because that is what it is:
# same first-party, same-site, HttpOnly posture, and deliberately NOT the
# same string. See docs/analytics.md "The visitor cookie" for the five
# rules; every one of them is enforced below rather than merely written
# down, because rule 4 in particular is one line of code away from being
# broken at all times.
VISITOR_COOKIE_NAME = "dbbasic_visitor"

# --- and it is OFF unless an operator turns it on ----------------------------
#
# The whole consent problem on this server is one cookie. Everything else
# it stores on a device is strictly necessary for something the visitor
# asked for -- the session they signed into, the basket they filled -- and
# server-side logging is not device storage at all. So the default that
# needs the least law is the one where this cookie does not exist:
# `page_views` still counts visitors by address, nothing is written to
# anybody's browser, and the ePrivacy consent trigger (storing a
# non-essential identifier on a device) never fires anywhere in the world.
#
# Turning it ON is therefore a deliberate operator act, and it is the
# moment the obligation appears -- so the obligation travels WITH the
# setting rather than living in a document: OBLIGATION below is printed on
# the daemon's and the server's boot lines, in words, by whoever is
# starting the process. An operator who flips this should not have to
# already know what they have taken on.
VISITOR_COOKIE_ENV = "DBBASIC_ANALYTICS_VISITOR_COOKIE"

OBLIGATION = (
    "stores an identifier on a visitor's device; "
    "in the EU/UK that requires consent"
)
NO_OBLIGATION = (
    "no identifier is stored on any device; visitors are counted by address"
)

# What `build_page_view` read before this cookie existed: a cookie literally
# named `session_id`, which nothing in this repo has ever set. Kept as a
# READ-ONLY fallback so a box that acquired one from somewhere still folds,
# and never written -- a second name being set would be two populations of
# visitors that cannot be compared.
LEGACY_VISITOR_COOKIE_NAME = "session_id"

VISITOR_DAYS_ENV = "DBBASIC_ANALYTICS_VISITOR_DAYS"
# Six months: long enough to see a return visit, short enough to lapse
# rather than follow somebody for years. "Indefinite" is how a session
# identifier quietly becomes a permanent one.
DEFAULT_VISITOR_DAYS = 180

# Refusals this server honours. Both mean the same thing in different
# vocabularies, and both mean NO COOKIE -- not a shorter one, not an
# anonymised one. Those visitors are still counted by the IP rule; they
# simply appear new on every visit, which is the correct consequence of
# having asked not to be remembered.
DO_NOT_TRACK_HEADERS = ("dnt", "sec-gpc")

_TRUE = {"1", "true", "yes", "on"}
_MAX_UA = 500
_MAX_REFERRER = 255
_MAX_PATH = 500
_MAX_HOST = 253      # the longest a DNS name can legally be


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


def max_rows(env: Mapping[str, str] | None = None) -> int:
    """Hard cap on stored page_views rows; 0 disables the cap."""
    env = os.environ if env is None else env
    try:
        value = int(str(env.get(MAX_ROWS_ENV, "")).strip())
    except (TypeError, ValueError):
        return DEFAULT_MAX_ROWS
    return value if value >= 0 else DEFAULT_MAX_ROWS


def should_capture(path: str) -> bool:
    """True for a real page hit -- not an asset/infra/polling path."""
    p = path or "/"
    return not p.startswith(SKIP_PREFIXES)


def visitor_days(env: Mapping[str, str] | None = None) -> int:
    """How long the visitor cookie lives, in days (DBBASIC_ANALYTICS_VISITOR_DAYS).

    A stated expiry is rule 3, so this cannot return "forever": a junk or
    non-positive value falls back to the default rather than being treated
    as unbounded.
    """
    env = os.environ if env is None else env
    try:
        value = int(str(env.get(VISITOR_DAYS_ENV, "")).strip())
    except (TypeError, ValueError):
        return DEFAULT_VISITOR_DAYS
    return value if value > 0 else DEFAULT_VISITOR_DAYS


def visitor_cookie_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether this server may set the visitor cookie at all.

    Off unless DBBASIC_ANALYTICS_VISITOR_COOKIE is truthy. Unset, the
    server behaves exactly as it did before the cookie existed: IP-based
    visitor counting, nothing stored on anybody's device, and no consent
    trigger. New-versus-returning is the one thing that becomes
    unavailable, and that is the trade -- the same trade Plausible and
    Fathom sell as a feature.

    Deliberately a separate switch from DBBASIC_ANALYTICS: recording what
    the server did (a log) and asking a browser to remember who it is
    (device storage) are different acts under different instruments, and
    one flag for both would mean an operator who wanted traffic numbers
    could not have them without also taking on a consent obligation.
    """
    env = os.environ if env is None else env
    return (env.get(VISITOR_COOKIE_ENV) or "").strip().lower() in _TRUE


def visitor_cookie_posture(env: Mapping[str, str] | None = None) -> str:
    """One line for a boot banner, stating the obligation rather than the
    setting. `DBBASIC_ANALYTICS_VISITOR_COOKIE=on` is not information; what
    it commits the operator to is."""
    if not visitor_cookie_enabled(env):
        return f"visitor cookie off (default) -- {NO_OBLIGATION}"
    return (f"visitor cookie ON ({visitor_days(env)}d, {VISITOR_COOKIE_NAME}) "
            f"-- {OBLIGATION}")


def _cookie_value(cookie_header: str, name: str) -> str:
    for part in (cookie_header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return ""


def refuses_tracking(headers: Mapping[str, str]) -> bool:
    """True when this request asked not to be remembered.

    `DNT: 1` and `Sec-GPC: 1` are the two ways a browser says it, and the
    answer here is the same for both: no cookie is set, ever. Only the
    exact value "1" counts -- `DNT: 0` is a positive statement of consent
    and an absent header is no statement at all, and reading either as a
    refusal would mean this server never remembered anybody.
    """
    for name in DO_NOT_TRACK_HEADERS:
        if str(headers.get(name) or "").strip() == "1":
            return True
    return False


def visitor_token(headers: Mapping[str, str]) -> str:
    """The visitor token this request already carries, or "".

    Reads the current name first and the legacy `session_id` cookie second,
    so a box that has one of the old ones keeps its thread instead of being
    counted as a stranger on the day this shipped.
    """
    cookie_header = headers.get("cookie") or ""
    return (_cookie_value(cookie_header, VISITOR_COOKIE_NAME)
            or _cookie_value(cookie_header, LEGACY_VISITOR_COOKIE_NAME))


def new_visitor_token() -> str:
    """An opaque token. It says *this browser has been here before* and
    nothing else: no name, no email, no account, no meaning outside this
    site's own logs, and nothing derived from the request that could be
    reversed into one (rule 2). A fingerprint hashed from IP + user agent
    would be a stable cross-visit identifier nobody could clear, which is
    the thing this deliberately is not."""
    return secrets.token_urlsafe(16)


def should_set_visitor_cookie(
    path: str, headers: Mapping[str, str], *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Whether THIS response should mint a visitor cookie.

    Every clause is one of the rules, in the order they can refuse:

    * the cookie is not switched on. This is the FIRST refusal and the
      default one: with DBBASIC_ANALYTICS_VISITOR_COOKIE unset nothing is
      ever stored on a visitor's device, so there is no consent question
      to answer. Every other clause below is about a visitor who could
      have been given a cookie; this one is about a server that does not
      hand them out.
    * analytics is off -- nothing is being recorded, so a cookie would be
      an identifier collected for no purpose at all, which is the worst
      possible trade.
    * the request refused (DNT / Sec-GPC).
    * it already has one; a second would reset the thread on every page.
    * it is not a page. `should_capture` drops assets and health checks,
      `object_visitors.is_page_path` drops the API and collection surfaces
      a script talks to. An API client is not a browser and cannot be a
      returning visitor; handing it a cookie only writes an identifier
      into somebody's automation.

    No status gate on purpose: a 404 is a real page to the person who
    typed the URL, and it is quite often the FIRST thing a visitor sees.
    Refusing to thread that visit would lose exactly the journey worth
    knowing about.
    """
    if not visitor_cookie_enabled(env):
        return False
    if not analytics_enabled(env):
        return False
    if refuses_tracking(headers):
        return False
    if visitor_token(headers):
        return False
    return should_capture(path) and object_visitors.is_page_path(path)


def visitor_cookie_header(
    token: str, *, days: int | None = None, secure: bool = True,
) -> str:
    """The `set-cookie` value for a minted visitor token.

    First-party, same-site, HttpOnly, path-wide, with a stated Max-Age --
    rules 1 and 3 written as attributes. HttpOnly matters more here than
    it looks: nothing on the page needs to read this, so anything that
    CAN read it is either an XSS payload or a third-party script, and both
    would turn a first-party counter into somebody else's identifier.
    """
    days = visitor_days() if days is None else days
    attributes = [
        f"{VISITOR_COOKIE_NAME}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={int(days) * 86400}",
    ]
    if secure:
        attributes.append("Secure")
    return "; ".join(attributes)


def build_page_view(
    *, path: str, method: str, status: int, ip: str,
    headers: Mapping[str, str], owners: frozenset[str],
    user_id: str = "", is_operator: bool = False,
    minted_visitor_token: str = "",
) -> dict[str, str]:
    """The page_views record one request implies. Pure -- created_at is stamped
    by the record layer on write.

    ## The visitor token and `user_id` are NEVER both written

    Read this before adding the join, because the join is one line and it
    will look reasonable. `session_id` here is the anonymous visitor
    cookie: an opaque token that means "this browser has been here
    before". `user_id` is an account. A row carrying BOTH is the join --
    it de-anonymises every earlier row with that token, retroactively,
    for a person who was never asked. That is the exact move that turns
    analytics into surveillance (docs/analytics.md, cookie rule 4), and
    the reason it is enforced here rather than left to judgement is that
    the enforcement has to survive somebody who has not read the doc.

    So: when a row carries a visitor token, `user_id` is dropped. A
    signed-in member's traffic is still counted -- it is still a row, with
    a path, a status and a token -- it simply is not labelled with who
    they are. Where there is no token (a visitor who sent DNT, an API
    call, a browser that refuses cookies) `user_id` is written as before,
    because there is nothing for it to be correlated WITH.

    `minted_visitor_token` is the cookie this very response is setting:
    the browser has not sent it back yet, so without it the first page of
    every visit would be threadless and every funnel would start one step
    late.

    `is_owner` marks traffic that is the SITE'S OWN rather than a visitor's,
    so reports can exclude it (every analytics rollup filters on it). Two
    things set it, and the second was missing for a long time:

    * the request came from an owner IP (cheap, no auth lookup on the hot
      path), and
    * `is_operator` -- the caller authenticated as the operator, which the
      server decides from the admin token.

    Without the second, every deploy call, every package install and every
    scripted `POST /objects/...` counted as a visitor page view. On one real
    box that produced a "Top paths" report where 45% of the rows had been hit
    exactly once and the contents were vulnerability scanners and the
    operator's own automation -- a report of the observer, not the observed.

    Note what this deliberately does NOT flag: a signed-in member browsing
    the site. That is real traffic and worth measuring; which pages members
    actually use is among the most useful things this collection knows. The
    line is between somebody USING the site and somebody OPERATING it.
    """
    ua = (headers.get("user-agent") or "")[:_MAX_UA]
    referrer = (headers.get("referer") or headers.get("referrer") or "")[:_MAX_REFERRER]
    session_id = minted_visitor_token or visitor_token(headers)
    return {
        # Which site was asked. One process here serves both a marketing
        # domain and the app, and without this their visitors are one
        # number that answers neither question. Port stripped so
        # example.com and example.com:443 are one host rather than two.
        "host": (headers.get("host") or "").split(":")[0].lower()[:_MAX_HOST],
        "path": (path or "/")[:_MAX_PATH],
        "method": (method or "GET").upper(),
        "status": str(int(status)),
        "ip": ip or "",
        "user_agent": ua,
        "referrer": referrer,
        "session_id": session_id,
        # Rule 4, enforced rather than documented: never both. See the
        # docstring above -- the two columns exist, and a row holding both
        # is the join that de-anonymises everything the token ever did.
        "user_id": "" if session_id else (user_id or ""),
        "is_owner": "true" if (is_operator or ip in owners) else "false",
    }
