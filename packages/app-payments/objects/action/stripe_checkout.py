"""action_stripe_checkout -- a portal token becomes a Stripe Checkout URL.

POST {token} -- public, because the payer has no account here and never
will: possession of the unguessable portal token IS the authorization,
exactly as it is for viewing the invoice at /pay/{token}. The action
resolves the invoice BY TOKEN with a constant-time compare (never by id
-- no enumeration path), computes the open balance server-side (the
client never names an amount; a payer who could pick their own price is
not paying an invoice), and mints a Checkout Session whose metadata
carries the invoice id -- the thread webhook_stripe follows back when
Stripe reports the money.

Success and cancel URLs return the payer to the SAME portal page, which
will show the payment once the webhook lands and the reaction chain flips
the invoice -- the portal already renders live truth, so checkout needs
no landing page of its own.

Unconfigured Stripe is a 409 naming the two env vars, not a broken
button: the portal only shows Pay when this can succeed, and a link that
lies is worse than no link.
"""

import hmac
import os

import object_records
import object_stripe

ACTOR = "action_stripe_checkout"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _setting(base, key, default=""):
    try:
        for row in object_records.read_collection_records("app_settings", base_dir=base):
            if row.get("key") == key and str(row.get("value") or "").strip():
                return row["value"].strip()
    except Exception:
        pass
    return default


def _invoice_by_token(base, token):
    try:
        rows = object_records.read_collection_records("invoices", base_dir=base)
    except Exception:
        return None
    for row in rows:
        stored = str(row.get("portal_token") or "")
        if stored and hmac.compare_digest(stored, token):
            return row
    return None


def _open_balance_cents(base, invoice):
    """total minus received-minus-refunded, summed from the records
    themselves -- a gate-adjacent amount must never trust a derived
    caption it could recompute (docs/business-logic-patterns.md)."""
    def cents(value):
        try:
            return int(str(value or "0").strip() or 0)
        except ValueError:
            return 0

    total = cents(invoice.get("total_cents"))
    received = refunded = 0
    invoice_id = invoice.get("id")
    try:
        for p in object_records.read_collection_records("payments", base_dir=base):
            if p.get("invoice_id") == invoice_id and (p.get("status") or "received") == "received":
                received += cents(p.get("amount_cents"))
    except Exception:
        pass
    try:
        for r in object_records.read_collection_records("refunds", base_dir=base):
            if r.get("invoice_id") == invoice_id:
                refunded += cents(r.get("amount_cents"))
    except Exception:
        pass
    return total - (received - refunded)


def POST(request):
    config = object_stripe.stripe_config_from_env()
    if not config.configured:
        return {"status": 409,
                "error": ("Card payment is not configured on this server. Set "
                          "DBBASIC_STRIPE_SECRET_KEY and DBBASIC_STRIPE_WEBHOOK_SECRET, "
                          "or pay by the instructions on the invoice page.")}

    token = str(request.get("token") or "").strip()
    if not token:
        return {"status": 400, "error": "token is required"}

    base = _base_dir()
    invoice = _invoice_by_token(base, token)
    if invoice is None:
        # Same posture as the portal: not-found, never "bad token" -- do
        # not confirm that a token space exists.
        return {"status": 404, "error": "Not found"}
    if (invoice.get("status") or "") == "void":
        return {"status": 409, "error": "This invoice was cancelled."}

    balance = _open_balance_cents(base, invoice)
    if balance <= 0:
        return {"status": 409, "error": "This invoice is already paid in full."}

    base_url = _setting(base, "portal.base_url").rstrip("/")
    if not base_url:
        return {"status": 409,
                "error": ("Set app_settings portal.base_url first -- Stripe needs "
                          "absolute return URLs, and guessing a hostname would send "
                          "your customer somewhere you do not control.")}
    portal_url = f"{base_url}/pay/{token}"

    try:
        session = object_stripe.create_checkout_session(
            config,
            amount_cents=balance,
            currency=str(invoice.get("currency") or "usd").lower()[:3] or "usd",
            description=f"Invoice {invoice.get('number') or invoice.get('id')}",
            success_url=portal_url + "?paid=1",
            cancel_url=portal_url,
            metadata={"invoice_id": str(invoice.get("id"))},
        )
    except object_stripe.StripeError as exc:
        # exc messages are operator-safe by object_stripe's contract, but
        # the PAYER gets a generic line: Stripe's error taxonomy is not
        # their problem, and it can leak configuration detail.
        return {"status": 502,
                "error": "The payment provider refused to start a checkout. "
                         "Try again shortly, or pay by the instructions on the "
                         "invoice page.",
                "_operator_detail": str(exc)[:200]}

    url = str(session.get("url") or "")
    if not url.startswith("https://"):
        return {"status": 502, "error": "The payment provider returned no checkout URL."}
    return {"status": 200, "url": url, "amount_cents": balance}
