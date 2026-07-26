"""Goals, funnels and the question a page view cannot answer on its own.

`page_views` says what was requested. It cannot say whether any of it
WORKED -- whether the person who read the pitch on Monday came back on
Thursday and bought something. Two things were missing for that, and this
module is the half that folds them:

* a **thread** -- the visitor cookie (`object_analytics`), stamped into
  `page_views.session_id`, which is what makes two visits one person
  rather than two strangers; and
* a **goal** -- one `conversions` row written by whichever app owns the
  transition worth counting (`packages/app-analytics/objects/system/
  record_conversion.py`).

Everything here is pure: rows in, numbers out, no data directory, no I/O.
The same posture object_visitors takes, and for the same reason -- a fold
you can call with a list of dicts is a fold you can argue with in a test.

## Three refusals, because the obvious version of each one lies

**A funnel says how much of itself is guesswork.** Rows written before
the cookie existed, and rows from anyone who sent Do Not Track, carry no
token; the only thread left for those is the IP address, which is not a
person (an office is one, a phone on the train is three). Stitching by IP
is better than dropping the rows, and pretending the result is as solid
as a cookie-threaded funnel is not. So `funnel` returns the IP-stitched
fraction as DATA, next to the numbers it qualifies, where a caller has to
decide what to do with it rather than being able to not notice it.

**Returning visitors are a FLOOR, never a census.** A cleared cookie is a
new visitor. A second browser is a second visitor. A phone is a third.
Every one of those errors points the same way -- undercounting -- which
is the right direction for a number to be wrong in, and it is still
wrong. `returning_visitors` returns `floor: True` and the caveat text
itself, so a surface cannot render the number without being handed the
caveat to render beside it.

**Time to conversion admits what aged out.** `page_views` is bounded by
days AND rows (docs/analytics.md), so a visitor whose first visit fell
off the end of retention has a conversion with no beginning. That is
reported as its own count -- not silently dropped, which would bias the
median toward fast conversions, and not counted as zero days, which would
be a lie in the same direction but louder.

## The join that must never happen

`conversions` has a `session_id` column and a `user_id` column. A row
carrying both correlates an anonymous browsing thread with a named
account, retroactively, for somebody who was never asked --
docs/analytics.md's cookie rule 4, the move that turns analytics into
surveillance. `build_conversion` therefore writes at most ONE of them and
prefers the anonymous one. See its docstring before changing it.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

CONVERSIONS_COLLECTION = "conversions"

# Where a shop describes its own funnel, so that having one is a settings
# row rather than a deploy. A JSON list; see parse_funnel_steps.
FUNNEL_STEPS_SETTING = "analytics.funnel_steps"

# Threads stitched by address rather than by cookie are prefixed, so a
# fold can always tell the two apart afterwards without a second pass and
# a caller can never mistake one for the other.
IP_THREAD_PREFIX = "ip:"

STEP_PATH, STEP_EVENT = "path", "event"

FLOOR_CAVEAT = (
    "A floor, never a census: a cleared cookie is a new visitor, a second "
    "browser is a second visitor, and a phone is a third. This undercounts, "
    "which is the right direction for it to be wrong in."
)

AGED_OUT_CAVEAT = (
    "page_views is bounded by age and by row count, so a visitor whose "
    "first visit has already aged out has a conversion with no beginning. "
    "Those are counted separately rather than dropped -- dropping them "
    "would bias the median toward fast conversions."
)


# --- small readers -----------------------------------------------------------

def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _day(stamp: Any) -> str:
    text = _text(stamp)
    return text[:10] if len(text) >= 10 else ""


def _moment(stamp: Any) -> datetime | None:
    """An aware datetime from whatever the record layer wrote, or None.

    Never raises on junk: a malformed timestamp is one row that cannot
    take part in a duration, not a report that fails to render.
    """
    text = _text(stamp).replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_metadata(value: Any) -> dict[str, Any]:
    """A conversion's metadata blob as a dict. A blob that is not a JSON
    object reads as empty rather than as an error -- it is a free-text
    column and a report must survive whatever ended up in it."""
    if isinstance(value, dict):
        return value
    text = _text(value)
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def source_ref(collection: str, record_id: str) -> str:
    """The provenance string a conversion is deduplicated on:
    `orders/ord-1`. Same shape as every other marker in this house."""
    return f"{_text(collection)}/{_text(record_id)}"


# --- writing one ---------------------------------------------------------------

def build_conversion(
    *, event_type: str, source: str = "", session_id: str = "",
    user_id: str = "", metadata: dict[str, Any] | None = None,
) -> dict[str, str]:
    """The `conversions` record one goal implies. Pure; created_at is
    stamped by the record layer on write.

    **At most one of `session_id` and `user_id` is written, and the
    anonymous one wins.** Both columns exist and it is one line to fill
    them both, which is exactly why this refuses: a row holding an opaque
    visitor token AND an account id de-anonymises every page view that
    token ever made, retroactively, for a person who was never asked.
    docs/analytics.md, cookie rule 4. The anonymous one wins because it is
    the one a funnel needs -- the account id is recoverable from the
    record the conversion points at, if somebody with a reason to look
    goes and looks, which is a different act from having it pre-joined in
    an analytics table.

    `source` is the provenance marker (`orders/ord-1`) and lives inside
    metadata rather than in a column of its own, because the schema is
    fixed and correct and a private field for one handler's bookkeeping
    would be a worse trade than a documented key in the blob.
    """
    blob = dict(metadata or {})
    if source:
        blob["source"] = _text(source)
    return {
        "event_type": _text(event_type),
        "session_id": _text(session_id),
        # Never both. See the docstring; the enforcement is here rather
        # than at the call sites because it has to survive a call site
        # written by somebody who has not read the docstring.
        "user_id": "" if _text(session_id) else _text(user_id),
        "metadata": json.dumps(blob, sort_keys=True) if blob else "",
    }


def recorded_sources(rows: Iterable[dict]) -> set[tuple[str, str]]:
    """Every (event_type, source) already in `conversions`.

    Read once and folded into a set rather than scanned per candidate:
    conversions is append storage and grows forever, and re-reading it
    once per goal is the shape that makes a handler quietly quadratic.
    """
    seen: set[tuple[str, str]] = set()
    for row in rows:
        source = _text(parse_metadata(row.get("metadata")).get("source"))
        if source:
            seen.add((_text(row.get("event_type")), source))
    return seen


def already_recorded(rows: Iterable[dict], event_type: str, source: str) -> bool:
    return (_text(event_type), _text(source)) in recorded_sources(rows)


# --- reading them back ---------------------------------------------------------

def by_event_type(
    conversions: Iterable[dict], *, window_days: int = 0,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Conversions grouped by goal, newest activity first.

    `threaded` is how many of each kind can take part in a funnel at all.
    It is reported per event type rather than as one total because the
    answer differs sharply by goal: a conversion recorded by a public page
    that saw the cookie is threaded, and one recorded by a back-office
    transition (an order confirmed by staff two days later) never can be.
    """
    now = now or datetime.now(timezone.utc)
    earliest = ((now - timedelta(days=window_days - 1)).strftime("%Y-%m-%d")
                if window_days > 0 else "")

    counts: dict[str, dict[str, Any]] = {}
    for row in conversions:
        event_type = _text(row.get("event_type"))
        if not event_type:
            continue
        day = _day(row.get("created_at"))
        if earliest and day and day < earliest:
            continue
        entry = counts.setdefault(event_type, {
            "event_type": event_type, "count": 0, "threaded": 0,
            "first": "", "last": "",
        })
        entry["count"] += 1
        if _text(row.get("session_id")):
            entry["threaded"] += 1
        stamp = _text(row.get("created_at"))
        if stamp:
            if not entry["first"] or stamp < entry["first"]:
                entry["first"] = stamp
            if stamp > entry["last"]:
                entry["last"] = stamp
    return sorted(counts.values(),
                  key=lambda row: (-row["count"], row["event_type"]))


