"""system_order_email -- the shop finally says something back.

Until this handler existed a customer paid and heard silence. Every piece
of the machinery was already here -- object_email.enqueue writes a row,
the daemon's process_email_outbox pass drains it -- and nothing in the
whole server ever composed a message to a buyer. This composes three, and
each one is sent by a TRANSITION rather than a button:

  order confirmed -- what they bought, the total, the tracking link, and
      the pay link if the bill is still open
  shipped         -- the carrier and tracking number if the parcel has
      them, and the tracking link
  refunded        -- how much went back, and which order it came off

A button would be a fourth thing somebody has to remember on a busy
Friday, and the whole complaint this slice answers is that the shop was
already forgetting. A transition cannot be forgotten: the fact that
causes the email IS the email's trigger.

**It queues; it never sends.** enqueue() is a plain local write of one
`queued` row. Nothing here opens a socket. A slow SMTP server would
otherwise add its timeout to the write that triggered it -- an order
confirmation would make taking the money slower, and a broken relay would
make it fail. The daemon owns delivery, retries and backoff, and it
already logs "outbound mail is queuing only" when no transport is
configured. So on a server with no SMTP at all, every message still lands
in the outbox, inspectable, and nothing errors: the shop's voice is
recorded even where it cannot yet be heard.

Placement is doctrine #6: a REACTION, post-commit, best-effort, never a
gate. It cannot fail the write that confirmed the order or recorded the
refund. Every failure lands in the result rather than an exception.

**Idempotency by provenance (#7), on the OUTBOX ROW.** The change
dispatcher promises at-least-once delivery, so this handler will see the
same order again -- and the confirmed email's trigger is `status is
confirmed`, a state the order SITS in, not an edge it crosses, so every
later write to a confirmed order re-fires it. Each message therefore
carries a marker in its outbox row's source_object_id:

    system_order_email:orders/{order_id}:confirmed
    system_order_email:orders/{order_id}:shipped
    system_order_email:orders/{order_id}:refunded/{refund_id}

and the handler refuses to compose one whose marker it can already find.

The marker lives on the outbox row rather than on a stamped field on the
order, and the choice is not arbitrary. The outbox row IS the message: the
claim being recorded is "this message exists", and putting that claim
anywhere else means two records that can disagree -- an order stamped
`confirmation_sent` whose message was never actually written is a customer
who will never be told, and nothing will ever notice. It also matches the
house pattern exactly (system_order_fulfillment stamps
`shipments/{id}:line/{id}` into stock_moves.reference and scans for it).
And it is the only option that gets refunds right: a second refund on one
order is a second fact that deserves a second email, so the refund's own
id is part of its marker, while an order-level `refund_email_sent` flag
would silence it. The honest cost: a compaction that discarded delivered
outbox rows would let a replay re-send. Nothing prunes the outbox today
(object_daemon.process_email_outbox only updates rows), and the day
something does, its retention window is the thing to argue about -- which
is a better place for the argument than a stamped boolean nobody can
audit.

**Degrading honestly.** No customer_email on the order: skipped, with the
reason in the result, never a crash and never a message sent to "". No
portal.base_url configured: the mail goes out with no links rather than
with guaranteed-404 ones, exactly as system_invoice_aging's dunning mail
already does. No shipments collection, no invoices collection: those are
other packages, app-orders depends on neither, and their absence costs a
line of detail rather than the message.

Plain text only. An HTML alternative is supported by the outbox
(html_body) and deliberately unused: these are four short paragraphs a
person reads, every mail client on earth renders text, and a templating
layer is the kind of thing that gets built once and then owns every
future change to a sentence.
"""

import os

import object_email
import object_money
import object_records

HANDLES = [
    "orders.record.created",
    "orders.record.updated",
    "refunds.record.created",
]

ACTOR = "system_order_email"

# The confirmation fires anywhere at or past `confirmed`, not only on the
# exact value. The zero-touch shop (app-shop's auto_fulfill) confirms and
# ships a paid order in one burst of writes, so an order can be `shipped`
# by the time this handler first sees it -- and a buyer who never got a
# confirmation because their order moved too fast is exactly the silence
# this slice exists to end. The marker makes the wider net safe.
CONFIRMED_ONWARD = {"confirmed", "processing", "partial", "shipped",
                    "delivered"}

