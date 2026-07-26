"""system_slot_generator -- tomorrow's collection windows, made overnight.

POST {today?, days_ahead?, dry_run?} -- the daily pass that turns an
opening-hours setting into the rows a storefront's time picker reads.

**A shop that has to create tomorrow's slots by hand will one day forget,
and take no orders at all.** That sentence is the whole design. Not a
page with an "add slot" button, not a wizard somebody runs on a Sunday:
a scheduled pass, declared in this package's manifest, that runs before
the shop opens and leaves the next N days bookable whether or not anybody
thought about it. The failure it exists to prevent is silent and total --
the storefront offers no times, every customer is told the shop cannot
take their order, and nothing anywhere logs an error, because a picker
with nothing in it is indistinguishable from a shop that is fully booked.

Five settings say what a day looks like, and every one of them has a
default, so a shop that has configured nothing still gets a sensible
board rather than an empty one:

  pickup.open_time          "09:00" -- when the first window starts
  pickup.close_time         "17:00" -- no window may end after this
  pickup.slot_minutes       30      -- how long each window is
  pickup.capacity_per_slot  4       -- orders, not items (see the schema)
  pickup.days_ahead         7       -- how far out to build
  pickup.location_id        ""      -- which counter these belong to

**Idempotent by (starts_at, location_id).** Re-running the pass -- twice
in a night, after a crash, or by an operator poking it to see what it
does -- creates nothing that already exists, and the identity is those
two fields rather than a generated marker because a slot IS its time and
its counter. The same property app-billing's runner buys with a
provenance marker in `notes`, bought here by the row's own natural key,
which is stronger: an operator who typed a 6pm slot by hand gets it
adopted rather than duplicated, and a shop with two counters gets two
6pm slots rather than one.

It builds from TODAY, not from tomorrow. The window that has already
passed is harmless -- action_pickup_slots never offers a slot inside the
lead time, so this morning's nine o'clock is filtered out by the thing
whose job that is -- and a shop that installs this app at eight in the
morning must be able to take lunch orders today rather than discovering
its own product tomorrow.

What it deliberately does NOT do: close slots, change capacity on slots
that already exist, or delete anything. A slot somebody edited is a
decision, and a nightly pass that restated it would overwrite the
operator every single night -- the same rule the package installer holds
to for a paused schedule and a hidden nav entry. This pass only ever
adds.
"""

import os
from datetime import date, datetime, timedelta

import object_ids
import object_records

ACTOR = "system_slot_generator"

DEFAULT_OPEN = "09:00"
DEFAULT_CLOSE = "17:00"
DEFAULT_SLOT_MINUTES = 30
DEFAULT_CAPACITY = 4
DEFAULT_DAYS_AHEAD = 7

# A guard, not a preference. days_ahead comes from a settings row anybody
# with a manager role can type into, and a fat-fingered 3650 would fold
# every collection in the shop into building a decade of empty windows on
# the same box that is serving requests.
MAX_DAYS_AHEAD = 60


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _truthy(value):
    return _text(value).lower() in ("true", "1", "yes", "on")


def _setting(base, key, default):
    """Duplicated, on purpose, from every other package that reads
    app_settings: there is no shared settings module in this codebase yet
    and inventing one for an nth copy is the layer docs/logic-decisions.md
    #4 says to wait on.
    """
    try:
        for row in object_records.read_collection_records("app_settings",
                                                          base_dir=base):
            if row.get("key") == key and _text(row.get("value")):
                return _text(row["value"])
    except Exception:
        pass
    return default


def _int(value, default=0):
    try:
        return int(_text(value) or default)
    except (TypeError, ValueError):
        return default


def _minutes_of_day(value, default):
    """"09:00" -> 540. A malformed setting falls back to the default
    rather than raising: a typo in an opening time must cost the shop its
    unusual hours for one night, not its whole board of slots.
    """
    text = _text(value) or default
    parts = text.split(":")
    try:
        hours, minutes = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        parts = default.split(":")
        hours, minutes = int(parts[0]), int(parts[1])
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        parts = default.split(":")
        hours, minutes = int(parts[0]), int(parts[1])
    return hours * 60 + minutes


def _stamp(day, minutes):
    return (datetime(day.year, day.month, day.day)
            + timedelta(minutes=minutes)).isoformat()