# --- the funnel ------------------------------------------------------------------

def parse_funnel_steps(raw: Any) -> tuple[list[dict[str, str]], str]:
    """(steps, error) from the `analytics.funnel_steps` setting.

    Returns the error as a STRING rather than raising, because the caller
    is a page: an operator who typed malformed JSON into a settings row
    needs to be told which row and why, on the screen where they typed it,
    not to be shown a 500 or -- worse -- an empty funnel that looks like
    nobody converted.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return [], ""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError) as exc:
            return [], f"not valid JSON ({exc})"
    if not isinstance(raw, (list, tuple)):
        return [], "expected a JSON list of steps"
    try:
        return normalize_steps(raw), ""
    except ValueError as exc:
        return [], str(exc)


def normalize_steps(steps: Sequence[Any]) -> list[dict[str, str]]:
    """Ordered step definitions in one shape: {label, kind, match}.

    Accepts the two spellings an operator would actually write:

        ["/shop", "/checkout", "order_confirmed"]
        [{"label": "Browsed", "path": "/shop"},
         {"label": "Bought",  "event_type": "order_confirmed"}]

    A bare string beginning with `/` is a PATH PREFIX and anything else is
    an event_type, which is unambiguous because every path starts with a
    slash and no event type may.
    """
    out: list[dict[str, str]] = []
    for raw in steps or ():
        if isinstance(raw, str):
            match = raw.strip()
            if not match:
                continue
            kind = STEP_PATH if match.startswith("/") else STEP_EVENT
            out.append({"label": match, "kind": kind, "match": match})
            continue
        if not isinstance(raw, dict):
            raise ValueError(f"a step must be a string or an object, got {type(raw).__name__}")
        # Idempotent on its own output. A caller that validated the steps
        # once (a page reporting a bad settings row before it renders
        # anything) then hands them to `funnel`, which normalizes again --
        # and a normalizer that rejects its own output turns that
        # perfectly reasonable sequence into a 500.
        if _text(raw.get("kind")) in (STEP_PATH, STEP_EVENT) and _text(raw.get("match")):
            out.append({"label": _text(raw.get("label")) or _text(raw["match"]),
                        "kind": _text(raw["kind"]), "match": _text(raw["match"])})
            continue
        path = _text(raw.get("path") or raw.get("path_prefix"))
        event_type = _text(raw.get("event_type") or raw.get("event"))
        if path and event_type:
            raise ValueError(
                f"step {raw.get('label') or path!r} names both a path and an "
                "event_type; a step is one or the other")
        if not path and not event_type:
            raise ValueError("a step needs a `path` or an `event_type`")
        kind = STEP_PATH if path else STEP_EVENT
        match = path or event_type
        out.append({"label": _text(raw.get("label")) or match,
                    "kind": kind, "match": match})
    return out


def _thread_key(row: dict) -> tuple[str, bool]:
    """(key, stitched_by_ip) for one page view.

    The cookie when there is one; the address when there is not. The
    fallback is deliberate rather than a purity failure: refusing to
    thread the cookieless would silently delete every visitor who sent Do
    Not Track from every funnel, which reports a smaller and cleaner
    business than the real one.
    """
    token = _text(row.get("session_id"))
    if token:
        return token, False
    ip = _text(row.get("ip"))
    if ip:
        return IP_THREAD_PREFIX + ip, True
    return "", False


def _matches(step: dict[str, str], event: tuple[str, str, str]) -> bool:
    _stamp, kind, value = event
    if kind != step["kind"]:
        return False
    if kind == STEP_PATH:
        return value.startswith(step["match"])
    return value == step["match"]


def funnel(
    page_views: Iterable[dict], conversions: Iterable[dict],
    steps: Sequence[Any],
) -> dict[str, Any]:
    """How many distinct visitors reached each step, and where they left.

    A step is a path prefix (`/shop`) or an event_type
    (`order_confirmed`), and the order is the operator's: this is a
    SEQUENCE, so a visitor counts at step N only if they had already
    reached step N-1 and then did step N afterwards. That "afterwards" is
    the difference between a funnel and a Venn diagram -- somebody who
    bought last week and browsed today did not convert today.

    Threading is by `session_id`, falling back to the IP address for rows
    that have none, and the result SAYS how much of itself is IP-stitched
    (`ip_stitched`, `ip_stitched_pct`, and a caveat in `caveats`). A
    funnel that hides its own uncertainty is the kind of analytics this
    repo is written against; an office behind one address is one thread
    that looks like a very decisive shopper.

    Conversions with no session token cannot be attached to any visitor at
    all. They are counted in `unthreaded_conversions` and named in a
    caveat rather than quietly dropped -- today that is MOST of them,
    because a back-office transition (an order confirmed by staff the next
    morning) has no browser anywhere near it.
    """
    steps = normalize_steps(steps)
    if not steps:
        return {
            "configured": False, "steps": [], "entered": 0, "converted": 0,
            "ip_stitched": 0, "ip_stitched_pct": 0.0,
            "unthreaded_conversions": 0, "caveats": [],
        }

    events: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    ip_threads: set[str] = set()

    for row in page_views:
        key, by_ip = _thread_key(row)
        if not key:
            continue
        events[key].append((_text(row.get("created_at")), STEP_PATH,
                            _text(row.get("path")) or "/"))
        if by_ip:
            ip_threads.add(key)

    unthreaded_conversions = 0
    for row in conversions:
        token = _text(row.get("session_id"))
        if not token:
            unthreaded_conversions += 1
            continue
        events[token].append((_text(row.get("created_at")), STEP_EVENT,
                              _text(row.get("event_type"))))

    reached: list[set[str]] = [set() for _ in steps]
    for key, timeline in events.items():
        timeline.sort(key=lambda event: event[0])
        cursor = -1
        for index, step in enumerate(steps):
            found = None
            for position in range(cursor + 1, len(timeline)):
                if _matches(step, timeline[position]):
                    found = position
                    break
            if found is None:
                break
            reached[index].add(key)
            cursor = found

    entered = len(reached[0])
    stitched = len(reached[0] & ip_threads)
    rows: list[dict[str, Any]] = []
    previous: int | None = None
    for index, step in enumerate(steps):
        count = len(reached[index])
        rows.append({
            "index": index,
            "label": step["label"],
            "kind": step["kind"],
            "match": step["match"],
            "visitors": count,
            "ip_stitched": len(reached[index] & ip_threads),
            "drop_off": 0 if previous is None else previous - count,
            "drop_off_pct": (None if not previous
                             else round(100.0 * (previous - count) / previous, 1)),
            "of_entered_pct": (round(100.0 * count / entered, 1)
                               if entered else 0.0),
        })
        previous = count

    caveats: list[str] = []
    if stitched:
        caveats.append(
            f"{stitched} of {entered} visitors in this funnel are stitched by "
            "IP address rather than by a visitor cookie -- rows written before "
            "the cookie existed, and everyone who sent Do Not Track. An IP is "
            "not a person: an office behind one connection is one thread.")
    if unthreaded_conversions:
        caveats.append(
            f"{unthreaded_conversions} conversion(s) carry no visitor token "
            "and could not be attached to any visitor, so they cannot appear "
            "in an event step here. A goal recorded by a back-office "
            "transition has no browser anywhere near it.")

    return {
        "configured": True,
        "steps": rows,
        "entered": entered,
        "converted": len(reached[-1]),
        "ip_stitched": stitched,
        "ip_stitched_pct": (round(100.0 * stitched / entered, 1)
                            if entered else 0.0),
        "unthreaded_conversions": unthreaded_conversions,
        "caveats": caveats,
    }


# --- new versus returning ----------------------------------------------------------

def returning_visitors(
    page_views: Iterable[dict], window_days: int = 30, *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """New versus returning, by visitor token, within the window.

    A token counts as RETURNING when it was seen on more than one calendar
    day inside the window, or at all before it. Days rather than visits,
    because a person who reads three pages over lunch has not returned;
    they are still here.

    **This is a floor and says so in the return value** (`floor: True`,
    `caveat`), not only in a comment somebody could render without. Every
    error in it points one way: a cleared cookie, a private window, a
    second browser and a phone are each counted as a new visitor. It is
    also blind by design to anyone who refused the cookie -- those page
    views are counted in `no_token_addresses` instead, so the size of the
    blind spot is on the same screen as the number it qualifies.
    """
    now = now or datetime.now(timezone.utc)
    earliest = (now - timedelta(days=window_days - 1)).strftime("%Y-%m-%d")

    days_seen: dict[str, set[str]] = defaultdict(set)
    seen_before_window: set[str] = set()
    untokened_addresses: set[str] = set()

    for row in page_views:
        day = _day(row.get("created_at"))
        if not day:
            continue
        token = _text(row.get("session_id"))
        if not token:
            if day >= earliest and _text(row.get("ip")):
                untokened_addresses.add(_text(row.get("ip")))
            continue
        if day < earliest:
            seen_before_window.add(token)
        else:
            days_seen[token].add(day)

    new = returning = 0
    for token, days in days_seen.items():
        if len(days) > 1 or token in seen_before_window:
            returning += 1
        else:
            new += 1

    counted = new + returning
    return {
        "window_days": window_days,
        "new": new,
        "returning": returning,
        "counted": counted,
        "returning_pct": (round(100.0 * returning / counted, 1)
                          if counted else 0.0),
        "no_token_addresses": len(untokened_addresses),
        "floor": True,
        "caveat": FLOOR_CAVEAT,
    }


# --- time to conversion ------------------------------------------------------------

def time_to_conversion(
    page_views: Iterable[dict], conversions: Iterable[dict],
) -> dict[str, Any]:
    """Days between a visitor's first recorded page and their conversion.

    The number that tells you whether your funnel is a funnel or a queue:
    a median of zero days is a shop people buy from on arrival, and a
    median of nine is a decision somebody goes away and thinks about.

    Only cookie-threaded conversions can answer it, and the two ways of
    not being able to are reported apart, because they mean different
    things and have different fixes:

    * `unthreaded` -- the conversion carries no visitor token at all
      (a back-office transition). Nothing about retention would help.
    * `no_first_view` -- it has a token, and no page view with that token
      survives. Their first visit aged out of a bounded log, so this
      conversion has a beginning nobody can see. Counted, never dropped:
      dropping them biases the median toward fast conversions, since the
      slowest journeys are exactly the ones whose start ages out first.

    A conversion timestamped BEFORE its visitor's first page view is clock
    skew or a row edited by hand; it is clamped to zero and counted in
    `before_first_view` rather than allowed to drag a median negative.
    """
    first_seen: dict[str, datetime] = {}
    for row in page_views:
        token = _text(row.get("session_id"))
        moment = _moment(row.get("created_at"))
        if not token or moment is None:
            continue
        if token not in first_seen or moment < first_seen[token]:
            first_seen[token] = moment

    samples: list[float] = []
    unthreaded = no_first_view = unreadable = before_first_view = 0
    for row in conversions:
        token = _text(row.get("session_id"))
        if not token:
            unthreaded += 1
            continue
        start = first_seen.get(token)
        if start is None:
            no_first_view += 1
            continue
        moment = _moment(row.get("created_at"))
        if moment is None:
            unreadable += 1
            continue
        days = (moment - start).total_seconds() / 86400.0
        if days < 0:
            before_first_view += 1
            days = 0.0
        samples.append(days)

    def _round(value: float) -> float:
        return round(value, 2)

    ordered = sorted(samples)
    caveats: list[str] = []
    if no_first_view:
        caveats.append(
            f"{no_first_view} conversion(s) have a visitor token whose first "
            f"page view is no longer in page_views. {AGED_OUT_CAVEAT}")
    if unthreaded:
        caveats.append(
            f"{unthreaded} conversion(s) carry no visitor token, so they have "
            "no first visit to measure from at all.")
    if before_first_view:
        caveats.append(
            f"{before_first_view} conversion(s) are timestamped before their "
            "visitor's first page view (clock skew, or a hand-edited row); "
            "clamped to zero days rather than allowed to pull the median "
            "below it.")

    return {
        "count": len(ordered),
        "median_days": _round(statistics.median(ordered)) if ordered else None,
        "p25_days": _round(_quantile(ordered, 0.25)) if ordered else None,
        "p75_days": _round(_quantile(ordered, 0.75)) if ordered else None,
        "min_days": _round(ordered[0]) if ordered else None,
        "max_days": _round(ordered[-1]) if ordered else None,
        "same_day": sum(1 for value in ordered if value < 1.0),
        "unthreaded": unthreaded,
        "no_first_view": no_first_view,
        "unreadable_timestamp": unreadable,
        "before_first_view": before_first_view,
        "caveats": caveats,
    }


def _quantile(ordered: list[float], fraction: float) -> float:
    """Nearest-rank quantile. Deliberately not interpolated: these are
    real observed journeys, and an interpolated p75 is a duration nobody
    actually took."""
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]
