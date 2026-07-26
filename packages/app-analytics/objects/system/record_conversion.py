"""system_record_conversion -- the goals finally get written down.

`conversions` has existed as a collection for a while and NOTHING wrote to
it. The shape was right and the wiring was missing, which is a worse
failure than an absent feature: a page can render an empty table and read
as "nobody converted" when the truth is "nothing was ever recorded".

The implementation is small, because this system is full of transitions
and **a conversion is simply a transition worth counting**. Nothing new
had to be observed; four facts the server already knew just never landed
anywhere a report could see them:

  order_confirmed   an order reached confirmed (or anywhere past it)
  order_collected   a pickup order was actually handed over
  payment_received  money arrived against an invoice
  scan_confirmed    a human confirmed a scanned document into the books

Placement is the same posture as system_order_email, deliberately: a
REACTION, post-commit, best-effort, never a gate. It cannot fail the write
that confirmed the order. Every failure lands in the result rather than in
an exception, and a shop with no app-analytics installed keeps taking
orders and is told plainly that nothing was counted.

**Idempotent by provenance (#7).** The change dispatcher promises
at-least-once delivery, and worse: `status is confirmed` is a state an
order SITS in, not an edge it crosses, so every later write to a confirmed
order fires this again. Each conversion therefore carries its source in
the metadata blob --

    {"source": "orders/ord-1", ...}

-- and the handler refuses to write one whose (event_type, source) pair it
can already find. That is the property the tests care about most: a
replayed event must record nothing, because a double-counted conversion is
a report that says the shop did twice the business it did, and there is no
later signal that would ever correct it.

The marker lives in metadata rather than in a column of its own because
the schema is fixed and correct, and a private bookkeeping field for one
handler is a worse trade than a documented key in a blob that already
exists for exactly this kind of detail.

**No session token is stamped, and that is honest rather than lazy.**
`conversions.session_id` is the thread a funnel is stitched by, and these
four goals have no browser anywhere near them: an order confirmed by staff
the next morning is a back-office write with no cookie in scope, and the
basket's `carts.session_token` is a DIFFERENT identifier in a different
namespace -- treating it as the visitor token would silently merge two
populations and produce a funnel that looks stitched and is not. So these
rows are unthreaded, object_conversions.funnel counts them in
`unthreaded_conversions` and says so on the page, and the day an app
records a goal from a request that DID carry the visitor cookie, the
field is there waiting.

**`user_id` and `session_id` are never both written** --
object_conversions.build_conversion enforces it. See docs/analytics.md,
cookie rule 4.
"""

import os

import object_conversions
import object_records

HANDLES = [
    "orders.record.created",
    "orders.record.updated",
    "payments.record.created",
    "scans.record.updated",
]

ACTOR = "system_record_conversion"

# Anywhere at or past `confirmed` counts, not the exact value only -- the
# same wider net system_order_email takes, and for the same reason: the
# zero-touch shop confirms and ships in one burst of writes, so an order
# can be `shipped` by the time this handler first sees it. A sale that
# never counted because it moved too fast is exactly the undercount this
# exists to end. The marker makes the wider net safe.
CONFIRMED_ONWARD = {"confirmed", "processing", "partial", "shipped",
                    "delivered", "preparing", "ready", "collected"}

# The counter's own goal, and a genuinely different fact from the sale:
# the money was taken when the order was confirmed, and the food was
# handed over when somebody walked in for it. A shop that wants to know
# how many of its ready orders were actually collected cannot ask that of
# `order_confirmed`.
COLLECTED = "collected"

# A bounced payment is money that did not arrive. It stays a payments row
# (money moves, never mutates) and it is not a conversion.
BOUNCED = "bounced"

SCAN_CONFIRMED = "confirmed"

EVENT_ORDER_CONFIRMED = "order_confirmed"
EVENT_ORDER_COLLECTED = "order_collected"
EVENT_PAYMENT_RECEIVED = "payment_received"
EVENT_SCAN_CONFIRMED = "scan_confirmed"

# Sale orders only. A purchase order reaching `confirmed` is money going
# OUT; counting it as a conversion would report a shop's own buying as its
# business won.
SALE_DOC_TYPE = "sale"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _int(value):
    try:
        return int(str(value).strip() or 0)
    except (TypeError, ValueError):
        return 0


def _record(base, collection, record_id):
    if not record_id:
        return None
    try:
        return object_records.get_collection_record(collection, record_id,
                                                     base_dir=base)
    except Exception:
        return None


def _recorded(base):
    """Every (event_type, source) already counted, or None when the
    conversions collection cannot be read at all.

    None is a REFUSAL, not an empty set -- the same argument
    system_order_email's `_sent_markers` makes, and the asymmetry is the
    same. A conversion this pass skips will be recorded by the next event
    on the same record, and there is always a next event because the
    dispatcher replays. A conversion recorded twice is a permanent
    overcount in a report nothing will ever correct. When in doubt, count
    nothing, and say WHICH doubt -- "app-analytics is not installed" must
    never read as "already counted".
    """
    try:
        rows = object_records.read_collection_records(
            object_conversions.CONVERSIONS_COLLECTION, base_dir=base)
    except Exception:
        return None
    return object_conversions.recorded_sources(rows)


def _write(base, *, event_type, source, metadata, user_id=""):
    """One conversions row, or the reason there is not one."""
    record = object_conversions.build_conversion(
        event_type=event_type, source=source, metadata=metadata,
        user_id=user_id)
    try:
        object_records.create_collection_record(
            object_conversions.CONVERSIONS_COLLECTION, record,
            base_dir=base, actor=ACTOR)
    except Exception as exc:
        return str(exc)[:200]
    return ""


