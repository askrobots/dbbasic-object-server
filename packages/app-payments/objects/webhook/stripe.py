"""webhook_stripe -- Stripe's word arrives, verified, and becomes a payment.

Served by the core raw-body endpoint (POST /webhooks/stripe): this object
receives the exact bytes on the wire plus the stripe-signature header,
verifies the HMAC (object_stripe -- constant-time, replay-windowed, fails
closed on anything malformed), and only then lets the event mean anything.

What it does with a verified event is deliberately tiny: **write the
payment record, nothing else.** checkout.session.completed with an
invoice_id in metadata becomes one payments row -- owner stamped from the
invoice, reference carrying the Stripe ids, deduped against the event id
so Stripe's at-least-once delivery (they redeliver for days on non-2xx)
can never double-record money. Everything downstream -- the invoice
flipping paid, the books journal, the receipt -- is the SAME reaction
chain any payment triggers. The predecessor's Stripe silo happened
because webhook code did bespoke things; here the webhook is just another
door into the one pipeline.

Storage-level writes do not reach the synchronous dispatcher, so this is
the third instance of docs/logic-decisions.md #9 -- and per that entry,
the third instance buys the real fix rather than a third workaround: the
change-dispatch daemon pass (DBBASIC_ENABLE_CHANGE_DISPATCH=true) replays
reactions from the change log. Deploying Stripe means enabling it; the
daemon's boot line says whether it is on.

Response discipline: 2xx for handled AND deliberately-ignored events
(a 4xx makes Stripe hammer retries for days); 400 only for signature
failures, where retrying can never help; nothing about the input is ever
reflected back.
"""

import json
import os
from datetime import datetime, timezone

import object_ids
import object_records
import object_stripe

ACTOR = "webhook_stripe"

HANDLED_EVENTS = {"checkout.session.completed"}


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _already_recorded(base, event_id):
    """Stripe delivers at-least-once for days; the event id is the dedup
    key, carried in the payment's reference field."""
    try:
        for row in object_records.read_collection_records("payments", base_dir=base):
            if event_id in str(row.get("reference") or ""):
                return True
    except Exception:
        return True  # cannot tell -> do not risk recording money twice
    return False


def POST(request):
    config = object_stripe.stripe_config_from_env()
    if not config.configured:
        # Unconfigured means this endpoint should not exist yet; give the
        # prober nothing to learn.
        return {"status": 404, "error": "Unknown webhook"}

    raw = request.get("_raw_body")
    headers = request.get("_headers") or {}
    signature = headers.get("stripe-signature") or headers.get("Stripe-Signature") or ""
    if not isinstance(raw, str) or not raw:
        return {"status": 400, "error": "Missing body"}

    verdict = object_stripe.verify_webhook_signature(
        raw.encode("utf-8"), signature, config.webhook_secret)
    if not verdict.get("ok"):
        # Retrying an invalid signature can never help, so 400 is honest --
        # and the reason goes to our log, not to the caller.
        return {"status": 400, "error": "Signature verification failed"}

    try:
        event = object_stripe.parse_event(raw.encode("utf-8"))
    except Exception:
        return {"status": 400, "error": "Unparseable event"}

    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")
    if not event_id:
        return {"status": 400, "error": "Event carries no id"}
    if event_type not in HANDLED_EVENTS:
        # Acknowledged and deliberately ignored: a 4xx here would make
        # Stripe redeliver an event we will never want, for days.
        return {"status": 200, "ok": True, "ignored": event_type}

    base = _base_dir()
    if _already_recorded(base, event_id):
        return {"status": 200, "ok": True, "duplicate": event_id}

    session = (event.get("data") or {}).get("object") or {}
    metadata = session.get("metadata") or {}
    invoice_id = str(metadata.get("invoice_id") or "").strip()
    amount_total = session.get("amount_total")
    if not invoice_id or not isinstance(amount_total, int) or amount_total <= 0:
        # A completed session this server did not mint (no invoice_id) is
        # acknowledged but recorded nowhere -- we only book what we asked
        # to be paid.
        return {"status": 200, "ok": True, "ignored": "no invoice metadata"}

    try:
        invoice = object_records.get_collection_record("invoices", invoice_id, base_dir=base)
    except Exception:
        return {"status": 200, "ok": True, "ignored": f"invoice gone: {invoice_id}"}

    created = event.get("created")
    received_on = (datetime.fromtimestamp(created, tz=timezone.utc).date().isoformat()
                   if isinstance(created, int) else
                   datetime.now(timezone.utc).date().isoformat())
    payment_id = object_ids.new_uuid4()
    try:
        object_records.create_collection_record(
            "payments",
            {
                "id": payment_id,
                "invoice_id": invoice_id,
                "amount_cents": str(amount_total),
                # "card", not "stripe": the enum names the INSTRUMENT the
                # customer used, which is what a bookkeeper reconciles by --
                # the provider's identity already rides in `reference` and
                # would be a second, redundant vocabulary here. The schema
                # caught this: an enum earning its keep.
                "method": "card",
                "received_on": received_on,
                # Both ids: the event id is the dedup key, the session id is
                # what a human pastes into the Stripe dashboard search.
                "reference": f"{event_id} {session.get('id', '')}".strip(),
                "notes": "Recorded from Stripe checkout webhook",
                "status": "received",
                "owner_id": invoice.get("owner_id", ""),
            },
            base_dir=base, actor=ACTOR)
    except Exception as exc:
        # A 5xx makes Stripe redeliver -- which is exactly right for a
        # transient write failure, and safe because of the dedup above.
        return {"status": 500, "error": f"Could not record payment: {str(exc)[:120]}"}

    return {"status": 200, "ok": True, "payment_id": payment_id,
            "invoice_id": invoice_id, "amount_cents": amount_total}
