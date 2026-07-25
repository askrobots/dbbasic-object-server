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
"""
from __future__ import annotations

import html
import os
import secrets
from datetime import datetime, timezone

import object_money
import object_records

DATA_DIR_ENV = "DBBASIC_DATA_DIR"
ACTOR = "site_invoice_portal"

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


def _setting(base, key, default=""):
    """Duplicated, on purpose, from system_invoice_aging._setting: there is
    no shared object_settings module in this codebase yet (checked -- every
    package that reads app_settings carries its own copy of this same few
    lines), and this file's edit boundary does not include introducing one.
    """
    try:
        for row in object_records.read_collection_records("app_settings", base_dir=base):
            if row.get("key") == key and row.get("value"):
                return row["value"].strip()
    except Exception:
        pass
    return default


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


_STYLE = """
.wrap { max-width: 720px; margin: 0 auto; padding: 2rem 1.25rem; }
.pagehead { margin-bottom: 1.5rem; }
.pagehead h1 { margin: 0 0 0.25rem; font-size: 1.4rem; }
.hint { color: var(--muted, #999); font-size: 0.9rem; }
.tiles { display: flex; gap: 0.75rem; flex-wrap: wrap; margin: 1.25rem 0; }
.tile { background: var(--panel, #1a1a22); border: 1px solid var(--line, #333); border-radius: 8px; padding: 0.6rem 1rem; min-width: 160px; }
.tile .n { font-size: 1.3rem; font-weight: 700; }
.tile .l { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted, #999); }
table.lines { width: 100%; border-collapse: collapse; margin: 1rem 0 1.5rem; }
table.lines th, table.lines td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--line, #38384a); }
table.lines td.num, table.lines th.num { text-align: right; }
.badge { display: inline-block; padding: 0.15rem 0.6rem; border-radius: 999px; font-size: 0.8rem; font-weight: 600; }
.badge.ok { background: rgba(82,210,115,0.15); color: var(--positive, #52d273); }
.badge.warn { background: rgba(241,183,71,0.15); color: var(--warning, #f1b747); }
.badge.bad { background: rgba(255,107,107,0.15); color: var(--danger, #ff6b6b); }
.badge.muted { background: rgba(153,153,153,0.15); color: var(--muted, #999); }
.instructions { border: 1px solid var(--line, #333); border-radius: 8px; padding: 0.9rem 1.1rem; margin: 1rem 0; white-space: pre-wrap; }
.notfound { text-align: center; padding: 3rem 1rem; }
"""


def _page(body: str, *, title: str) -> dict:
    """Deliberately bare: no /nav script, no global search mount, no
    breadcrumb back into the app. This page is handed to a stranger's
    inbox; it must not be one click from anything but this one invoice.
    """
    return {
        "content_type": "text/html; charset=utf-8",
        "body": f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{_esc(title)}</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
{body}
</div>
</body>
</html>""",
    }


def _not_found() -> dict:
    page = _page(
        '<div class="notfound"><h1>Not found</h1>'
        '<p class="hint">This payment link is not valid. It may have been '
        "mistyped, or the invoice owner may have generated a newer link. "
        "Contact the business that sent it for a fresh one.</p></div>",
        title="Not found",
    )
    page["status"] = 404
    return page


def _business_identity_html(base) -> str:
    """Whatever the operator has configured under business.*, rendered if
    present and simply omitted if not -- there is no fabricated default
    name/address; an unbranded invoice is honest, a fake one is not.
    """
    name = _setting(base, "business.name", "")
    address = _setting(base, "business.address", "")
    if not name and not address:
        return ""
    parts = []
    if name:
        parts.append(f"<strong>{_esc(name)}</strong>")
    if address:
        parts.append(f'<div class="hint">{_esc_multiline(address)}</div>')
    return f'<div class="business">{"".join(parts)}</div>'


def _lines_table_html(lines: list[dict], currency: str, base) -> str:
    if not lines:
        return ""
    rows = []
    for line in lines:
        qty = line.get("quantity") or "1"
        rows.append(
            "<tr>"
            f"<td>{_esc(line.get('description'))}</td>"
            f"<td class=\"num\">{_esc(qty)}</td>"
            f"<td class=\"num\">{object_money.format_amount(line.get('unit_price_cents') or 0, currency, base_dir=base)}</td>"
            f"<td class=\"num\">{object_money.format_amount(line.get('line_total_cents') or 0, currency, base_dir=base)}</td>"
            "</tr>"
        )
    return f"""
<table class="lines">
<thead><tr><th>Description</th><th class="num">Qty</th><th class="num">Unit price</th><th class="num">Amount</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>"""


def _payment_instructions_html(base) -> str:
    text = _setting(base, "portal.payment_instructions", "")
    if not text:
        return (
            '<div class="instructions hint">This business has not published '
            "payment instructions yet. Contact them directly to arrange "
            "payment.</div>"
        )
    return f'<div class="instructions">{_esc_multiline(text)}</div>'


def _header_html(invoice: dict, base) -> str:
    return f"""
<div class="pagehead">
{_business_identity_html(base)}
<h1>Invoice {_esc(invoice.get('number') or invoice['id'])}</h1>
<p class="hint">Billed to {_esc(invoice.get('customer_name'))}
&middot; issued {_esc(invoice.get('issue_date') or 'unknown date')}
&middot; due {_esc(invoice.get('due_date') or 'on receipt')}</p>
</div>"""


def _render_void(invoice: dict, base) -> dict:
    """Void refuses the portal outright: no balance, no line items, no
    payment instructions -- showing any of that on a cancelled document
    would invite paying a bill that no longer exists. The status check
    happens here, in the page, rather than only via a token that gets
    scrubbed on void (that hook lives outside this file's edit boundary),
    so the customer-facing outcome holds regardless of whether the token
    itself was ever cleared -- and it is a terminal status in the schema's
    own transition table (no move ever leaves void), so this is permanent.
    """
    body = _header_html(invoice, base) + (
        '<p><span class="badge muted">Cancelled</span></p>'
        '<p class="hint">This invoice was cancelled. If you believe this is '
        "a mistake, contact the business that sent it.</p>"
    )
    return _page(body, title=f"Invoice {invoice.get('number') or ''} -- cancelled")


def _render_paid(invoice: dict, currency: str, base, lines: list[dict]) -> dict:
    total = object_money.format_amount(invoice.get("total_cents") or 0, currency, base_dir=base)
    paid = object_money.format_amount(invoice.get("amount_paid_cents") or 0, currency, base_dir=base)
    paid_at = invoice.get("paid_at") or ""
    tiles = f"""
<div class="tiles">
<div class="tile"><div class="n">{total}</div><div class="l">Total</div></div>
<div class="tile"><div class="n">{paid}</div><div class="l">Paid</div></div>
</div>"""
    body = _header_html(invoice, base) + (
        f'<p><span class="badge ok">Paid in full</span>'
        + (f' <span class="hint">on {_esc(paid_at)}</span>' if paid_at else "")
        + "</p>"
        + tiles
        + _lines_table_html(lines, currency, base)
        + '<p class="hint">This is your receipt. No further payment is due on this invoice.</p>'
    )
    return _page(body, title=f"Invoice {invoice.get('number') or ''} -- paid")


def _render_partial(invoice: dict, currency: str, base, lines: list[dict]) -> dict:
    total = object_money.format_amount(invoice.get("total_cents") or 0, currency, base_dir=base)
    paid = object_money.format_amount(invoice.get("amount_paid_cents") or 0, currency, base_dir=base)
    balance = object_money.format_amount(invoice.get("balance_due_cents") or 0, currency, base_dir=base)
    tiles = f"""
<div class="tiles">
<div class="tile"><div class="n">{total}</div><div class="l">Total</div></div>
<div class="tile"><div class="n">{paid}</div><div class="l">Received so far</div></div>
<div class="tile"><div class="n">{balance}</div><div class="l">Still due</div></div>
</div>"""
    body = _header_html(invoice, base) + (
        '<p><span class="badge warn">Partially paid</span></p>'
        + tiles
        + _lines_table_html(lines, currency, base)
        + "<h2>How to pay the remaining balance</h2>"
        + _payment_instructions_html(base)
    )
    return _page(body, title=f"Invoice {invoice.get('number') or ''} -- partially paid")


def _render_unpaid(invoice: dict, currency: str, base, lines: list[dict]) -> dict:
    total = object_money.format_amount(invoice.get("total_cents") or 0, currency, base_dir=base)
    balance = object_money.format_amount(invoice.get("balance_due_cents") or 0, currency, base_dir=base)
    tiles = f"""
<div class="tiles">
<div class="tile"><div class="n">{balance}</div><div class="l">Amount due</div></div>
<div class="tile"><div class="n">{total}</div><div class="l">Invoice total</div></div>
</div>"""
    # No Pay button: there is no card-processing rail wired up yet, and a
    # button that posts nowhere is worse than no button at all -- it tells
    # a customer trying to pay you that something is broken on YOUR end.
    body = _header_html(invoice, base) + (
        '<p><span class="badge bad">Payment due</span></p>'
        + tiles
        + _lines_table_html(lines, currency, base)
        + "<h2>How to pay</h2>"
        + _payment_instructions_html(base)
    )
    return _page(body, title=f"Invoice {invoice.get('number') or ''} -- payment due")


def GET(request):
    token = request.get("token")
    base = _base_dir()
    invoice = _find_invoice_by_token(base, token)
    if invoice is None:
        return _not_found()

    _stamp_view(base, invoice)
    currency = str(invoice.get("currency") or "USD").strip() or "USD"
    status = str(invoice.get("status") or "")
    lines = _invoice_lines_for(base, invoice["id"])

    if status == STATUS_VOID:
        return _render_void(invoice, base)
    if status == STATUS_PAID:
        return _render_paid(invoice, currency, base, lines)
    if status == STATUS_PARTIAL:
        return _render_partial(invoice, currency, base, lines)
    return _render_unpaid(invoice, currency, base, lines)
