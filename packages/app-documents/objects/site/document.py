"""site_document -- the link a document was delivered as. GET /d/{token}.

THE RULE THIS PAGE EXISTS TO ENFORCE:

    A document that was SENT is evidence of what was sent.

This page renders the SNAPSHOT stored on the sent_documents row, not the live
record. Email a quote at 4,000, change the price to 4,500, and the customer's
link must not silently show 4,500 -- they may be holding a printout of the
first one. Silently restating a document somebody was sent is the same class of
error as restating an approved rate or a rolled-up invoice total
(docs/logic-decisions.md #1 and #8), and it is worse here than in the ledger,
because the counterparty has a copy and we do not know it.

A changed source is not hidden either -- hiding it would be the other half of
the same lie. When the live record no longer matches the fingerprint taken at
send time, the page keeps serving the snapshot and adds a banner naming BOTH
dates (sent on X; changed on Y) with a link to the current version at
/d/{token}?v=current. The reader chooses which one they are looking at, and the
page always says which one it is showing.

Change detection compares object_records.compute_record_rev of the live record
against the source_rev stamped when the document was sent. A content
fingerprint rather than a timestamp: a record edited in the same second it was
sent would slip past a timestamp comparison, and that is exactly the case where
the two documents differ. When the source cannot be read at all (the row was
deleted, the package removed), the answer is "cannot tell" and NO banner is
shown -- inventing "it changed" would be as wrong as inventing "it did not".

ACCESS, in the posture site_invoice_portal established and this reuses
verbatim: the token is the whole credential, looked up BY TOKEN ONLY and never
by id, matched with hmac.compare_digest (a plain `==` on attacker-controlled
input leaks how many leading characters matched, via how long it took -- the
exact side channel a bearer token exists to close). A blank token matches
nothing, so a row whose token was never minted is not an accidental open door.
An unknown token gets the SAME "not found" page, at 404, as a document that
never existed -- never 403, because a 403 confirms the token namespace is worth
attacking, while "not found" makes a guess and a typo indistinguishable.

No nav, no global search, no chrome beyond the shared stylesheet: somebody
following an emailed link must never be one click from another customer's
paperwork or from a sign-in wall for a system they have no account on.

opened_at is stamped on first view and never restamped -- "did they read it?"
is the first thing anybody asks after sending a quote, and it comes free with a
tokened link. Idempotent by construction (written only when the field is
empty), and best-effort: no read receipt is worth breaking the page somebody is
actively trying to read.

The PDF button is drawn only when a PDF engine is actually configured
(object_documents.pdf_engine_status), which today is never. Print -> Save as
PDF already works and the page says so instead.
"""

import hmac
import html
import os
from datetime import datetime, timezone

import object_documents
import object_money
import object_record_changes
import object_records

ACTOR = "site_document"
COLLECTION = "sent_documents"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _settings(base):
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


def _find_by_token(base, token):
    """The only lookup this page performs: a full scan matched in constant
    time, never a keyed lookup by id. A blank token matches nothing."""
    token = _text(token)
    if not token:
        return None
    try:
        rows = object_records.read_collection_records(COLLECTION, base_dir=base)
    except Exception:
        return None
    for row in rows:
        candidate = _text(row.get("token"))
        if not candidate:
            continue
        if hmac.compare_digest(candidate, token):
            return row
    return None


def _stamp_opened(base, row):
    """Once, ever. Written only when opened_at is empty, so a reader who
    refreshes twenty times still opened it once -- and every failure is
    swallowed, because a read receipt is not worth a broken page."""
    if _text(row.get("opened_at")):
        return
    try:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        object_records.update_collection_record(
            COLLECTION, row["id"], {"opened_at": now},
            base_dir=base, actor=ACTOR)
    except Exception:
        pass


def _source_record(base, source):
    """The live record behind a sent document, or None when it cannot be
    read at all -- which is 'cannot tell', not 'unchanged'."""
    source = _text(source)
    if source.count("/") != 1:
        return None
    collection, _, record_id = source.partition("/")
    if not _text(collection) or not _text(record_id):
        return None
    try:
        record = object_records.get_collection_record(_text(collection),
                                                      _text(record_id),
                                                      base_dir=base)
    except Exception:
        return None
    return record or None


def _changed_on(base, source):
    """The day the source last changed, from the record change log. Best
    effort: the banner is better with a date and still true without one."""
    source = _text(source)
    if source.count("/") != 1:
        return ""
    collection, _, record_id = source.partition("/")
    try:
        page = object_record_changes.list_record_changes(
            _text(collection), record_id=_text(record_id), base_dir=base,
            limit=1, tail_only=True)
    except Exception:
        return ""
    changes = page.get("changes") or []
    if not changes:
        return ""
    return _text(changes[0].get("timestamp"))[:10]


