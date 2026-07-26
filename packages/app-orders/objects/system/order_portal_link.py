"""system_order_portal_link -- give every real order a door the customer
can walk through.

HANDLES orders writes: the moment an order stops being a draft, mint its
portal_token if it has none. This shop sells to guests by default -- a
buyer types an email address at checkout and never creates an account --
so there is no identity to sign in with and therefore nothing to
authenticate a "where is my order?" page against. The token IS the
identity, scoped to exactly one order.

The argument is the same one system_invoice_portal_link makes for the pay
link, and it is worth restating because it is the whole reason this is a
handler and not a button: the FIRST email a customer receives has to
carry the door. A token minted lazily -- when somebody complains, when a
nightly pass gets round to it -- is a token that exists only for the
orders that already went wrong. So it is minted when the order becomes
real, not when the trouble starts.

Draft orders deliberately get nothing. A draft is a basket the shop has
written down, not a commitment; a tracking URL for something nobody has
paid for is a link waiting to be forwarded to somebody who will find an
order that later vanished.

Placement is doctrine #6: this is a REACTION, post-commit and
best-effort, never a gate. It cannot fail the write that confirmed the
order -- an order that could not get a token is still a perfectly good
order, and the next event on it mints one. Every failure lands in the
result, never in an exception, because the dispatcher's job is to carry
on.

**It never overwrites.** A token that already exists is left exactly
alone -- skipped, not rotated. Two reasons, and both are real: the change
dispatcher promises at-least-once delivery (object_change_dispatch.py),
so this handler WILL see the same order again; and action_checkout mints
the token synchronously so the checkout response can carry the link,
which means by the time this handler first runs on a web order the token
is usually already there and is already in a customer's inbox. Rotating
it here would break a link the shop had itself just sent.

portal_token is schema read_only so no client can ever choose its own --
a predictable capability URL is not a capability at all. Server-side
writers pass preserve_read_only to set it, which is exactly the narrow
escape hatch that flag exists for.
"""

import os
import secrets

import object_records

HANDLES = [
    "orders.record.created",
    "orders.record.updated",
]

ACTOR = "system_order_portal_link"

# Everything except draft. Cancelled orders are included on purpose: a
# customer whose order was cancelled is precisely the customer most likely
# to follow the link they were sent, and the tracking page has honest
# words for that state ("Cancelled") -- a dead link instead would read as
# "they deleted my order and hid it".
UNLINKABLE = {"draft"}


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def EVENT(request):
    # The dispatcher's payload carries the RAW verb ("create"/"update");
    # the event NAME uses the participle. Accept both.
    action = _text(request.get("action"))
    action = {"create": "created", "update": "updated",
              "delete": "deleted"}.get(action, action)
    if action not in ("created", "updated"):
        return {"ok": True, "skipped": "not a create or update"}

    record_id = _text(request.get("record_id") or request.get("id"))
    if not record_id:
        return {"ok": True, "skipped": "no record id"}

    base = _base_dir()
    try:
        order = object_records.get_collection_record("orders", record_id,
                                                     base_dir=base)
    except Exception:
        return {"ok": True, "skipped": "order gone"}

    status = _text(order.get("status")) or "draft"
    if status in UNLINKABLE:
        return {"ok": True, "skipped": "still a draft"}
    if _text(order.get("portal_token")):
        # Not an error and not a rotation -- see the module docstring. A
        # replayed event must be a no-op, and the link may already be in
        # somebody's inbox.
        return {"ok": True, "skipped": "already has a link",
                "order_id": record_id}

    try:
        object_records.update_collection_record(
            "orders", record_id,
            {"portal_token": secrets.token_urlsafe(32)},
            base_dir=base, actor=ACTOR, preserve_read_only=True)
    except Exception as exc:  # never break the dispatcher
        return {"ok": False, "error": str(exc)[:200], "order_id": record_id}
    return {"ok": True, "minted": True, "order_id": record_id}


# EVENT is the verb the change dispatcher calls handlers with (see
# object_change_dispatch); POST stays as an alias so an operator can poke
# the handler by hand over HTTP. The alias is not decoration -- a handler
# shipped with only POST silently matched nothing in production once, and
# every handler in this house has carried both ever since.
POST = EVENT
