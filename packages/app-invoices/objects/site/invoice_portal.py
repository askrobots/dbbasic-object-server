"""site_invoice_portal -- the door a dunning email currently has no way to
point at (plan/customer-payment-portal-spec.md). GET /pay/{token}.

The core posture: the customer is NOT a user of this system. Invoices
carry customer_email, not an identity, so there is no account to sign
into and no signup wall to put between a willing payer and their money.
Access is instead a CAPABILITY URL -- an unguessable bearer token
(secrets.token_urlsafe(32), minted by action_regenerate_portal_link or
lazily by system_invoice_aging) that grants exactly "view this one
invoice and see how to pay it." No session, no identity, no other record
ever reachable from here.

Looked up BY TOKEN ONLY -- never by id. Record ids are already UUIDv4
(docs/runtime-contract.md) and appear in record_changes, in the
owner-scoped /collections/invoices/records/{id} path, and in correlation
ids threaded through logs -- surfaces an operator reasonably expects to
stay internal, not something a stranger's browser history holds. Keeping
portal_token a *separate* field (rather than treating the id itself as
the secret) means there is no enumeration path from a guessed id to an
invoice, and a leaked link is revoked (action_regenerate_portal_link)
without touching the invoice's identity or its own audit trail.

secrets.compare_digest is used for the match (constant-time: a plain `==`
on attacker-controlled input leaks how many leading characters matched,
via how long the comparison took, which is exactly the kind of side
channel a bearer-token scheme exists to close). A blank or unknown token
renders the SAME "not found" page, at 404, as an invoice that never
existed -- never 403 -- because a 403 would confirm the token namespace
itself is a thing worth attacking; "not found" makes a guess and a typo
indistinguishable.

No nav, no global search, no site chrome beyond the base stylesheet: this
page is not "in" the app. A customer following an emailed link should
never be one click from someone else's invoices or the sign-in wall of a
system they have no account on.

**THE DOCUMENT HALF NOW COMES FROM THE SHARED LAYER** (object_documents,
one of the two pages migrated as proof in app-documents 0.1.0). The
business header, the document identity, the bill-to party, the lines
table, the totals and every print rule are the same code every other
document on this server uses -- so an invoice and a quote cannot slowly
stop looking like they came from the same business, and the print CSS
gets the one rule seven hand-rolled `@media print` blocks all missed
(`thead { display: table-header-group }`, which repeats the column
headings when a long invoice runs onto a second page).

What stays local is what is genuinely PORTAL rather than DOCUMENT: the
status bucket the customer sees, the tiles, the payment instructions, and
the pay button. Those are about paying, not about the invoice.

**bill_to, not ship_to.** The layer decides that from the kind
(KINDS["invoice"]["to_role"]), which is the same mechanism that keeps the
packing slip's ship-to off an invoice and prices off a packing slip.

Print -> Save as PDF is stated on the page, because it already works and
costs nothing. A PDF *button* appears only if a PDF engine is actually
configured -- the same rule as the Pay button below, and for the same
reason.
"""
from __future__ import annotations

import html
import os
import secrets
from datetime import datetime, timezone

import object_documents
import object_money
import object_stripe
import object_records

DATA_DIR_ENV = "DBBASIC_DATA_DIR"
ACTOR = "site_invoice_portal"
KIND = "invoice"

# Buckets shown to the customer. The schema's real status enum is finer
# (draft/sent/paid/partial/overdue/void) than the four states this spec
# calls for -- draft/sent/overdue all read as "money is owed" from a
# payer's point of view, so they share the "unpaid" rendering. void is the
# one status that refuses the door outright regardless of what the token
# validates against (see _render_void).
STATUS_PAID = "paid"
STATUS_PARTIAL = "partial"
STATUS_VOID = "void"


def _base_dir() -> str:
    return os.environ.get(DATA_DIR_ENV, "data")


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _esc_multiline(value) -> str:
    return _esc(value).replace("\n", "<br>")


