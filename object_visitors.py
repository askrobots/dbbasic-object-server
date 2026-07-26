"""Who actually turned up: visitors, bots, and us, counted apart.

The pure half of visitor analytics -- one pass over page_views rows,
no data directory, no I/O.

The question this exists to answer is "I talked about the thing today,
did anyone come?", and a raw unique-IP count answers it badly. On this
server 45% of distinct paths were hit exactly once, by vulnerability
scanners probing for /wp-login.php and /index1.php from a fresh address
each time. Counting those as visitors turns a quiet day into a busy one
and makes the number useless precisely when somebody is relying on it.

So traffic is classified into three kinds and never silently merged:

**operator** -- our own. The `is_owner` flag already marks owner IPs and
admin-token requests, so deploys and scripts stop inflating the count.
Shown, not hidden: seeing your own hits is how you sanity-check that the
page is recording anything at all.

**bot** -- a client that never successfully loaded a real page. The test
is behavioural rather than a user-agent blocklist, because the blocklist
is always out of date and the behaviour is not: a scanner walks a list of
URLs that do not exist here and collects 404s. An address that got a 200
on an ordinary page did something a person does. A declared bot
user-agent still counts as a bot even if it fetched something real --
Googlebot is honest about what it is, and honesty should not promote it
to a visitor.

**visitor** -- everything else. A human, probably.

"Unique" means distinct IP, and that is an approximation worth stating
out loud rather than dressing up: an office behind one NAT is one
visitor, a phone moving between wifi and cellular is two. It is the
honest unit available to a server that sets no tracking cookie and
fingerprints nobody, and the alternative -- a cross-site identifier -- is
a thing this system deliberately does not have.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

# Clients that say what they are. Matched case-insensitively against a
# substring of the user agent -- deliberately short, because this list is
# a courtesy to honest crawlers, not the mechanism that catches scanners.
# The behavioural test below is what does that work.
_DECLARED_BOTS = (
    "bot", "crawler", "spider", "slurp", "curl/", "wget", "python-requests",
    "httpx", "go-http-client", "scrapy", "headlesschrome", "monitoring",
    "uptime", "pingdom", "semrush", "ahrefs", "facebookexternalhit",
)

# Paths that a person does not "visit" -- fetched by a page, not chosen by
# a human. They still count as traffic; they just cannot be the ONLY thing
# an address did and still make it a visitor.
_NON_PAGE_PREFIXES = ("/api/", "/collections/", "/objects/", "/style", "/nav")

OPERATOR, BOT, VISITOR = "operator", "bot", "visitor"


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _truthy(value: Any) -> bool:
    return _text(value).lower() in ("true", "1", "yes", "on")


def _status(value: Any) -> int:
    try:
        return int(_text(value) or 0)
    except (TypeError, ValueError):
        return 0


def is_page_path(path: str) -> bool:
    """A path a person could have chosen to visit."""
    clean = _text(path) or "/"
    return not clean.startswith(_NON_PAGE_PREFIXES)


def declared_bot(user_agent: str) -> bool:
    lowered = _text(user_agent).lower()
    if not lowered:
        # No user agent at all is a script, not a browser. Every real
        # browser sends one.
        return True
    return any(token in lowered for token in _DECLARED_BOTS)


def classify(rows: Iterable[dict]) -> dict[str, str]:
    """Decide what each IP was, from everything it did.

    Classification is per ADDRESS rather than per request, because the
    question is "how many of these were people" and a person generates
    many rows. One 200 on a real page is enough to call an address human;
    a hundred 404s on paths that do not exist is not.
    """
    seen_real_page: set[str] = set()
    declared: set[str] = set()
    operator: set[str] = set()
    everyone: set[str] = set()

    for row in rows:
        ip = _text(row.get("ip"))
        if not ip:
            continue
        everyone.add(ip)
        if _truthy(row.get("is_owner")):
            operator.add(ip)
        if declared_bot(row.get("user_agent")):
            declared.add(ip)
        if (200 <= _status(row.get("status")) < 400
                and is_page_path(row.get("path"))):
            seen_real_page.add(ip)

    kinds: dict[str, str] = {}
    for ip in everyone:
        if ip in operator:
            kinds[ip] = OPERATOR
        elif ip in declared or ip not in seen_real_page:
            kinds[ip] = BOT
        else:
            kinds[ip] = VISITOR
    return kinds


def _bucket(stamp: str, *, hourly: bool) -> str:
    text = _text(stamp)
    if len(text) < 10:
        return ""
    return text[:13].replace("T", " ") if hourly else text[:10]


def summarize(
    rows: Iterable[dict],
    *,
    now: datetime | None = None,
    days: int = 7,
) -> dict[str, Any]:
    """Visitors, views and bots -- hourly for today, daily for the window.

    Returns {"today", "days", "hours", "totals", "referrers", "kinds"}.
    Counted in ONE pass with sets per bucket: the alternative, a distinct
    count per bucket computed by re-scanning, is the shape that made a
    rollup rewrite 1818 rows every five minutes on this very server.
    """
    rows = list(rows)
    kinds = classify(rows)
    now = now or datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    earliest = (now - timedelta(days=days - 1)).strftime("%Y-%m-%d")

    views: dict[tuple[str, str], int] = defaultdict(int)
    uniques: dict[tuple[str, str], set[str]] = defaultdict(set)
    hour_views: dict[tuple[str, str], int] = defaultdict(int)
    hour_uniques: dict[tuple[str, str], set[str]] = defaultdict(set)
    referrers: dict[str, set[str]] = defaultdict(set)
    landing: dict[str, set[str]] = defaultdict(set)

    for row in rows:
        ip = _text(row.get("ip"))
        stamp = _text(row.get("created_at"))
        day = _bucket(stamp, hourly=False)
        if not ip or not day or day < earliest:
            continue
        kind = kinds.get(ip, VISITOR)

        views[(day, kind)] += 1
        uniques[(day, kind)].add(ip)

        if day == today:
            hour = _bucket(stamp, hourly=True)
            hour_views[(hour, kind)] += 1
            hour_uniques[(hour, kind)].add(ip)

        if kind != VISITOR:
            continue
        referrer = _text(row.get("referrer"))
        # An empty referrer is "typed it, or came from a private link" --
        # worth showing as its own row rather than dropped, because for a
        # server nobody has linked to yet it is the whole story.
        referrers[referrer or "(direct or private link)"].add(ip)
        if is_page_path(row.get("path")) and 200 <= _status(row.get("status")) < 400:
            landing[_text(row.get("path")) or "/"].add(ip)

    def _series(bucket_views, bucket_uniques, keys):
        return [{
            "bucket": key,
            "visitors": len(bucket_uniques.get((key, VISITOR), ())),
            "views": bucket_views.get((key, VISITOR), 0),
            "bots": len(bucket_uniques.get((key, BOT), ())),
            "bot_views": bucket_views.get((key, BOT), 0),
            "operator_views": bucket_views.get((key, OPERATOR), 0),
        } for key in keys]

    day_keys = [(now - timedelta(days=offset)).strftime("%Y-%m-%d")
                for offset in range(days - 1, -1, -1)]
    hour_keys = [f"{today} {hour:02d}" for hour in range(24)]

    totals: dict[str, Any] = {}
    for kind in (VISITOR, BOT, OPERATOR):
        ips = {ip for ip, k in kinds.items() if k == kind}
        totals[kind] = {
            "unique": len(ips),
            "views": sum(count for (day, k), count in views.items()
                         if k == kind and day >= earliest),
        }

    return {
        "today": today,
        "days": _series(views, uniques, day_keys),
        "hours": _series(hour_views, hour_uniques, hour_keys),
        "totals": totals,
        "referrers": sorted(
            ({"referrer": ref, "visitors": len(ips)} for ref, ips in referrers.items()),
            key=lambda row: (-row["visitors"], row["referrer"]))[:12],
        "landing": sorted(
            ({"path": path, "visitors": len(ips)} for path, ips in landing.items()),
            key=lambda row: (-row["visitors"], row["path"]))[:12],
        "window_days": days,
    }
