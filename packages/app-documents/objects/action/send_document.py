"""action_send_document -- render a document, freeze what it said, mint the
link, and QUEUE the mail. POST {kind, source, to}.

Four facts get created here and they are deliberately in this order:

  1. the render model              (object_documents, pure)
  2. the sent_documents row        (the snapshot + the token: the evidence)
  3. the outbox row                (the message)
  4. the link, returned to caller  (so a human can paste it anywhere)

The row before the message, always. If the mail write fails after the row
exists, the operator has a working link they can send by hand; if the message
went first and the row write failed, the customer would have a link to nothing.
One of those is recoverable and the other is a support ticket.

**It queues; it never sends.** enqueue() is a plain local write of one `queued`
row and the daemon owns delivery, retries and backoff -- the same posture
system_order_email argues at length. Nothing here opens a socket, so a slow
SMTP server cannot add its timeout to the click that sent a quote.

**Idempotent per (source, recipient), the way system_order_email is.** Two
levels, because there are two facts:

  * the DOCUMENT is keyed on (source, sent_to) in sent_documents. Sending the
    same quote to the same person twice is one document, one snapshot, one
    token, one opened_at -- not two links racing to be the one they clicked.
    Sending it to their accounts department as well is genuinely a second
    delivery, so the recipient is in the key.
  * the MESSAGE is keyed on a marker in the outbox row's source_object_id:

        action_send_document:{source}:{recipient}

    scanned before composing, exactly as system_order_email does, and for the
    identical reason: the outbox row IS the message, so the claim "this message
    exists" belongs on it rather than on a stamped flag somewhere else that can
    disagree with it.

Split that way, a retry after a half-finished send finishes the job instead of
duplicating it: the row is found and reused, the marker is missing, so the mail
is composed once.

**A nameless business is refused here, and only here (409).** object_documents
raises NamelessBusinessError when `business.name` is unset. The printable pages
swallow it -- an operator standing at a printer gets their paperwork, unbranded
but honest -- because refusing there costs somebody a parcel. Refusing HERE
costs one settings row and nobody is waiting, and an anonymous document in a
stranger's inbox cannot be paid, queried or filed.

**portal.base_url is also a 409, not a degrade.** system_order_email sends its
mail without links when no origin is configured, and that is right for a
confirmation whose body is worth reading on its own. This message's entire
payload IS the link. Sending it without one would be posting an empty envelope,
so the refusal names the setting instead.

What is deliberately NOT here: attaching a PDF. Print -> Save as PDF already
produces a good PDF and costs nothing, and server-side PDF is a capability with
an honest absent state (action_render_pdf). A send that silently produced no
attachment because no engine was installed would be the worst of both.
"""

import os
import secrets
from datetime import datetime, timezone

import object_documents
import object_email
import object_money
import object_records

ACTOR = "action_send_document"
COLLECTION = "sent_documents"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _settings(base):
    """Every app_settings row as one dict.

    Read whole rather than key by key: object_documents wants a mapping, the
    collection is tiny, and one scan beats six. Duplicated on purpose like
    every other package that reads app_settings (docs/logic-decisions.md #4).
    """
    values = {}
    try:
        for row in object_records.read_collection_records("app_settings",
                                                          base_dir=base):
            key, value = _text(row.get("key")), _text(row.get("value"))
            if key and value:
                values[key] = value
    except Exception:
        pass
    return values


def _split_source(source):
    """'orders/9f3...' -> ('orders', '9f3...'), or (None, None)."""
    source = _text(source)
    if source.count("/") != 1:
        return None, None
    collection, _, record_id = source.partition("/")
    collection, record_id = _text(collection), _text(record_id)
    if not collection or not record_id:
        return None, None
    return collection, record_id


def _lines_for(base, spec, record_id):
    """The document's line rows, or none at all.

    Wrapped whole: the lines collection belongs to another package, and a
    server that never installed line-item detail must still be able to send a
    document -- one without lines, honestly, rather than a traceback.
    """
    collection = spec.get("lines_collection")
    if not collection:
        return []
    try:
        rows = object_records.read_collection_records(collection, base_dir=base)
    except Exception:
        return []
    key = spec.get("lines_fk")
    lines = [row for row in rows if _text(row.get(key)) == record_id]
    lines.sort(key=lambda row: _text(row.get("description")))
    return lines


def _existing_document(base, source, to):
    """The document already sent for this (source, recipient), or None."""
    try:
        rows = object_records.read_collection_records(COLLECTION, base_dir=base)
    except Exception:
        return None
    for row in rows:
        if _text(row.get("source")) == source and _text(row.get("sent_to")) == to:
            return row
    return None


def _marker(source, to):
    return f"{ACTOR}:{source}:{to}"


def _already_queued(base, marker):
    """Whether this exact message is already in the outbox, or None when the
    outbox cannot be read at all.

    None is a REFUSAL, not a False -- system_order_email's argument holds
    here word for word: a message skipped now can be composed by a retry, and
    a message sent twice is already in somebody's inbox.
    """
    try:
        rows = object_records.read_collection_records(
            object_email.OUTBOX_COLLECTION, base_dir=base)
    except Exception:
        return None
    return any(_text(row.get("source_object_id")) == marker for row in rows)