def _live_model(base, row, settings):
    """Re-render the CURRENT version of the document from the live record.

    Only reached from ?v=current -- an explicit request for the other thing.
    Uses the same pure fold the snapshot was built with, so "current" and
    "sent" cannot be two renderers that drift.
    """
    kind = _text(row.get("kind"))
    try:
        spec = object_documents.source_spec(kind)
    except object_documents.UnknownKindError:
        return None
    if spec is None:
        return None
    record = _source_record(base, row.get("source"))
    if record is None:
        return None

    record_id = _text(row.get("source")).partition("/")[2]
    lines = []
    if spec.get("lines_collection"):
        try:
            rows = object_records.read_collection_records(
                spec["lines_collection"], base_dir=base)
            lines = [line for line in rows
                     if _text(line.get(spec["lines_fk"])) == record_id]
            lines.sort(key=lambda line: _text(line.get("description")))
        except Exception:
            lines = []

    currency = _text(record.get("currency")) or "USD"

    def money(cents):
        try:
            return object_money.format_amount(cents or 0, currency, base_dir=base)
        except Exception:
            return f"{cents or 0} (minor units)"

    facts = object_documents.facts_from_records(kind, record, lines, money=money)
    try:
        return object_documents.build_model(kind, facts, settings,
                                            require_business=False)
    except object_documents.DocumentError:
        return None


def _esc(value):
    return html.escape(_text(value))


def _not_found():
    return {
        "status": 404,
        "content_type": "text/html; charset=utf-8",
        "body": """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Not found</title>
<link rel="stylesheet" href="/style">
</head>
<body>
<div style="max-width:34rem;margin:0 auto;padding:3rem 1.25rem;text-align:center">
<h1>Not found</h1>
<p>This document link is not valid. It may have been mistyped, or the business
that sent it may have issued a newer one. Contact them for a fresh link.</p>
</div>
</body>
</html>""",
    }


def _changed_banner(token, sent_on, changed_on):
    """Both dates, named, with the way to the other version.

    Not a warning colour and not an apology: documents change, and the only
    unacceptable outcome is a reader who cannot tell which one they have.
    """
    when = (f" on {_esc(changed_on)}" if changed_on else " since")
    return (
        '<div class="doc-banner"><strong>You are reading the version that was '
        f'sent to you on {_esc(sent_on)}.</strong>'
        f'This document was changed{when}. '
        f'<a href="/d/{_esc(token)}?v=current">See the current version</a>.'
        "</div>")


def _current_banner(token, sent_on, changed_on):
    when = (f" on {_esc(changed_on)}" if changed_on else "")
    return (
        '<div class="doc-banner"><strong>This is the CURRENT version of this '
        f'document, changed{when}.</strong>'
        f'The version sent to you on {_esc(sent_on)} is '
        f'<a href="/d/{_esc(token)}">still here</a>, unchanged.'
        "</div>")


def GET(request):
    base = _base_dir()
    row = _find_by_token(base, request.get("token"))
    if row is None:
        return _not_found()

    settings = _settings(base)
    _stamp_opened(base, row)

    try:
        snapshot = object_documents.model_from_snapshot(row.get("snapshot"))
    except object_documents.DocumentError:
        # The snapshot is the document. A half-parsed one is not evidence of
        # anything, and rendering something else under this link is exactly
        # what this page exists to prevent.
        return _not_found()

    token = _text(row.get("token"))
    sent_on = _text(row.get("sent_at"))[:10]

    # Has the source moved since we froze it? An exact question, asked of the
    # platform's own record fingerprint. Blank on either side means "cannot
    # tell", which shows no banner rather than a false one.
    stored_rev = _text(row.get("source_rev"))
    live = _source_record(base, row.get("source")) if stored_rev else None
    changed = False
    if live is not None:
        try:
            changed = object_records.compute_record_rev(live) != stored_rev
        except Exception:
            changed = False
    changed_on = _changed_on(base, row.get("source")) if changed else ""

    pdf = object_documents.pdf_engine_status(
        settings.get(object_documents.PDF_ENGINE_SETTING))
    size = settings.get(object_documents.PAGE_SIZE_SETTING)

    want_current = _text(request.get("v")).lower() == "current"
    if want_current and changed:
        model = _live_model(base, row, settings)
        if model is not None:
            return object_documents.render_page(
                model, banner=_current_banner(token, sent_on, changed_on),
                chrome=False, pdf=pdf["available"], size=size)
        # Could not re-render the live record -- fall through to the snapshot
        # rather than showing an error page to somebody who asked a reasonable
        # question. The banner below still says the document changed.

    banner = _changed_banner(token, sent_on, changed_on) if changed else ""
    return object_documents.render_page(
        snapshot, banner=banner, chrome=False, pdf=pdf["available"], size=size)
