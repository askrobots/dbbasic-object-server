"""action_regenerate_portal_link -- mint a fresh customer-payment-portal
capability token for an invoice, invalidating whatever token was live
before (plan/customer-payment-portal-spec.md).

This is the ONLY place a client can cause invoices.portal_token to be
set. portal_token is schema read_only (never client-submitted directly,
same posture invoice_totals.py's docstring documents for stamped totals);
this object writes it with object_records.update_collection_record's
preserve_read_only=True, the "trusted, explicit, opt-in... never wired to
an HTTP request payload" escape hatch object_records.py itself reserves
for exactly this: a caller that owns the field's derivation, not a
pass-through of client input. The client controls WHETHER a new token is
minted (by calling this action at all); it never controls WHAT the token
is.

Doubles, today, as the only way a portal_token is minted for an invoice
the owner has not yet had dunned (system_invoice_aging mints one lazily
the first time a dunning email needs a working link, but that only fires
once an invoice is overdue). Wiring eager token issuance into the invoice
"send" flow itself (draft -> sent) is a natural next step, but that logic
lives in objects this task's edit boundary does not include (site_invoices
/ any hook on the invoices collection) -- flagged for whoever owns that
surface next, not solved here by reaching outside the boundary.

Regeneration is the correct response to a leaked link (a customer forwards
the email, a token ends up in a log, etc): the OLD token stops matching
anything the instant this writes, because site_invoice_portal's lookup is
a live scan against the current stored value, not a cached credential.
"""
from __future__ import annotations

import os
import secrets

import object_records

ACTOR = "action_regenerate_portal_link"
DATA_DIR_ENV = "DBBASIC_DATA_DIR"


def _base_dir() -> str:
    return os.environ.get(DATA_DIR_ENV, "data")


def POST(request):
    identity = request.get("_identity") or {}
    user_id = identity.get("user_id") or ""
    is_admin = "admin" in (identity.get("roles") or [])
    if not user_id:
        return {"status": 403, "error": "Sign in to regenerate a portal link."}

    invoice_id = str(request.get("invoice_id") or "").strip()
    if not invoice_id:
        return {"status": 400, "error": "invoice_id is required"}

    base = _base_dir()
    try:
        invoice = object_records.get_collection_record("invoices", invoice_id, base_dir=base)
    except Exception:
        return {"status": 404, "error": f"Invoice not found: {invoice_id}"}

    # Owner-or-admin, same shape as app-finance's reverse_journal and
    # app-banking's resolve_bank_line: the object enforces this itself
    # (execute is granted broadly to "registered"; ownership is not a
    # row_filter concern for an action, it is this check).
    if not is_admin and invoice.get("owner_id") != user_id:
        return {
            "status": 403,
            "error": "Only the invoice's owner (or an admin) may regenerate its portal link.",
        }

    token = secrets.token_urlsafe(32)
    object_records.update_collection_record(
        "invoices",
        invoice_id,
        {"portal_token": token},
        base_dir=base,
        actor=ACTOR,
        preserve_read_only=True,
    )
    return {"status": 200, "ok": True, "invoice_id": invoice_id, "path": f"/pay/{token}"}
