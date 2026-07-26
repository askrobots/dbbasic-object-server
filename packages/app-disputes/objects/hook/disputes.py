"""Pre-write hook for disputes: a dispute may not be resolved unless it
names a resolution, and the name has to resolve.

This is the one rule the whole package exists for, and it is a GATE
rather than a convention on purpose. A convention is a sentence in a
docstring that a busy Friday afternoon overrules; a gate is the reason
the sentence stays true. What it prevents is specific and expensive: a
dispute moved to `resolved` with nothing behind it, which is how a
customer gets told "it's sorted" when no money went back, no parcel was
raised, and nobody wrote down why not. Every part of that failure is
invisible afterwards -- the row looks finished, the queue count goes
down, and the only person who knows is the customer who is still
waiting.

Three checks, in the order they matter.

**A resolution has to be NAMED.** status `resolved` with a blank
resolution_kind is refused. There is no default and there deliberately
cannot be one: guessing `no_action` would close claims nobody decided
and guessing `refund` would claim money moved.

**`no_action` has to say WHY.** It is a legitimate ending -- a claim
outside the return window, a carrier delay nobody owes for, a parcel the
customer found behind the bins -- and pretending otherwise would push
operators into fake refunds to escape a gate. But "we are not
compensating this" is a decision somebody will be asked to justify
weeks later, usually by the customer and occasionally by a card
processor, so it carries a sentence in resolution_note or it is not a
decision, it is a cleared queue.

**refund / replacement / credit have to point at a REAL record.** Each
kind implies exactly one collection -- refund -> refunds, replacement ->
orders, credit -> wallet_entries -- and the reference is parsed,
checked against that collection, and looked up. A reference to the wrong
collection is refused by name, and so is one naming a row that does not
exist, because a plausible-looking pointer at nothing is strictly worse
than a blank field: a blank field is visibly unfinished and
`refunds/abc123` reads as done. Both refusals carry the ref, the
collection it should have been, and the collection it actually named --
"no" alone leaves somebody guessing which half they got wrong.

Fail closed, the way every gate in this house does: a collection that
cannot be read at all refuses the resolution rather than waving it
through, because a gate that passes on error is not a gate. The one
exception is a collection that does not EXIST -- wallet_entries on a
server with no app-billing -- which is refused too, with a message
naming the missing app, because a credit nobody can issue is not a
resolution either.

Trusted server-side writes bypass hooks by design
(docs/validation-and-logic.md), so action_resolve_dispute carries the
same three checks itself and reports them the friendly way. That
duplication is deliberate and named in both files: the hook guards the
generic HTTP write path a form or an API client uses, the action guards
the one door that also composes the compensating record, and removing
either opens the other's. app-receiving's hook/action pair says the same
thing about over-receiving; this is the same shape.
"""

import os

import object_collections
import object_records

RESOLVED = "resolved"

# Each ending implies exactly one collection. The map IS the rule: a
# refund that points at an order and a replacement that points at a
# refund are both stories about something that did not happen.
RESOLUTION_COLLECTIONS = {
    "refund": "refunds",
    "replacement": "orders",
    "credit": "wallet_entries",
}

# Endings that need a record behind them, versus the one that needs a
# sentence.
NEEDS_A_RECORD = tuple(RESOLUTION_COLLECTIONS)
NEEDS_A_REASON = "no_action"

RESOLUTION_KINDS = NEEDS_A_RECORD + (NEEDS_A_REASON,)


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _split_ref(ref):
    """'refunds/abc' -> ('refunds', 'abc'). Anything else -> ('', '').

    Split once from the left so an id containing a slash still round-trips
    rather than being silently truncated into a different id.
    """
    if "/" not in ref:
        return "", ""
    collection, _, record_id = ref.partition("/")
    return _text(collection), _text(record_id)