def _existing_keys(base):
    """Every (starts_at, location_id) already on the board.

    An unreadable collection is NOT treated as an empty one: returning an
    empty set there would make the pass create a duplicate of every slot
    it could not see, which is the one failure mode worse than creating
    none. None means "cannot tell", and the caller declines.
    """
    try:
        rows = object_records.read_collection_records("pickup_slots",
                                                      base_dir=base)
    except Exception:
        return None
    return {(_text(row.get("starts_at")), _text(row.get("location_id")))
            for row in rows}


def _settings(base):
    open_minutes = _minutes_of_day(_setting(base, "pickup.open_time", ""),
                                   DEFAULT_OPEN)
    close_minutes = _minutes_of_day(_setting(base, "pickup.close_time", ""),
                                    DEFAULT_CLOSE)
    slot_minutes = _int(_setting(base, "pickup.slot_minutes", ""),
                        DEFAULT_SLOT_MINUTES)
    if slot_minutes <= 0:
        slot_minutes = DEFAULT_SLOT_MINUTES
    capacity = _int(_setting(base, "pickup.capacity_per_slot", ""),
                    DEFAULT_CAPACITY)
    if capacity < 0:
        capacity = DEFAULT_CAPACITY
    days_ahead = _int(_setting(base, "pickup.days_ahead", ""),
                      DEFAULT_DAYS_AHEAD)
    days_ahead = max(0, min(days_ahead, MAX_DAYS_AHEAD))
    return {"open_minutes": open_minutes, "close_minutes": close_minutes,
            "slot_minutes": slot_minutes, "capacity": capacity,
            "days_ahead": days_ahead,
            "location_id": _setting(base, "pickup.location_id", "")}


def _starts_for_day(day, config):
    """Every window start on one day.

    A slot whose END would fall past closing time is not created: a shop
    that closes at five does not hand somebody their dinner at five past,
    and a half-length last window is a promise made by arithmetic rather
    than by anybody who works there.
    """
    starts = []
    minute = config["open_minutes"]
    while minute + config["slot_minutes"] <= config["close_minutes"]:
        starts.append(minute)
        minute += config["slot_minutes"]
    return [(_stamp(day, m), _stamp(day, m + config["slot_minutes"]))
            for m in starts]


def POST(request):
    base = _base_dir()
    dry_run = _truthy(request.get("dry_run"))
    try:
        today = date.fromisoformat(_text(request.get("today"))
                                   or date.today().isoformat())
    except ValueError:
        return {"status": 400, "error": "today must be an ISO date (YYYY-MM-DD)"}

    config = _settings(base)
    # An explicit argument beats the setting, so an operator can build a
    # long weekend without editing the shop's standing configuration.
    if _text(request.get("days_ahead")):
        config["days_ahead"] = max(0, min(_int(request.get("days_ahead"),
                                               config["days_ahead"]),
                                          MAX_DAYS_AHEAD))

    existing = _existing_keys(base)
    if existing is None:
        # No pickup_slots collection (this app is not installed on this
        # box, or its records file is unreadable). Saying so beats both
        # raising -- the scheduler would log a failure every night on a
        # shop that never wanted pickup -- and pretending it worked.
        return {"ok": True, "skipped": "pickup not installed (pickup_slots absent)"}

    created = 0
    skipped = 0
    days = []
    for offset in range(config["days_ahead"] + 1):
        day = today + timedelta(days=offset)
        made = 0
        for starts_at, ends_at in _starts_for_day(day, config):
            key = (starts_at, config["location_id"])
            if key in existing:
                skipped += 1
                continue
            existing.add(key)
            made += 1
            if dry_run:
                continue
            object_records.create_collection_record(
                "pickup_slots",
                {
                    "id": object_ids.new_uuid4(),
                    "starts_at": starts_at,
                    "ends_at": ends_at,
                    "capacity": str(config["capacity"]),
                    "orders_taken": "0",
                    "is_open": "true",
                    "location_id": config["location_id"],
                    "notes": f"Generated by {ACTOR}",
                    "owner_id": "",
                },
                base_dir=base, actor=ACTOR)
        created += made
        days.append({"day": day.isoformat(), "created": made})

    return {"ok": True, "today": today.isoformat(),
            "days_ahead": config["days_ahead"],
            "slot_minutes": config["slot_minutes"],
            "capacity_per_slot": config["capacity"],
            "location_id": config["location_id"],
            "created": created, "already_there": skipped,
            "days": days,
            **({"dry_run": True} if dry_run else {})}