def _goals_for_order(order):
    """(event_type, metadata) pairs this order has earned.

    Money is carried in minor units exactly as the record holds them --
    no formatting, no currency conversion, no second opinion about what
    the order is worth. A report that reformats money is a report that can
    disagree with the document it describes.
    """
    status = _text(order.get("status"))
    number = _text(order.get("number")) or _text(order.get("id"))
    common = {
        "order_number": number,
        "amount_cents": _int(order.get("total_cents")),
        "currency": _text(order.get("currency")) or "USD",
    }
    goals = []
    if status in CONFIRMED_ONWARD:
        goals.append((EVENT_ORDER_CONFIRMED, dict(common)))
    if status == COLLECTED:
        goals.append((EVENT_ORDER_COLLECTED,
                      dict(common, fulfillment_method=_text(
                          order.get("fulfillment_method")))))
    return goals


def _on_order(base, order_id, recorded):
    order = _record(base, "orders", order_id)
    if order is None:
        return {"ok": True, "skipped": "order gone", "recorded": []}

    # doc_type is read with .get so an orders schema that predates the
    # sale/purchase split still counts its orders rather than none of them.
    doc_type = _text(order.get("doc_type"))
    if doc_type and doc_type != SALE_DOC_TYPE:
        return {"ok": True, "recorded": [],
                "skipped": f"{doc_type} order: not a conversion",
                "order_id": order_id}

    source = object_conversions.source_ref("orders", order_id)
    return _apply(base, source, _goals_for_order(order), recorded,
                  extra={"order_id": order_id})


def _on_payment(base, payment_id, recorded):
    payment = _record(base, "payments", payment_id)
    if payment is None:
        return {"ok": True, "skipped": "payment gone", "recorded": []}
    if _text(payment.get("status")) == BOUNCED:
        return {"ok": True, "recorded": [],
                "skipped": "bounced: the money did not arrive",
                "payment_id": payment_id}

    source = object_conversions.source_ref("payments", payment_id)
    metadata = {
        "amount_cents": _int(payment.get("amount_cents")),
        "invoice_id": _text(payment.get("invoice_id")),
        "method": _text(payment.get("method")),
    }
    return _apply(base, source, [(EVENT_PAYMENT_RECEIVED, metadata)],
                  recorded, extra={"payment_id": payment_id})


def _on_scan(base, scan_id, recorded):
    scan = _record(base, "scans", scan_id)
    if scan is None:
        return {"ok": True, "skipped": "scan gone", "recorded": []}
    if _text(scan.get("status")) != SCAN_CONFIRMED:
        return {"ok": True, "recorded": [],
                "skipped": "not confirmed yet", "scan_id": scan_id}

    source = object_conversions.source_ref("scans", scan_id)
    # What it BECAME, not what was in it. The extraction is a guess and
    # the image is evidence; neither belongs in an analytics row.
    metadata = {
        "kind": _text(scan.get("category_hint")),
        "became": _text(scan.get("confirmed_record")),
    }
    return _apply(base, source, [(EVENT_SCAN_CONFIRMED, metadata)],
                  recorded, extra={"scan_id": scan_id})


def _apply(base, source, goals, recorded, *, extra=None):
    written, already, errors = [], [], {}
    for event_type, metadata in goals:
        if (event_type, source) in recorded:
            already.append(event_type)
            continue
        error = _write(base, event_type=event_type, source=source,
                       metadata=metadata)
        if error:
            errors[event_type] = error
        else:
            written.append(event_type)
            # Within one invocation too: an order that is both confirmed
            # and collected writes two DIFFERENT event types, but a goal
            # must never be written twice by one pass either.
            recorded.add((event_type, source))
    result = {"ok": True, "recorded": written, "skipped_already_counted": already,
              "source": source}
    result.update(extra or {})
    if errors:
        result["errors"] = errors
    return result


def EVENT(request):
    # The dispatcher's payload carries the RAW verb ("create"/"update");
    # the event NAME uses the participle. Accept both.
    action = _text(request.get("action"))
    action = {"create": "created", "update": "updated",
              "delete": "deleted"}.get(action, action)
    if action not in ("created", "updated"):
        return {"ok": True, "skipped": "not a create or update", "recorded": []}

    record_id = _text(request.get("record_id") or request.get("id"))
    if not record_id:
        return {"ok": True, "skipped": "no record id", "recorded": []}

    collection = _text(request.get("collection"))
    if collection not in ("orders", "payments", "scans"):
        return {"ok": True, "recorded": [],
                "skipped": f"nothing to count about {collection or 'nothing'}"}

    base = _base_dir()
    recorded = _recorded(base)
    if recorded is None:
        # Honest degradation: app-analytics is not installed, or the
        # collection cannot be read. Nothing is counted and the reason
        # says which -- silence here would look exactly like "already
        # counted", and the shop would never find out it had no numbers.
        return {"ok": True, "recorded": [],
                "skipped": "conversions collection unreadable "
                           "(app-analytics not installed?); nothing counted"}

    if collection == "orders":
        return _on_order(base, record_id, recorded)
    if collection == "payments":
        return _on_payment(base, record_id, recorded)
    return _on_scan(base, record_id, recorded)


# EVENT is the verb the change dispatcher calls handlers with; POST stays
# as an alias so an operator can poke the handler by hand over HTTP. A
# handler shipped with only POST silently matched nothing in production
# once, and every handler in this house has carried both ever since.
POST = EVENT
