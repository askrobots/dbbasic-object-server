"""action_pickup_slots -- the windows a customer may actually book.

GET|POST {now?, location_id?, limit?} -> {ok, slots: [...], lead_minutes}

This is the one question a storefront asks before it draws a time picker,
and the answer is deliberately narrow: OPEN, in the FUTURE, past the LEAD
TIME, and NOT AT CAPACITY. A slot that fails any of those is not returned
at all -- not returned-with-a-flag, not greyed out with a reason.

**"Can I have a pizza at 3am" is answered by not offering 3am.** A picker
that shows every window and refuses the impossible ones after the
customer has chosen a time, typed their name and entered a card is a
picker that wastes somebody's evening to enforce a rule it knew before
they started. The lead time (`pickup.lead_minutes`) is the shop saying
how long it needs; the correct place to spend that knowledge is the list,
not the refusal.

Both verbs, because both callers are real: a server-rendered storefront
GETs this while building a page, and a JSON client POSTs it. Neither
writes anything -- this object only reads -- so there is no CSRF-shaped
reason to insist on one of them.

`now` is an argument rather than a hidden call to the clock so a test can
stand at a chosen minute and watch the lead-time edge move, the same
posture app-billing's runner takes with `today`. In production nothing
passes it.

**The list is a snapshot, not a hold.** A slot returned here with one
place left may be taken by somebody else before this customer commits;
nothing is reserved by asking. That race is checked again at checkout,
where it is refused with the next free slot named -- see
action_checkout's own docstring for why this system accepts the race
rather than building a reservation ledger for it.
"""

import os
from datetime import datetime, timedelta

import object_records

ACTOR = "action_pickup_slots"

DEFAULT_LEAD_MINUTES = 30

# A page of times, not a calendar. Somebody choosing when to collect a
# coffee is choosing among the next few windows; a picker holding six
# hundred of them is a scroll bar pretending to be a choice. A caller who
# genuinely wants more says so with `limit`.
DEFAULT_LIMIT = 48
MAX_LIMIT = 500


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _truthy(value):
    return _text(value).lower() in ("true", "1", "yes", "on")


def _int(value, default=0):
    try:
        return int(_text(value) or default)
    except (TypeError, ValueError):
        return default


def _setting(base, key, default):
    try:
        for row in object_records.read_collection_records("app_settings",
                                                          base_dir=base):
            if row.get("key") == key and _text(row.get("value")):
                return _text(row["value"])
    except Exception:
        pass
    return default


def _moment(value):
    """An ISO datetime as NAIVE local wall-clock, or None when it is blank
    or unparseable.

    None is treated everywhere below as "not bookable": a slot whose start
    time cannot be read is a slot nobody can be promised, and offering it
    would put an unparseable string on somebody's order.

    The offset is dropped rather than honoured because every time in this
    repo is the shop's own clock, and because comparing one aware value to
    a naive one raises -- which would take a whole picker down over a
    single imported row.
    """
    text = _text(value)
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment


def lead_minutes(base):
    """How long the shop needs before it can have anything ready.

    Public because it is the number two objects have to agree on -- this
    one to decide what to offer, action_checkout to decide what to accept
    -- and a lead time those two disagreed about would show a customer a
    slot and then refuse it.
    """
    return max(0, _int(_setting(base, "pickup.lead_minutes", ""),
                       DEFAULT_LEAD_MINUTES))


def is_bookable(slot, *, now, earliest):
    """The whole rule, in one place: open, in the future, past the lead
    time, and with room.

    `earliest` (now + the lead time) is passed in rather than computed
    here so every slot in one pass is judged against exactly the same
    edge -- a boundary recomputed per row would let a slot pass or fail
    depending on how long the loop took to reach it.
    """
    starts = _moment(slot.get("starts_at"))
    if starts is None:
        return False
    if not _truthy(slot.get("is_open") or "true"):
        return False
    if starts <= now:
        return False
    if starts < earliest:
        return False
    return _int(slot.get("orders_taken"), 0) < _int(slot.get("capacity"), 0)


def _present(slot):
    capacity = _int(slot.get("capacity"), 0)
    taken = _int(slot.get("orders_taken"), 0)
    return {"id": slot["id"],
            "starts_at": _text(slot.get("starts_at")),
            "ends_at": _text(slot.get("ends_at")),
            "capacity": capacity,
            "orders_taken": taken,
            "places_left": max(0, capacity - taken),
            "location_id": _text(slot.get("location_id"))}


def _bookable(request):
    base = _base_dir()
    now = _moment(request.get("now")) or datetime.now()
    lead = lead_minutes(base)
    earliest = now + timedelta(minutes=lead)
    location = _text(request.get("location_id"))
    limit = _int(request.get("limit"), DEFAULT_LIMIT)
    limit = max(1, min(limit or DEFAULT_LIMIT, MAX_LIMIT))

    try:
        rows = object_records.read_collection_records("pickup_slots",
                                                      base_dir=base)
    except Exception:
        # A box without this app installed has no slots, which is a real
        # answer to "what can I book?" and not an error page on somebody's
        # storefront.
        return {"ok": True, "slots": [], "lead_minutes": lead,
                "now": now.isoformat(),
                "note": "pickup not installed (pickup_slots absent)"}

    slots = [row for row in rows
             if is_bookable(row, now=now, earliest=earliest)
             and (not location or _text(row.get("location_id")) == location)]
    slots.sort(key=lambda row: _text(row.get("starts_at")))
    return {"ok": True,
            "lead_minutes": lead,
            "now": now.isoformat(),
            "earliest_bookable": earliest.isoformat(),
            "count": len(slots),
            "slots": [_present(row) for row in slots[:limit]]}


def GET(request):
    return _bookable(request)


def POST(request):
    return _bookable(request)