def _email_body(model, link, settings):
    name = ""
    for party in model.get("parties") or []:
        if party.get("role") != "from":
            name = _text(party.get("name"))
    label = _text(model.get("kind_label")) or "document"
    number = _text(model.get("number"))
    business = _text((model.get("business") or {}).get("name"))
    lines = [f"Hello {name or 'there'},", "",
             f"Your {label.lower()}{(' ' + number) if number else ''} "
             f"from {business} is ready to read:", "", link, ""]
    terms = _text(model.get("terms"))
    if terms:
        lines += [terms, ""]
    lines += ["This link shows the document exactly as it was sent to you. "
              "You can print it, or print it and choose Save as PDF.", ""]
    return "\n".join(lines)


def POST(request):
    identity = request.get("_identity") or {}
    user_id = _text(identity.get("user_id"))
    if not user_id:
        return {"status": 403, "error": "Sign in to send a document."}

    kind = _text(request.get("kind"))
    source = _text(request.get("source"))
    to = _text(request.get("to"))
    if not kind or not source or not to:
        return {"status": 400,
                "error": "kind, source and to are all required",
                "example": {"kind": "quote", "source": "orders/{id}",
                            "to": "buyer@example.test"}}

    try:
        spec = object_documents.source_spec(kind)
    except object_documents.UnknownKindError as exc:
        return {"status": 400, "error": str(exc)}
    if spec is None:
        # Printable but not sendable: the kind has no source record to render
        # from yet. An honest state, said out loud, rather than a document
        # built out of guesses.
        return {"status": 409,
                "error": f"'{kind}' documents can be printed but not yet sent: "
                         "the layer has no source mapping for them.",
                "kind": kind}

    collection, record_id = _split_source(source)
    if collection is None:
        return {"status": 400,
                "error": f"source must look like 'collection/{{id}}', not {source!r}"}
    if collection != spec["collection"]:
        # A packing slip built from an invoice would print the wrong address on
        # the box. Caught here rather than discovered on a doorstep.
        return {"status": 400,
                "error": (f"a '{kind}' is rendered from {spec['collection']}, "
                          f"but source names {collection}")}

    base = _base_dir()
    try:
        record = object_records.get_collection_record(collection, record_id,
                                                      base_dir=base)
    except Exception:
        record = None
    if not record:
        return {"status": 404, "error": f"No such record: {source}"}

    if _text(record.get("owner_id")) and _text(record.get("owner_id")) != user_id:
        is_admin = "admin" in (identity.get("roles") or [])
        if not is_admin:
            return {"status": 403,
                    "error": "Only the owner (or an admin) may send this document."}

    settings = _settings(base)
    origin = _text(settings.get("portal.base_url")).rstrip("/")
    if not origin:
        return {"status": 409,
                "error": ("portal.base_url is not set, so there is no address "
                          "to put in the email. This message is nothing but a "
                          "link -- set portal.base_url in Settings first."),
                "setting": "portal.base_url"}

    currency = _text(record.get("currency")) or "USD"

    def money(cents):
        try:
            return object_money.format_amount(cents or 0, currency, base_dir=base)
        except Exception:
            return f"{cents or 0} (minor units)"

    facts = object_documents.facts_from_records(
        kind, record, _lines_for(base, spec, record_id), money=money)
    try:
        model = object_documents.build_model(kind, facts, settings)
    except object_documents.NamelessBusinessError as exc:
        return {"status": 409, "error": str(exc), "setting": "business.name"}
    except object_documents.DocumentError as exc:
        return {"status": 400, "error": str(exc)}

    marker = _marker(source, to)
    queued_already = _already_queued(base, marker)
    if queued_already is None:
        return {"status": 409,
                "error": "The email outbox could not be read, so nothing was "
                         "sent -- app-email may not be installed."}

    existing = _existing_document(base, source, to)
    if existing is not None:
        token = _text(existing.get("token"))
        row_id = _text(existing.get("id"))
    else:
        token = secrets.token_urlsafe(32)
        try:
            created = object_records.create_collection_record(
                COLLECTION,
                {"kind": kind, "source": source, "token": token,
                 "sent_to": to, "sent_at": _now(), "opened_at": "",
                 "content_hash": object_documents.content_hash(model),
                 "source_rev": object_records.compute_record_rev(record),
                 "snapshot": object_documents.snapshot_json(model),
                 "owner_id": user_id},
                base_dir=base, actor=ACTOR, preserve_read_only=True)
        except Exception as exc:
            return {"status": 500,
                    "error": f"Could not record the sent document: {exc}"}
        row_id = created["id"]

    link = f"{origin}/d/{token}"

    if queued_already:
        return {"status": "ok", "document_id": row_id, "link": link,
                "queued": False,
                "note": (f"already sent to {to}; this is the same link, and "
                         "the same document they were sent")}

    number = _text(model.get("number"))
    subject = (f"{model.get('kind_label')}"
               + (f" {number}" if number else "")
               + f" from {(model.get('business') or {}).get('name', '')}").strip()
    try:
        object_email.enqueue(to, subject, _email_body(model, link, settings),
                             base_dir=base, source_object_id=marker)
    except Exception as exc:
        # The document exists and the link works; only the mail failed. Say
        # which, so an operator sends it by hand instead of assuming silence.
        return {"status": "ok", "document_id": row_id, "link": link,
                "queued": False, "error": f"could not queue the email: {exc}"}

    return {"status": "ok", "document_id": row_id, "link": link,
            "queued": True, "sent_to": to, "kind": kind, "source": source}


# Kept for symmetry with the rest of the house: an operator poking an action by
# hand over HTTP uses POST, and nothing else calls this.
def GET(request):
    return {"status": 405,
            "error": "POST {kind, source, to} to send a document."}