def check_resolution(record, *, base_dir=None):
    """The gate itself, as a function, so action_resolve_dispute can hold
    the identical rule without a second vocabulary for it.

    Returns a refusal dict ({"error", "status"}) or None.
    """
    base = base_dir or _base_dir()
    dispute_id = _text(record.get("id")) or "(new)"
    kind = _text(record.get("resolution_kind"))

    if not kind:
        return {"error": (
            f"Dispute {dispute_id} cannot be resolved without saying HOW. "
            f"Name a resolution_kind -- one of "
            f"{', '.join(RESOLUTION_KINDS)} -- because a dispute closed "
            f"with nothing behind it is how a customer gets told it is "
            f"sorted when nothing happened."), "status": 400}

    if kind not in RESOLUTION_KINDS:
        return {"error": (
            f"Unknown resolution_kind {kind!r} on dispute {dispute_id}; "
            f"expected one of {', '.join(RESOLUTION_KINDS)}."), "status": 400}

    if kind == NEEDS_A_REASON:
        if not _text(record.get("resolution_note")):
            return {"error": (
                f"Dispute {dispute_id} is being resolved as no_action with "
                f"no reason. That is a legitimate ending -- plenty of "
                f"claims are not ours to compensate -- but it needs a "
                f"sentence in resolution_note, because 'we are not paying "
                f"this' is a decision somebody will be asked to justify "
                f"weeks later, and a bare no_action is indistinguishable "
                f"from a queue somebody cleared to make the number go "
                f"down."), "status": 400}
        return None

    expected = RESOLUTION_COLLECTIONS[kind]
    ref = _text(record.get("resolution_ref"))
    if not ref:
        return {"error": (
            f"Dispute {dispute_id} is being resolved as {kind} with no "
            f"resolution_ref. A {kind} resolution has to name the record "
            f"that carries it -- '{expected}/{{id}}' -- because the "
            f"promise to the customer is that something exists, and this "
            f"field is the only place that can be checked."),
            "status": 400}

    named, record_id = _split_ref(ref)
    if not named or not record_id:
        return {"error": (
            f"resolution_ref {ref!r} on dispute {dispute_id} is not a "
            f"reference. It has to read '{expected}/{{id}}' so the record "
            f"behind it can actually be looked up; a bare id names a row "
            f"in no particular collection."), "status": 400}

    if named != expected:
        return {"error": (
            f"Dispute {dispute_id} says {kind} but points at "
            f"{named}/{record_id}. A {kind} resolution lives in "
            f"{expected}, and a pointer into the wrong collection is a "
            f"story about something that did not happen: refunds hold "
            f"money that went back, orders hold goods that will go out, "
            f"wallet_entries hold credit somebody can spend."),
            "status": 409}

    try:
        object_records.get_collection_record(expected, record_id,
                                             base_dir=base)
    except object_collections.CollectionNotFoundError:
        return {"error": (
            f"Dispute {dispute_id} cannot be resolved as {kind}: there is "
            f"no {expected} collection on this server, so "
            f"{ref} names nothing. A credit needs app-billing installed; "
            f"a refund needs app-payments. Resolve it a way this server "
            f"can actually carry out, or say no_action and why."),
            "status": 409}
    except object_records.RecordNotFoundError:
        return {"error": (
            f"resolution_ref {ref} on dispute {dispute_id} names no record "
            f"in {expected}. A dispute is only resolved when something "
            f"real happened, and a pointer at a row that does not exist "
            f"reads as done while nothing has: check the id, or resolve it "
            f"as no_action and say why."), "status": 409}
    except Exception:
        return {"error": (
            f"Dispute {dispute_id} names {ref} and {expected} could not be "
            f"read, so this resolution cannot be verified. Refusing rather "
            f"than closing a dispute against a record nobody can confirm "
            f"exists."), "status": 503}

    return None


def BEFORE_WRITE(request):
    action = _text(request.get("action"))
    if action not in ("create", "update"):
        return None
    record = request.get("record") or {}
    if _text(record.get("status")) != RESOLVED:
        # Every other status is somebody's work in progress and this hook
        # has no opinion about it. The gate is on the ENDING, because the
        # ending is the thing the customer is told about.
        return None
    return check_resolution(record)