def _settings(base) -> dict:
    """Every app_settings row as one mapping.

    Read whole rather than key by key: object_documents wants a mapping of
    business.*, the collection is tiny, and one scan beats five. Duplicated,
    on purpose, from every other package that reads app_settings -- there is
    still no shared object_settings module in this codebase
    (docs/logic-decisions.md #4).
    """
    values: dict[str, str] = {}
    try:
        for row in object_records.read_collection_records("app_settings", base_dir=base):
            key = str(row.get("key") or "").strip()
            value = str(row.get("value") or "").strip()
            if key and value:
                values[key] = value
    except Exception:
        pass
    return values


def _find_invoice_by_token(base, token: str) -> dict | None:
    """The only lookup this page performs: a full scan matched with a
    constant-time comparison, never a keyed/indexed lookup by id. A blank
    token matches nothing -- without this guard, an invoice that has never
    had a portal_token minted (stored as "") would otherwise "match" a
    blank incoming token, which would turn "no link generated yet" into an
    accidental open door.
    """
    token = str(token or "").strip()
    if not token:
        return None
    try:
        rows = object_records.read_collection_records("invoices", base_dir=base)
    except Exception:
        return None
    for row in rows:
        candidate = str(row.get("portal_token") or "")
        if not candidate:
            continue
        if secrets.compare_digest(candidate, token):
            return row
    return None


def _invoice_lines_for(base, invoice_id: str) -> list[dict]:
    """Line items, skipped gracefully when app-invoices' invoice_lines
    collection/schema is not installed -- this page must render a usable
    invoice even on a deployment that never adopted line-item detail.
    """
    try:
        rows = object_records.read_collection_records("invoice_lines", base_dir=base)
    except Exception:
        return []
    return [r for r in rows if r.get("invoice_id") == invoice_id]


def _stamp_view(base, invoice: dict) -> None:
    """Best-effort view counter. Dunning intelligence downstream ("viewed
    3x, still unpaid" is a different follow-up than "never opened") depends
    on this, but no counter is worth breaking the page a customer is
    actively trying to read -- any failure here is swallowed.
    """
    try:
        views = int(str(invoice.get("portal_views") or "0").strip() or "0")
    except ValueError:
        views = 0
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        object_records.update_collection_record(
            "invoices",
            invoice["id"],
            {"portal_views": str(views + 1), "last_viewed_at": now},
            base_dir=base,
            actor=ACTOR,
        )
    except Exception:
        pass


# What is left of this page's own CSS after the migration: the portal's
# paying-a-bill furniture (tiles, status badges, the instructions panel) and
# nothing else. Every rule about how a DOCUMENT looks, on screen or on paper,
# now comes from object_documents.document_css, inlined by render_page.
_STYLE = """
.tiles { display: flex; gap: 0.75rem; flex-wrap: wrap; margin: 1.25rem 0; }
.tile { background: var(--panel, #1a1a22); border: 1px solid var(--line, #333); border-radius: 8px; padding: 0.6rem 1rem; min-width: 160px; }
.tile .n { font-size: 1.3rem; font-weight: 700; }
.tile .l { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted, #999); }
.badge { display: inline-block; padding: 0.15rem 0.6rem; border-radius: 999px; font-size: 0.8rem; font-weight: 600; }
.badge.ok { background: rgba(82,210,115,0.15); color: var(--positive, #52d273); }
.badge.warn { background: rgba(241,183,71,0.15); color: var(--warning, #f1b747); }
.badge.bad { background: rgba(255,107,107,0.15); color: var(--danger, #ff6b6b); }
.badge.muted { background: rgba(153,153,153,0.15); color: var(--muted, #999); }
.instructions { border: 1px solid var(--line, #333); border-radius: 8px; padding: 0.9rem 1.1rem; margin: 1rem 0; white-space: pre-wrap; }
.notfound { text-align: center; padding: 3rem 1rem; }
"""


