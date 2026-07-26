"""action_render_pdf -- a CAPABILITY, absent by default. POST {token} or
{source, kind}.

**Browser print-to-PDF already produces a good PDF and costs nothing.** That is
the default answer, it is said out loud on every document page rendered by this
layer, and it is the reason this object is allowed to be absent: nobody is
blocked by it.

Server-side PDF is a real dependency decision and does not belong in the core:

    WeasyPrint   pango/cairo system libs -- heavy on a 1GB box
    Chrome       heavier still: a whole browser to render a page we already
                 rendered
    wkhtmltopdf  an unmaintained binary
    ReportLab    pure Python, but you rebuild the layout by hand, which is two
                 renderers again -- the exact failure this layer exists to end

So this follows the pattern already used for OCR engines
(docs/capability-objects.md) and carrier labels (app-shipping's action_buy_label
and connectors/manual.py): the capability is named in a SETTING,
`documents.pdf_engine`, defaulting to `none`, and an unconfigured server gets a
409 that NAMES WHAT TO INSTALL rather than a traceback, a silent nothing, or a
stub PDF with a watermark on it.

**No surface offers a PDF button when it would 409.** object_documents.
pdf_engine_status is the single pure answer to "is there an engine?", every
document page consults it before drawing the button, and render_page(pdf=...)
is the only way a button gets drawn at all. A button that 409s tells the person
pressing it that something is broken on YOUR end -- the same reason
site_invoice_portal only draws "Pay by card" when Stripe is actually
configured.

**There is deliberately no WeasyPrint or Chrome adapter in this file.** Nothing
in this repo can test one; an untested integration against a heavyweight
dependency is the worst kind of confident wrong answer; and the honest boundary
is more useful than a guess at the far side of it. What an engine must provide
when one is installed is exactly this: take `html` -- the SAME HTML the print
view renders, from object_documents.render_page, never a second template -- and
return PDF bytes. That is the whole contract, and it is why this object still
builds the HTML and returns it in the refusal: the boundary is demonstrably the
same document, not a promise that it would be.
"""

import hmac
import os

import object_documents
import object_money
import object_records

ACTOR = "action_render_pdf"
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


def _document_by_token(base, token):
    token = _text(token)
    if not token:
        return None
    try:
        rows = object_records.read_collection_records(COLLECTION, base_dir=base)
    except Exception:
        return None
    for row in rows:
        candidate = _text(row.get("token"))
        if candidate and hmac.compare_digest(candidate, token):
            return row
    return None


def _model_from_source(base, kind, source, settings):
    """Render a document straight from its source record -- the operator path,
    for a document that has not been sent to anybody yet."""
    try:
        spec = object_documents.source_spec(kind)
    except object_documents.UnknownKindError as exc:
        return None, {"status": 400, "error": str(exc)}
    if spec is None:
        return None, {"status": 409,
                      "error": f"'{kind}' documents have no source mapping yet."}
    if source.count("/") != 1:
        return None, {"status": 400,
                      "error": "source must look like 'collection/{id}'"}
    collection, _, record_id = source.partition("/")
    collection, record_id = _text(collection), _text(record_id)
    if collection != spec["collection"]:
        return None, {"status": 400,
                      "error": (f"a '{kind}' is rendered from "
                                f"{spec['collection']}, not {collection}")}
    try:
        record = object_records.get_collection_record(collection, record_id,
                                                      base_dir=base)
    except Exception:
        record = None
    if not record:
        return None, {"status": 404, "error": f"No such record: {source}"}

    lines = []
    if spec.get("lines_collection"):
        try:
            rows = object_records.read_collection_records(
                spec["lines_collection"], base_dir=base)
            lines = [row for row in rows
                     if _text(row.get(spec["lines_fk"])) == record_id]
            lines.sort(key=lambda row: _text(row.get("description")))
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
        model = object_documents.build_model(kind, facts, settings,
                                             require_business=False)
    except object_documents.DocumentError as exc:
        return None, {"status": 400, "error": str(exc)}
    return model, None


def POST(request):
    identity = request.get("_identity") or {}
    if not _text(identity.get("user_id")):
        return {"status": 403, "error": "Sign in to render a PDF."}

    base = _base_dir()
    settings = _settings(base)
    engine = object_documents.pdf_engine_status(
        settings.get(object_documents.PDF_ENGINE_SETTING))

    token = _text(request.get("token"))
    source = _text(request.get("source"))
    kind = _text(request.get("kind"))
    if not token and not source:
        return {"status": 400,
                "error": "pass either {token} for a sent document, or "
                         "{kind, source} for one that has not been sent"}

    if token:
        row = _document_by_token(base, token)
        if row is None:
            return {"status": 404, "error": "No such document."}
        try:
            model = object_documents.model_from_snapshot(row.get("snapshot"))
        except object_documents.DocumentError as exc:
            return {"status": 409, "error": str(exc)}
        # A sent document's PDF is a PDF of what was SENT -- the snapshot, the
        # same thing the link shows. A PDF built from the live record would be
        # a second version of a document somebody already has.
    else:
        model, refusal = _model_from_source(base, kind, source, settings)
        if refusal is not None:
            return refusal

    page = object_documents.render_page(
        model, chrome=False,
        size=settings.get(object_documents.PAGE_SIZE_SETTING))
    html_body = page["body"]

    if not engine["available"]:
        # The honest absent state. It names the setting, it names what to
        # install and what each option costs, and it names the thing that
        # already works -- so an operator gets a decision rather than an error.
        return {
            "status": 409,
            "error": engine["reason"],
            "setting": object_documents.PDF_ENGINE_SETTING,
            "engine": engine["engine"],
            "install": engine["install"],
            "alternative": ("Print this document and choose Save as PDF. It "
                            "needs no dependency and produces the same page."),
            # The boundary, demonstrated rather than described: this is the
            # exact HTML an engine would be handed, byte for byte the page the
            # print view serves.
            "html": html_body,
        }

    # Unreachable today: pdf_engine_status never returns available=True,
    # because no engine module ships with this server. The line is here so the
    # contract is unambiguous for whoever installs one -- take `html_body`,
    # return bytes, and never render a second template.
    return {"status": 501,
            "error": (f"'{engine['engine']}' reports as available but no "
                      "adapter is wired up in this build."),
            "html": html_body}


def GET(request):
    """The capability, asked about rather than invoked -- so a surface can
    check before it draws a button, without side effects."""
    settings = _settings(_base_dir())
    status = object_documents.pdf_engine_status(
        settings.get(object_documents.PDF_ENGINE_SETTING))
    return {"status": "ok", "pdf": status,
            "alternative": "Print -> Save as PDF works today and costs nothing."}