# Same reasoning for the shipping note: `delivered` is downstream of
# `shipped`, and an order that jumped straight to it still needs the
# "it is on its way" mail somebody may be waiting for.
SHIPPED_ONWARD = {"shipped", "delivered"}

# An invoice in one of these still wants money, so the confirmation
# carries the pay door. `void` and `paid` do not.
UNPAID_INVOICE_STATUSES = {"draft", "sent", "partial", "overdue"}

KIND_CONFIRMED = "confirmed"
KIND_SHIPPED = "shipped"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _setting(base, key, default=""):
    """Duplicated on purpose, like every other package that reads
    app_settings -- see docs/logic-decisions.md #4."""
    try:
        for row in object_records.read_collection_records("app_settings",
                                                          base_dir=base):
            if row.get("key") == key and _text(row.get("value")):
                return _text(row["value"])
    except Exception:
        pass
    return default


def _marker(order_id, kind):
    return f"{ACTOR}:orders/{order_id}:{kind}"


def _sent_markers(base):
    """Every message this handler has already composed, or None when the
    outbox cannot be read at all.

    None is a REFUSAL, not an empty set. The two ways to be wrong here are
    not symmetric: a message this pass skips will be composed by the next
    event on the same record (and there is always a next event, because
    the dispatcher replays), whereas a message sent twice is already in
    somebody's inbox and cannot be taken back. When in doubt, stay quiet
    -- and say WHICH doubt, so "app-email is not installed" never
    masquerades as "already told them".

    Read once per invocation rather than once per message: the outbox is
    append-storage and grows, and scanning it three times to compose three
    mails is the kind of quiet waste that only shows up at volume.
    """
    try:
        rows = object_records.read_collection_records(
            object_email.OUTBOX_COLLECTION, base_dir=base)
    except Exception:
        return None
    return {_text(row.get("source_object_id")) for row in rows}


def _base_url(base):
    """The public origin this shop's links are built from, or "" when the
    operator has not configured one. Never a bare path: a relative URL in
    an email is a link that does nothing in every mail client there is, so
    an unconfigured deployment sends the text without the link rather than
    with a broken one.
    """
    return _setting(base, "portal.base_url").rstrip("/")


def _track_link(base, order):
    token = _text(order.get("portal_token"))
    origin = _base_url(base)
    if not token or not origin:
        return ""
    return f"{origin}/orders/track/{token}"


def _pay_link(base, invoice):
    if invoice is None:
        return ""
    token = _text(invoice.get("portal_token"))
    origin = _base_url(base)
    if not token or not origin:
        return ""
    if _text(invoice.get("status")) not in UNPAID_INVOICE_STATUSES:
        return ""
    return f"{origin}/pay/{token}"


def _order(base, order_id):
    if not order_id:
        return None
    try:
        return object_records.get_collection_record("orders", order_id,
                                                    base_dir=base)
    except Exception:
        return None


def _invoice_for(base, order):
    invoice_id = _text(order.get("invoice_id"))
    if not invoice_id:
        return None
    try:
        return object_records.get_collection_record("invoices", invoice_id,
                                                    base_dir=base)
    except Exception:
        return None


def _lines_for(base, order_id):
    try:
        rows = object_records.read_collection_records("order_lines",
                                                      base_dir=base)
    except Exception:
        return []
    lines = [row for row in rows if _text(row.get("order_id")) == order_id]
    lines.sort(key=lambda row: _text(row.get("description")))
    return lines


def _money(base, cents, currency):
    """Whole currency units, never raw minor units. "1200" in a customer's
    email is a bill for twelve hundred of something -- and the one place a
    formatting failure must not become a traceback is the message that
    tells somebody their money moved."""
    try:
        return object_money.format_amount(cents or 0, currency or "USD",
                                          base_dir=base)
    except Exception:
        return f"{cents or 0} (minor units)"


def _shipment_with_tracking(base, order_id):
    """The outbound parcel worth naming: the one that actually carries a
    carrier or a tracking number. Returns None when shipping is not
    installed, when nothing has been dispatched, or when the parcel simply
    has no tracking -- all three of which are "say it shipped, say no more"
    rather than an error.
    """
    try:
        rows = object_records.read_collection_records("shipments",
                                                      base_dir=base)
    except Exception:
        return None
    best = None
    for row in rows:
        if _text(row.get("order_id")) != order_id:
            continue
        if _text(row.get("direction")) == "inbound":
            continue
        if _text(row.get("carrier")) or _text(row.get("tracking_number")):
            best = row
    return best


