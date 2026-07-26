"""system_tracking_poll -- ask the carrier where the parcel is, and stop
asking about the ones that will never answer.

POST {limit?, today?} -- the scheduled pass declared in this package's
`schedules`. It reads; it writes back only what the carrier said, plus the
one derived move that follows from it: a parcel the carrier reports as
delivered becomes `delivered`, and system_order_fulfillment turns that into
an order status the way it already turns `shipped` into one. This object
therefore never touches an order, never composes a stock move and never
sends anything -- it is a reader with one status stamp, and everything
downstream of that stamp is somebody else's already-tested job.

Time-driven state belongs to a daemon pass (docs/logic-decisions.md #2).
Nothing writes when a van drives, so no reaction can notice a delivery, and
a surface computing "probably arrived by now" from a ship date would be
exactly the fake precision the shipments schema refuses elsewhere. Hourly
is the cadence, argued in the manifest: a carrier scan lands a handful of
times a day, so hourly is well inside the resolution of the underlying
facts, while a shop with parcels out is a shop whose customers are emailing
about them TODAY -- a nightly pass would mean the answer to "did it arrive?"
is up to a day stale exactly when somebody is asking.

WHICH PARCELS. Outbound only, in `shipped` or `in_transit`, with a tracking
number, that have not already failed MAX_ATTEMPTS times. Outbound only is a
decision, not an omission: an inbound RMA's ladder ends at `received`, which
means a human has the box on the dock and has looked at it. A carrier saying
"delivered" is not that fact, and writing it in as though it were would tell
the returns bench a parcel had arrived and been accepted when nobody had
touched it. No tracking number means there is nothing to ask WITH; the
manual shop that hands parcels to a driver is not failing, it simply is not
in this queue.

FAILURE POSTURE, copied from system_scan_processor deliberately, because
this is the same shape of problem: something outside the box will sometimes
not answer, and a pass that treats every silence identically either churns a
metered API forever or retires a whole warehouse's parcels the day somebody
mistypes a key. So the two silences are told apart explicitly and by the
CONNECTOR'S OWN WORD rather than a third spelling of it:

  the host is not set up   `{"skip": True}` from the carrier, or no carrier
                           configured at all. The row is untouched, NOTHING
                           is counted against it, and the pass says why in
                           its report. Fixing the configuration drains the
                           backlog; nothing had to be un-failed first.
  this parcel cannot be    `{"ok": False, "error": ...}`. tracking_attempts
  read                     goes up, tracking_error records what was said,
                           and at MAX_ATTEMPTS the parcel stops being polled
                           and starts being VISIBLE in `needing_a_human` --
                           and in the stuck-parcel attention count, which is
                           where a human actually finds it.

`permanent` jumps straight to the attempt ceiling. A carrier that says a
tracking number does not exist will say the same thing tomorrow, and three
polite retries against a typo is just three more chances to be wrong slowly.
"""

import os
from datetime import date, datetime, timezone
from pathlib import Path

import object_connectors
import object_packages
import object_records

ACTOR = "system_tracking_poll"

DEFAULT_LIMIT = 50
MAX_ATTEMPTS = 3

# Outbound and still on the road. `delivered`, `lost` and
# `returned_to_sender` are all terminal answers about a parcel; asking the
# carrier again is spending somebody's API quota on a settled fact.
POLLABLE = {"shipped", "in_transit"}


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _int(value, default=0):
    try:
        return int(_text(value) or default)
    except (TypeError, ValueError):
        return default


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _setting(base, key, default=""):
    """Duplicated on purpose, same as every other package that reads
    app_settings (docs/logic-decisions.md #4)."""
    try:
        for row in object_records.read_collection_records("app_settings",
                                                          base_dir=base):
            if row.get("key") == key and _text(row.get("value")):
                return _text(row["value"])
    except Exception:
        pass
    return default


# --- the carrier boundary -----------------------------------------------------
#
# The next two helpers are a deliberate second copy of the pair in
# action_buy_label, named in both files. Objects cannot import each other on
# this platform (a sibling is reached by executing it, not by importing it),
# and #4 says to wait for the third occurrence before extracting a layer --
# so the third carrier-facing object is the one that should be made to hurt,
# not this one.

def _carrier_declaration():
    """The connector declaration that owns `shipments` on this box.

    Resolved through object_packages.iter_connectors, which is the existing
    mechanism: it validates that the module lives inside its package, hands
    back an absolute path, and lets a private overlay's declaration win --
    exactly the resolution object_daemon's reconcile pass uses. Finding the
    module any other way would be a second registry for connector modules,
    and two registries is how a deployment ends up running an adapter
    nobody declared.
    """
    packages = os.environ.get("DBBASIC_PACKAGES_DIR", object_packages.PACKAGES_DIR)
    private = (os.environ.get("DBBASIC_PRIVATE_PACKAGES_DIR")
               or str(Path(packages).parent / "packages-private"))
    roots = ([private] if Path(private).is_dir() else []) + [packages]
    for root in roots:
        for declaration in object_packages.iter_connectors(root=root):
            if declaration["collection"] == "shipments":
                return declaration
    return None


