"""system_invoice_portal_link -- give every issued invoice a pay link.

HANDLES invoices writes: the moment an invoice stops being a draft, mint
its portal_token if it has none. This closes the gap left by minting
lazily: a link that only appears once an invoice goes overdue is a link
that exists only for the customers who already failed to pay. The point of
the portal is that the FIRST email a customer receives can carry a door --
so the token has to exist when the invoice is issued, not when chasing
starts.

Placement is doctrine #6: this is a REACTION, post-commit and best-effort,
not a gate. It never blocks or fails the write that issued the invoice --
an invoice that could not get a token is still a perfectly good invoice,
and the next event (or a dunning pass, or the owner clicking regenerate)
will mint one.

portal_token is schema read_only so no client can ever choose its own --
a predictable capability URL is not a capability at all. Server-side
writers pass preserve_read_only to set it, which is exactly the narrow
escape hatch that flag exists for.
"""

import os
import secrets

import object_records

HANDLES = [
    "invoices.record.created",
    "invoices.record.updated",
]

ACTOR = "system_invoice_portal_link"

# draft invoices deliberately get no link: nothing has been issued yet, and
# a payable URL for a document the owner is still editing is a mistake
# waiting to be forwarded.
LINKABLE = {"sent", "partial", "overdue", "paid"}


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def EVENT(request):
    # The dispatcher's payload carries the RAW verb ("create"/"update");
    # the event NAME uses the participle. Accept both.
    action = str(request.get("action") or "")
    action = {"create": "created", "update": "updated", "delete": "deleted"}.get(action, action)
    if action not in ("created", "updated"):
        return {"ok": True, "skipped": "not a create or update"}
    record_id = str(request.get("record_id") or "")
    if not record_id:
        return {"ok": True, "skipped": "no record id"}

    base = _base_dir()
    try:
        invoice = object_records.get_collection_record("invoices", record_id, base_dir=base)
    except Exception:
        return {"ok": True, "skipped": "invoice gone"}

    if (invoice.get("status") or "draft") not in LINKABLE:
        return {"ok": True, "skipped": "not issued yet"}
    if str(invoice.get("portal_token") or "").strip():
        return {"ok": True, "skipped": "already has a link"}

    try:
        object_records.update_collection_record(
            "invoices", record_id,
            {"portal_token": secrets.token_urlsafe(32)},
            base_dir=base, actor=ACTOR, preserve_read_only=True)
    except Exception as exc:  # never break the dispatcher
        return {"ok": False, "error": str(exc)[:200]}
    return {"ok": True, "minted": True, "invoice_id": record_id}