def _queue(base, *, to, subject, body, marker):
    """One outbox row, or a reason it could not be written. Wrapped whole:
    the outbox belongs to another package (app-email) and a shop that
    never installed it must still be able to take orders."""
    try:
        object_email.enqueue(to, subject, body, base_dir=base,
                             source_object_id=marker)
    except Exception as exc:
        return str(exc)[:200]
    return ""


# --- the three messages ------------------------------------------------------

def _confirmation_body(base, order, lines, invoice):
    currency = _text(order.get("currency")) or "USD"
    number = _text(order.get("number")) or order["id"]
    parts = [f"Hello {_text(order.get('customer_name')) or 'there'},", "",
             f"Thank you -- we have your order {number}.", ""]
    for line in lines:
        description = (_text(line.get("description"))
                       or _text(line.get("product_id")) or "Item")
        quantity = _text(line.get("quantity")) or "1"
        amount = _money(base, line.get("line_total_cents"), currency)
        parts.append(f"  {quantity} x {description} -- {amount}")
    if lines:
        parts.append("")
    parts.append(f"Total: {_money(base, order.get('total_cents'), currency)}")

    track = _track_link(base, order)
    if track:
        parts += ["", f"Follow your order here: {track}"]
    pay = _pay_link(base, invoice)
    if pay:
        # Only when money is genuinely still owed -- see _pay_link. A "pay
        # now" line on a paid order is how a shop gets paid twice.
        parts += ["", f"There is still a balance to settle: {pay}"]
    parts += ["", "We will email you again when it ships.", ""]
    return "\n".join(parts)


def _shipped_body(base, order, shipment):
    number = _text(order.get("number")) or order["id"]
    parts = [f"Hello {_text(order.get('customer_name')) or 'there'},", "",
             f"Your order {number} is on its way.", ""]
    if shipment is not None:
        carrier = _text(shipment.get("carrier"))
        tracking = _text(shipment.get("tracking_number"))
        if carrier:
            parts.append(f"Carrier: {carrier}")
        if tracking:
            parts.append(f"Tracking number: {tracking}")
        if carrier or tracking:
            parts.append("")
    track = _track_link(base, order)
    if track:
        parts += [f"Track it here: {track}", ""]
    return "\n".join(parts)


def _refund_body(base, order, refund):
    number = _text(order.get("number")) or order["id"]
    currency = _text(order.get("currency")) or "USD"
    amount = _money(base, refund.get("amount_cents"), currency)
    parts = [f"Hello {_text(order.get('customer_name')) or 'there'},", "",
             f"We have refunded {amount} against your order {number}.", ""]
    reason = _text(refund.get("reason"))
    if reason:
        parts += [f"Reason: {reason}", ""]
    parts += ["Depending on your bank, it can take a few days to appear on "
              "your statement.", ""]
    track = _track_link(base, order)
    if track:
        parts += [f"Your order: {track}", ""]
    return "\n".join(parts)


# --- triggers ----------------------------------------------------------------

def _on_order(base, order_id):
    order = _order(base, order_id)
    if order is None:
        return {"ok": True, "skipped": "order gone"}

    to = _text(order.get("customer_email"))
    if not to:
        # A guest checkout always types one; an order raised by hand in the
        # back office may not have. Say so plainly -- an operator reading
        # "no customer_email" knows exactly what to add, whereas silence
        # looks like a working mailer.
        return {"ok": True, "skipped": "no customer_email on the order",
                "order_id": order_id}

    sent = _sent_markers(base)
    if sent is None:
        return {"ok": True, "skipped": "outbox unreadable; nothing composed",
                "order_id": order_id}

    status = _text(order.get("status"))
    number = _text(order.get("number")) or order_id
    queued, skipped, errors = [], [], {}

    if status in CONFIRMED_ONWARD:
        marker = _marker(order_id, KIND_CONFIRMED)
        if marker in sent:
            skipped.append(KIND_CONFIRMED)
        else:
            error = _queue(
                base, to=to,
                subject=f"Your order {number} is confirmed",
                body=_confirmation_body(base, order, _lines_for(base, order_id),
                                        _invoice_for(base, order)),
                marker=marker)
            (queued.append(KIND_CONFIRMED) if not error
             else errors.setdefault(KIND_CONFIRMED, error))

    if status in SHIPPED_ONWARD:
        marker = _marker(order_id, KIND_SHIPPED)
        if marker in sent:
            skipped.append(KIND_SHIPPED)
        else:
            error = _queue(
                base, to=to,
                subject=f"Your order {number} has shipped",
                body=_shipped_body(base, order,
                                   _shipment_with_tracking(base, order_id)),
                marker=marker)
            (queued.append(KIND_SHIPPED) if not error
             else errors.setdefault(KIND_SHIPPED, error))

    result = {"ok": True, "order_id": order_id, "queued": queued,
              "skipped_already_sent": skipped}
    if errors:
        result["errors"] = errors
    return result


