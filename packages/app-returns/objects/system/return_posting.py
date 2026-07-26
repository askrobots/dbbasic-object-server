"""system_return_posting -- the parcel landed; nobody has decided anything.

HANDLES shipments writes. It does exactly one thing, and the discipline of
that one thing is the point: when an INBOUND shipment reaches `received`,
it stamps the return authorization so the two records agree that the goods
are here. It moves no stock. It composes no refund. It touches no money at
all.

Goods arriving is not a decision. A box on the dock could hold a mug in
its original wrapping, the same mug in three pieces, or somebody's old
shoes -- and the difference decides whether stock goes up, whether a loss
is recorded, and whether money goes back. A handler that guessed would be
wrong in whichever direction was cheaper to code: guess restock and
damaged goods go on the shelf to be sold again; guess waste and sellable
stock is written off. So the decision stays with a human, in
action_disposition_return, where it is one explicit call that composes the
moves and the refund together and cannot half-happen.

Then why does this handler exist at all? Because without it the RMA and
its parcel silently disagree. The shipment would say `received` while the
authorization still said the goods were out with the customer, and every
list, count and reminder built on the RMA would be quietly a day (or a
month) behind the dock. Keeping two records in step is exactly the kind
of bookkeeping a reaction should do; deciding what is in the box is
exactly the kind it should not.

`past_expiry` in the result is the one judgement it does make, and it is a
statement rather than an action: the goods arrived after the day we said
the offer ran out. Nothing is refused for it -- a box on the dock is a box
on the dock, and refusing to write down that it arrived would make the
process less auditable, not more -- but the person about to choose
restock-or-refund gets told they are outside the promise BEFORE they
choose, which is the only moment the fact is worth anything. The daemon
pass that sweeps stale authorizations to `expired` belongs with the other
time-driven work in the tracking slice (plan/fulfillment-logistics-spec.md
item 4) and is deliberately not invented here.

Placement follows docs/logic-decisions.md #6 -- a REACTION, post-commit,
best-effort, never blocking the write that triggered it. A dock that
cannot stamp an RMA must still be able to record that a parcel arrived.

Idempotent by observable state (#7): an authorization already past
`authorized` is left exactly where it is, so a replayed event -- and
events are replayed here by design, the change dispatcher promises
at-least-once -- changes nothing. Note that there is no separate
`received` status on the authorization: the parcel's status is the
parcel's business, and duplicating it onto the RMA would create two places
to disagree about the same fact. What the RMA gains from this handler is
the check that it is still open at all, and a loud result when it is not.
"""

import os
from datetime import date

import object_records

HANDLES = [
    "shipments.record.created",
    "shipments.record.updated",
]

ACTOR = "system_return_posting"

# The parcel is on our dock and unjudged.
RECEIVED_STATUS = "received"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _shipment_for(request, base):
    """The shipment this event is about.

    Both the record itself and a bare record_id are accepted: the HTTP
    dispatcher and the change-log dispatcher both send record_id, while an
    operator poking this by hand (or a sibling handler calling it in
    process) has the row already. The row is re-read either way -- an event
    carrying a stale copy, dispatched from the change log minutes later,
    must not make this handler act on a status the parcel has since moved
    past.
    """
    record = request.get("record")
    record_id = _text(request.get("record_id") or request.get("id"))
    if isinstance(record, dict) and record.get("id"):
        record_id = _text(record.get("id"))
    if not record_id:
        return None
    try:
        return object_records.get_collection_record("shipments", record_id,
                                                    base_dir=base)
    except Exception:
        return None


def _rma_for(base, shipment_id):
    try:
        rows = object_records.read_collection_records("return_authorizations",
                                                      base_dir=base)
    except Exception:
        return None
    for row in rows:
        if _text(row.get("shipment_id")) == shipment_id:
            return row
    return None


def EVENT(request):
    base = _base_dir()
    shipment = _shipment_for(request, base)
    if not shipment:
        return {"ok": True, "skipped": "no shipment in the event"}

    if _text(shipment.get("direction")) != "inbound":
        # Outbound parcels are system_order_fulfillment's business, and two
        # handlers reacting to the same row is how one of them ends up
        # undoing the other's work.
        return {"ok": True, "skipped": "outbound shipments are not returns",
                "shipment_id": shipment["id"]}

    if _text(shipment.get("status")) != RECEIVED_STATUS:
        return {"ok": True, "skipped": f"shipment is "
                                       f"{_text(shipment.get('status'))}, not "
                                       f"received",
                "shipment_id": shipment["id"]}

    rma = _rma_for(base, shipment["id"])
    if not rma:
        # Goods came back against a shipment nobody authorized. Not this
        # handler's job to invent the paperwork -- but saying so is how the
        # discrepancy gets found while somebody is still holding the box.
        return {"ok": True, "shipment_id": shipment["id"],
                "warning": "no return authorization points at this inbound "
                           "shipment; the goods are here and nothing says who "
                           "agreed to take them back"}

    result = {"ok": True, "shipment_id": shipment["id"],
              "return_id": rma["id"], "return_status": _text(rma.get("status"))}

    expires_on = _text(rma.get("expires_on"))
    if expires_on and expires_on < date.today().isoformat():
        # A statement, never a refusal: the box is here either way. The
        # value of saying it now is that the person about to choose
        # restock-or-refund learns it BEFORE they choose.
        result["past_expiry"] = True
        result["expires_on"] = expires_on

    status = _text(rma.get("status"))
    if status == "requested":
        # The goods turned up against a return nobody had granted yet. The
        # parcel is the more authoritative fact -- it exists -- so the
        # authorization catches up rather than the dock being told it is
        # wrong.
        try:
            object_records.update_collection_record(
                "return_authorizations", rma["id"], {"status": "authorized"},
                base_dir=base, actor=ACTOR)
            result["return_status"] = "authorized"
            result["return_status_changed"] = True
        except Exception as exc:
            result["return_status_error"] = str(exc)[:200]
    elif status in ("closed", "declined"):
        result["warning"] = (f"this return is already {status}, but a parcel "
                             f"has arrived against it")

    result["note"] = ("the goods are here and nothing financial has happened: "
                      "a human decides restock or dispose, and the refund (if "
                      "any) goes with that decision, in "
                      "action_disposition_return")
    return result


# EVENT is the verb the change dispatcher calls handlers with (see
# object_change_dispatch); POST stays as an alias so an operator can poke
# the handler by hand over HTTP. The alias is not decoration -- a handler
# shipped with only POST silently matched nothing in production once, and
# every handler in this house has carried both ever since.
POST = EVENT