def _carrier(base, entry):
    """(function, provider, problem) -- the configured carrier's `entry`, or
    the honest reason there is not one.

    A `problem` is always a HOST problem: nothing is configured, nothing is
    installed, or what is installed answers to a different name than the
    setting. None of those are a parcel's fault, so every caller here treats
    a problem as a decline rather than a failure.
    """
    provider = _setting(base, "carrier.provider", "none").lower() or "none"
    if provider == "none":
        return None, provider, (
            "carrier.provider is `none`, so nobody is asked and nothing is "
            "counted: tracking is whatever an operator typed on the shipment. "
            "Set app_settings carrier.provider to `manual` or to an installed "
            "carrier adapter's name to change that.")

    declaration = _carrier_declaration()
    if declaration is None:
        return None, provider, (
            f"carrier.provider is {provider!r} but no installed package "
            f"declares a connector for the shipments collection, so there is "
            f"nothing to ask.")
    try:
        function = object_connectors.load_connector(declaration["module"], entry)
    except object_connectors.ConnectorLoadError as exc:
        return None, provider, (
            f"the carrier connector for {provider!r} could not be loaded "
            f"({str(exc)[:160]}), so no parcel was asked about.")

    # The module's own name for itself, read off the loaded function's
    # globals -- load_connector deliberately does not register the module in
    # sys.modules, and a second loader just to read one constant would be a
    # second loader.
    declared = _text(function.__globals__.get("PROVIDER"))
    if declared and declared.lower() != provider:
        return None, provider, (
            f"carrier.provider is {provider!r} but the connector installed "
            f"for shipments answers to {declared!r}. Nothing was asked: "
            f"quietly using a carrier the shop did not configure is worse "
            f"than a pass that did nothing and said so.")
    return function, provider, ""


# --- the pass ------------------------------------------------------------------

def POST(request):
    base = _base_dir()
    limit = _int(request.get("limit"), DEFAULT_LIMIT) or DEFAULT_LIMIT
    today = _text(request.get("today")) or date.today().isoformat()

    try:
        shipments = object_records.read_collection_records("shipments",
                                                           base_dir=base)
    except Exception:
        return {"ok": True, "skipped": "shipping not installed (shipments absent)"}

    on_the_road = [row for row in shipments
                   if _text(row.get("direction")) != "inbound"
                   and _text(row.get("status")) in POLLABLE]
    # Parcels that have already used up their attempts are not polled again;
    # they are REPORTED, which is the difference between a queue that drains
    # and one that grinds.
    stuck = [row["id"] for row in on_the_road
             if _int(row.get("tracking_attempts")) >= MAX_ATTEMPTS]
    pending = [row for row in on_the_road
               if _text(row.get("tracking_number"))
               and _int(row.get("tracking_attempts")) < MAX_ATTEMPTS]
    # Least recently answered first, so one busy parcel cannot starve the
    # rest when a shop has more in flight than `limit`. A blank
    # tracking_checked_at sorts first, which is right: never asked is the
    # oldest answer there is.
    pending.sort(key=lambda row: (_text(row.get("tracking_checked_at")),
                                  _text(row.get("created_at"))))

    report = {"ok": True, "today": today, "considered": len(pending),
              "polled": 0, "delivered": 0, "failed": 0, "declined": 0,
              "needing_a_human": stuck, "results": []}

    track, provider, problem = _carrier(base, "track")
    report["provider"] = provider
    if problem:
        # Not an attempt against anybody. The host is what is unfinished.
        report["skipped"] = problem
        return report

    for shipment in pending[:limit]:
        shipment_id = shipment["id"]
        try:
            outcome = track(shipment, base_dir=base)
            if not isinstance(outcome, dict):
                outcome = {"ok": False,
                           "error": (f"the carrier connector returned "
                                     f"{type(outcome).__name__}, expected a dict")}
        except Exception as exc:                      # noqa: BLE001
            # One bad parcel must not end the pass for the rest of the shop.
            outcome = {"ok": False,
                       "error": f"the carrier connector raised: {str(exc)[:160]}"}

        if outcome.get("skip"):
            report["declined"] += 1
            report["results"].append({"shipment": shipment_id,
                                      "declined": _text(outcome.get("reason"))})
            continue

        if not outcome.get("ok"):
            attempts = _int(shipment.get("tracking_attempts")) + 1
            if outcome.get("permanent"):
                # Asking again will produce the same sentence.
                attempts = max(attempts, MAX_ATTEMPTS)
            error = _text(outcome.get("error"))[:500]
            object_records.update_collection_record(
                "shipments", shipment_id,
                {"tracking_attempts": str(attempts), "tracking_error": error,
                 "tracking_checked_at": _now()},
                base_dir=base, actor=ACTOR)
            report["failed"] += 1
            report["results"].append({"shipment": shipment_id, "error": error,
                                      "attempts": attempts})
            if attempts >= MAX_ATTEMPTS and shipment_id not in stuck:
                stuck.append(shipment_id)
            continue

        update = {
            "tracking_status": (_text(outcome.get("status"))
                                or _text(shipment.get("tracking_status"))),
            "tracking_checked_at": _now(),
            "tracking_attempts": "0",
            "tracking_error": "",
        }
        arrived = bool(outcome.get("delivered"))
        if arrived:
            update["status"] = "delivered"
            update["delivered_on"] = (_text(outcome.get("delivered_on"))
                                      or _text(shipment.get("delivered_on"))
                                      or today)
        result = {"shipment": shipment_id}
        try:
            object_records.update_collection_record(
                "shipments", shipment_id, update, base_dir=base, actor=ACTOR)
        except Exception as exc:                      # noqa: BLE001
            # A ladder that refuses the move is a real answer, not a crash:
            # keep what the carrier SAID, drop only the derived move, and say
            # so. Losing the carrier's words because our own enum disagreed
            # would leave nothing for a human to work from.
            arrived = False
            update.pop("status", None)
            update.pop("delivered_on", None)
            object_records.update_collection_record(
                "shipments", shipment_id, update, base_dir=base, actor=ACTOR)
            result["status_error"] = str(exc)[:200]

        report["polled"] += 1
        if arrived:
            report["delivered"] += 1
        result["tracking_status"] = update["tracking_status"]
        result["delivered"] = arrived
        report["results"].append(result)

    report["needing_a_human"] = stuck
    return report