def _order_for_refund(base, refund):
    """Walk a refund back to the order it came off.

    refunds.invoice_id is stamped by app-payments' hook_refunds from the
    payment, and app-shop's checkout stamps invoices.source_order_id, so
    the chain is refund -> invoice -> order. The fallback scan on
    orders.invoice_id catches an invoice raised before source_order_id
    existed, or one converted from an order the other way round -- a
    refund whose order cannot be found is a message not sent, never a
    crash.
    """
    invoice_id = _text(refund.get("invoice_id"))
    if not invoice_id:
        return None
    try:
        invoice = object_records.get_collection_record("invoices", invoice_id,
                                                       base_dir=base)
    except Exception:
        invoice = None
    if invoice is not None and _text(invoice.get("source_order_id")):
        order = _order(base, _text(invoice.get("source_order_id")))
        if order is not None:
            return order
    try:
        rows = object_records.read_collection_records("orders", base_dir=base)
    except Exception:
        return None
    for row in rows:
        if _text(row.get("invoice_id")) == invoice_id:
            return row
    return None


def _on_refund(base, refund_id):
    try:
        refund = object_records.get_collection_record("refunds", refund_id,
                                                      base_dir=base)
    except Exception:
        return {"ok": True, "skipped": "refund gone"}

    order = _order_for_refund(base, refund)
    if order is None:
        return {"ok": True, "skipped": "no order behind this refund",
                "refund_id": refund_id}

    to = _text(order.get("customer_email"))
    if not to:
        return {"ok": True, "skipped": "no customer_email on the order",
                "refund_id": refund_id, "order_id": order["id"]}

    # The refund's own id is in the marker: two refunds against one order
    # are two separate facts, and a customer told about the first but not
    # the second is a support ticket.
    marker = _marker(order["id"], f"refunded/{refund_id}")
    sent = _sent_markers(base)
    if sent is None:
        return {"ok": True, "skipped": "outbox unreadable; nothing composed",
                "order_id": order["id"], "refund_id": refund_id}
    if marker in sent:
        return {"ok": True, "order_id": order["id"], "refund_id": refund_id,
                "queued": [], "skipped_already_sent": ["refunded"]}

    number = _text(order.get("number")) or order["id"]
    error = _queue(base, to=to,
                   subject=f"A refund on your order {number}",
                   body=_refund_body(base, order, refund),
                   marker=marker)
    result = {"ok": True, "order_id": order["id"], "refund_id": refund_id,
              "queued": [] if error else ["refunded"],
              "skipped_already_sent": []}
    if error:
        result["errors"] = {"refunded": error}
    return result


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

    collection = _text(request.get("collection"))
    if not collection:
        # An operator poking this by hand over HTTP: infer from what they
        # named rather than refusing on a technicality.
        collection = "refunds" if _text(request.get("refund_id")) else "orders"

    base = _base_dir()
    if collection == "refunds":
        return _on_refund(base, record_id)
    if collection == "orders":
        return _on_order(base, record_id)
    return {"ok": True, "skipped": f"nothing to say about {collection}"}


# EVENT is the verb the change dispatcher calls handlers with (see
# object_change_dispatch); POST stays as an alias so an operator can poke
# the handler by hand over HTTP. The alias is not decoration -- a handler
# shipped with only POST silently matched nothing in production once, and
# every handler in this house has carried both ever since.
POST = EVENT
