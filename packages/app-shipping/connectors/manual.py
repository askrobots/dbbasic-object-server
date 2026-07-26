"""The manual carrier: the operator IS the integration.

This is app-shipping's own carrier connector, declared in the manifest
under `connectors` for the `shipments` collection and loaded at runtime by
object_connectors.load_connector -- the mechanism the mail connector spec
already established (plan/vocabulary/03-external-connectors-spec.md).
Open core never imports it statically; a deployment that swaps in a real
carrier installs a package declaring `connectors` for `shipments` too, and
the private overlay's declaration wins for the whole box. That swap is the
entire point of this file existing rather than the manual behaviour being
welded into the two objects that use it: if the free path is not shaped
exactly like the paid one, the paid one is a rewrite rather than a plug-in.

THE CARRIER INTERFACE, in full, so an adapter author needs nothing else:

    PROVIDER = "manual"      the value of app_settings carrier.provider this
                             module answers to. The objects refuse to use a
                             module whose PROVIDER disagrees with the
                             setting rather than quietly doing something
                             other than what the shop configured.

    track(shipment, *, base_dir) -> outcome
        Ask the carrier where this parcel is.
        {"ok": True, "status": "Out for delivery", "delivered": False}
        {"ok": True, "status": "Delivered", "delivered": True,
         "delivered_on": "2026-07-24"}      -- moves the shipment to delivered

    buy_label(shipment, *, base_dir, tracking_number="", label_bytes=None,
              label_content_type="", label_filename="") -> outcome
        Get a label for this parcel. Direction is on the shipment: an
        INBOUND shipment is a return label, and there is deliberately no
        second verb for it -- a return label is the same purchase with the
        addresses the other way round, and two verbs would be two places
        for the credentials, the errors and the file to drift.
        {"ok": True, "tracking_number": "1Z...", "label_bytes": b"%PDF...",
         "label_content_type": "application/pdf", "label_filename": "..."}

    reconcile(record, *, base_dir) -> outcome
        The entry object_daemon's connector pass calls. See below.

Every one of them answers in the SAME vocabulary object_connectors already
defines for reconcile, and reusing it is deliberate:

    {"ok": True, ...}                     it worked; here is what we learned
    {"skip": True, "reason": "..."}       declined this tick -- the HOST is
                                          not set up. The row is left exactly
                                          as it is and NOTHING is counted
                                          against it.
    {"ok": False, "error": "..."}         THIS parcel could not be read; count
                                          the attempt against it.
    {"ok": False, "error": "...",
     "permanent": True}                   ...and do not bother asking again.

That distinction is the one system_scan_processor spells `recoverable`, and
it is the difference between "somebody has not pasted the API key in yet"
and "this tracking number is nonsense". Spelling it a third way here would
mean three subsystems that agree about the rule and disagree about the word,
so this file borrows the connector contract's word and the poll borrows its
meaning.

WHAT `manual` ACTUALLY DOES:

`track` always declines. There is no carrier to ask -- the operator is the
one who reads the tracking page and types what it said, and a poll that
"succeeded" every hour by writing back the status a human typed would burn
tracking_checked_at into a timestamp that means nothing. Declining is the
honest answer, and because it declines rather than fails, nothing is ever
counted against a manually-tracked parcel.

`buy_label` accepts what the operator brings back from the post office
counter: the tracking number, and optionally the label PDF they were handed.
It buys nothing and pretends to buy nothing. It is still worth routing
through the connector interface, because it is what proves the interface
covers the free path -- and because the day a real adapter lands, the
operator-typed path is still there for the parcel the API refuses to quote.

`reconcile` declines every tick, always, and that is not a stub. The
daemon's connector pass converges an external system toward what our
records DESIRE; a parcel is the opposite shape of fact. We cannot make a
van arrive by wishing, the carrier's truth flows toward us rather than away,
and reading it is time-driven work that belongs to a scheduled pass
(docs/logic-decisions.md #2) -- system_tracking_poll, declared in this
package's `schedules`. The declaration exists so that the module is
DISCOVERABLE by the mechanism that already resolves connector modules
safely (escape-checked, package-relative, dynamically loaded); implementing
`reconcile` as an explicit decline is how it stays discoverable without the
reconcile pass quietly acquiring a meaning nobody wanted.
"""

PROVIDER = "manual"

# Kept short because it is written into a poll's report and read by somebody
# scanning a list of skipped rows.
_NO_CARRIER = ("carrier.provider is manual: tracking is whatever the operator "
               "typed, so there is nobody to ask")


def track(shipment, *, base_dir=None):
    """Decline, always -- see the module docstring. Not an attempt, not a
    failure: the row waits exactly as the operator left it."""
    return {"skip": True, "reason": _NO_CARRIER}


def buy_label(shipment, *, base_dir=None, tracking_number="",
              label_bytes=None, label_content_type="", label_filename=""):
    """Take what the human brings back from the counter.

    The one refusal here is an empty tracking number, and it is a refusal
    rather than a shrug because the whole reason to press this button
    manually is to put a tracking number on the parcel -- stamping an empty
    string would leave a shipment that claims to have a label and cannot be
    followed. The label FILE is optional: plenty of shops print the label
    from the carrier's own site and never hold the bytes, and refusing them
    a tracking number over a missing PDF would be paperwork for its own
    sake.
    """
    number = str(tracking_number or "").strip()
    if not number:
        return {"ok": False, "permanent": True,
                "error": ("A manual label needs the tracking number the "
                          "carrier gave you -- that number is the whole "
                          "point of the button, and a label with no number "
                          "cannot be followed by anybody.")}
    outcome = {"ok": True, "tracking_number": number,
               "provider": PROVIDER,
               "note": ("recorded from the counter; nothing was bought and "
                        "nothing pretended to be")}
    if label_bytes:
        outcome["label_bytes"] = label_bytes
        outcome["label_content_type"] = (str(label_content_type or "").strip()
                                         or "application/pdf")
        outcome["label_filename"] = (str(label_filename or "").strip()
                                     or f"label-{number}.pdf")
    return outcome


def reconcile(record, *, base_dir=None):
    """The daemon's desired-state pass has nothing to converge here."""
    return {"skip": True,
            "reason": ("a shipment is not desired state: the carrier's truth "
                       "flows toward us, and reading it is system_tracking_"
                       "poll's scheduled job")}