def _not_found() -> dict:
    """Deliberately not rendered through the document layer: there is no
    document. Bare, 404, and identical for a mistyped token and a token that
    never existed."""
    return {
        "status": 404,
        "content_type": "text/html; charset=utf-8",
        "body": f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Not found</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="notfound"><h1>Not found</h1>
<p>This payment link is not valid. It may have been mistyped, or the invoice
owner may have generated a newer link. Contact the business that sent it for a
fresh one.</p></div>
</body>
</html>""",
    }


def _payment_instructions_html(settings) -> str:
    text = str(settings.get("portal.payment_instructions") or "").strip()
    if not text:
        return (
            '<div class="instructions hint">This business has not published '
            "payment instructions yet. Contact them directly to arrange "
            "payment.</div>"
        )
    return f'<div class="instructions">{_esc_multiline(text)}</div>'


def _model_for(invoice, lines, settings, base, *, with_lines=True):
    """The invoice as a document, folded by the shared layer.

    require_business=False keeps the promise site_invoice_portal has always
    made: an unbranded invoice is honest, a fake one is not. A customer is
    never shown a nudge to configure somebody else's business (render_page
    only draws that for an operator -- see setup_nudge).
    """
    currency = str(invoice.get("currency") or "USD").strip() or "USD"

    def money(cents):
        try:
            return object_money.format_amount(cents or 0, currency, base_dir=base)
        except Exception:
            return f"{cents or 0} (minor units)"

    due = str(invoice.get("due_date") or "").strip()
    facts = object_documents.facts_from_records(
        KIND, invoice, lines if with_lines else (), money=money,
        extra={"to": {"name": str(invoice.get("customer_name") or ""),
                      "address": str(invoice.get("customer_address") or ""),
                      "email": str(invoice.get("customer_email") or "")},
               "terms": f"Due {due}" if due else "Due on receipt"})
    if not with_lines:
        # A cancelled invoice shows no financial content at all -- no lines,
        # no totals, no balance, and no due date either: a payment term on a
        # bill that no longer exists is an instruction to pay it.
        facts["totals"] = []
        facts["terms"] = ""
    return object_documents.build_model(KIND, facts, settings,
                                        require_business=False)


def _page(model, *, title, before="", after="", settings=None):
    settings = settings or {}
    pdf = object_documents.pdf_engine_status(
        settings.get(object_documents.PDF_ENGINE_SETTING))
    page = object_documents.render_page(
        model, title=title, before=before, after=after,
        # chrome=False is the posture this page has always had: handed to a
        # stranger's inbox, so never one click from anything but this invoice.
        chrome=False, pdf=pdf["available"],
        size=settings.get(object_documents.PAGE_SIZE_SETTING))
    # The portal's own furniture rides in the same <head>, after the shared
    # document CSS so a tile can override a document rule and never the other
    # way round.
    page["body"] = page["body"].replace("</head>", f"<style>{_STYLE}</style></head>", 1)
    return page


def _tiles(*pairs) -> str:
    cells = "".join(f'<div class="tile"><div class="n">{value}</div>'
                    f'<div class="l">{label}</div></div>'
                    for label, value in pairs)
    return f'<div class="tiles">{cells}</div>'


def _render_void(invoice, settings, base) -> dict:
    """Void refuses the portal outright: no balance, no line items, no
    payment instructions -- showing any of that on a cancelled document
    would invite paying a bill that no longer exists. The status check
    happens here, in the page, rather than only via a token that gets
    scrubbed on void (that hook lives outside this file's edit boundary),
    so the customer-facing outcome holds regardless of whether the token
    itself was ever cleared -- and it is a terminal status in the schema's
    own transition table (no move ever leaves void), so this is permanent.
    """
    model = _model_for(invoice, (), settings, base, with_lines=False)
    return _page(
        model, settings=settings,
        title=f"Invoice {invoice.get('number') or ''} -- cancelled",
        after='<p><span class="badge muted">Cancelled</span></p>'
              '<p class="doc-hint">This invoice was cancelled. If you believe '
              "this is a mistake, contact the business that sent it.</p>")


def _render_paid(invoice, currency, base, lines, settings) -> dict:
    total = object_money.format_amount(invoice.get("total_cents") or 0, currency, base_dir=base)
    paid = object_money.format_amount(invoice.get("amount_paid_cents") or 0, currency, base_dir=base)
    paid_at = invoice.get("paid_at") or ""
    model = _model_for(invoice, lines, settings, base)
    before = (f'<p><span class="badge ok">Paid in full</span>'
              + (f' <span class="doc-hint">on <time datetime="{_esc(paid_at)}">'
                 f'{_esc(paid_at)}</time></span>' if paid_at else "")
              + "</p>"
              + _tiles(("Total", total), ("Paid", paid)))
    return _page(model, settings=settings, before=before,
                 after='<p class="doc-hint">This is your receipt. No further '
                       "payment is due on this invoice.</p>",
                 title=f"Invoice {invoice.get('number') or ''} -- paid")


def _render_partial(invoice, currency, base, lines, settings) -> dict:
    total = object_money.format_amount(invoice.get("total_cents") or 0, currency, base_dir=base)
    paid = object_money.format_amount(invoice.get("amount_paid_cents") or 0, currency, base_dir=base)
    balance = object_money.format_amount(invoice.get("balance_due_cents") or 0, currency, base_dir=base)
    model = _model_for(invoice, lines, settings, base)
    before = ('<p><span class="badge warn">Partially paid</span></p>'
              + _tiles(("Total", total), ("Received so far", paid),
                       ("Still due", balance)))
    return _page(model, settings=settings, before=before,
                 after="<h2>How to pay the remaining balance</h2>"
                       + _payment_instructions_html(settings),
                 title=f"Invoice {invoice.get('number') or ''} -- partially paid")


_PAY_SCRIPT = """
const btn = document.getElementById("paybtn");
btn.addEventListener("click", async () => {
  btn.disabled = true;
  btn.textContent = "Starting secure checkout…";
  const token = location.pathname.split("/").pop();
  let data = {};
  try {
    const resp = await fetch("/objects/action_stripe_checkout", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({token: token}),
    });
    data = await resp.json();
  } catch (err) { data = {}; }
  if (data && data.url) { location.href = data.url; return; }
  // Never strand the payer: fall back to the instructions already on the
  // page rather than leaving a dead button.
  btn.disabled = false;
  btn.textContent = "Pay by card";
  document.getElementById("payerror").textContent =
    (data && data.error) || "Card payment is unavailable right now -- the payment "
    + "instructions below still work.";
});
"""


def _pay_button_html() -> str:
    """Rendered only when Stripe is configured -- see _render_unpaid.

    The same rule the PDF button obeys, one layer up: a control exists
    because the action behind it can actually succeed.
    """
    if not object_stripe.stripe_config_from_env().configured:
        return ""
    return ('<p class="noprint" style="margin:1rem 0"><button id="paybtn" class="btn primary">'
            "Pay by card</button> "
            '<span id="payerror" class="warn" style="margin-left:0.5rem"></span></p>'
            f"<script>{_PAY_SCRIPT}</script>")


def _render_unpaid(invoice, currency, base, lines, settings) -> dict:
    total = object_money.format_amount(invoice.get("total_cents") or 0, currency, base_dir=base)
    balance = object_money.format_amount(invoice.get("balance_due_cents") or 0, currency, base_dir=base)
    model = _model_for(invoice, lines, settings, base)
    before = ('<p><span class="badge bad">Payment due</span></p>'
              + _tiles(("Amount due", balance), ("Invoice total", total)))
    # The Pay button appears ONLY when card payment can actually succeed.
    # A button that posts nowhere tells a customer trying to pay you that
    # something is broken on YOUR end, so an unconfigured server shows the
    # payment instructions alone rather than a lie with a shadow on it.
    after = (_pay_button_html()
             + "<h2>How to pay</h2>"
             + _payment_instructions_html(settings))
    return _page(model, settings=settings, before=before, after=after,
                 title=f"Invoice {invoice.get('number') or ''} -- payment due")


def GET(request):
    token = request.get("token")
    base = _base_dir()
    invoice = _find_invoice_by_token(base, token)
    if invoice is None:
        return _not_found()

    _stamp_view(base, invoice)
    settings = _settings(base)
    currency = str(invoice.get("currency") or "USD").strip() or "USD"
    status = str(invoice.get("status") or "")
    lines = _invoice_lines_for(base, invoice["id"])

    if status == STATUS_VOID:
        return _render_void(invoice, settings, base)
    if status == STATUS_PAID:
        return _render_paid(invoice, currency, base, lines, settings)
    if status == STATUS_PARTIAL:
        return _render_partial(invoice, currency, base, lines, settings)
    return _render_unpaid(invoice, currency, base, lines, settings)
