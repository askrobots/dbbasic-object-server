"""site_reconcile -- the bank reconciliation statement (plan/bank-import-
reconciliation-spec.md section 6): "bank closing balance (last accepted
import) vs. book cash balance (fin account), reconciled by: matched total,
outstanding timing items, unresolved tail. The classic rec statement -- a
generative view over stored state, no new engine."

Identity posture, same as packages/system-dashboard/objects/system/
scheduler.py: this object is public execute, and everything is gated on a
signed-in identity inside. Unlike the scheduler board (admin-only), any
registered user may see this page -- but ONLY their own accounts and lines.
The scoping happens twice, deliberately: once here (every read below is
filtered to owner_id == the caller before it is ever displayed), and again
in object_banking.reconciliation() itself, which requires the caller to
name a bank_account_id it has already verified belongs to them -- the same
trust-boundary posture packages/app-finance/objects/site/trial_balance.py
documents for object_finance.trial_balance(): these library functions read
collections directly via object_records, which is NOT subject to
permissions/rules.json's row_filter the way the HTTP /collections/* API
is, so the caller (this page) is the only thing standing between one
owner's ledger and another's.

The account list and the one-account statement are the same page (?account=
<id> selects one of the caller's own accounts) rather than two objects,
matching trial_balance.py's "pick simpler, report which" posture -- there
is no natural second collection endpoint here either; the statement is a
fold across three collections, not a single record's detail.
"""
from __future__ import annotations

import html
import json
import os

import object_banking
import object_records

DATA_DIR_ENV = "DBBASIC_DATA_DIR"


def _base_dir() -> str:
    return os.environ.get(DATA_DIR_ENV, "data")


def _esc(value) -> str:
    return html.escape(str(value or ""))


def _money(cents_value, currency: str = "USD") -> str:
    """Whole-currency-unit display, 2dp -- the stored value stays integer
    cents everywhere else (object_banking.py, bank_lines.amount_cents).
    None means "unknown" (no import yet, no book account set), which is a
    materially different thing from a real zero balance and must not
    print as one.
    """
    if cents_value is None:
        return "—"  # em dash: genuinely unknown, not zero
    cents = int(cents_value)
    sign = "-" if cents < 0 else ""
    amount = abs(cents)
    return f"{sign}{currency} {amount // 100:,}.{amount % 100:02d}"


ASSURANCE_LABEL = {
    "verified": ("Verified", "ok"),
    "unverified": ("Unverified", "warn"),
    "flagged": ("Flagged", "bad"),
}


def _assurance_badge(assurance) -> str:
    if not assurance:
        return '<span class="badge muted">No imports yet</span>'
    label, tone = ASSURANCE_LABEL.get(assurance, (assurance, "muted"))
    return f'<span class="badge {tone}">{_esc(label)}</span>'


def _my_accounts(base, user_id):
    return [a for a in object_records.read_collection_records("bank_accounts", base_dir=base)
            if a.get("owner_id") == user_id]


def _my_lines(base, user_id, bank_account_id):
    return [l for l in object_records.read_collection_records("bank_lines", base_dir=base)
            if l.get("bank_account_id") == bank_account_id and l.get("owner_id") == user_id]


def _suggestion_summary(row) -> str:
    raw = row.get("suggestions") or ""
    if not raw:
        return ""
    try:
        proposals = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    if not proposals:
        return ""
    best = proposals[0]
    return (f"tier {_esc(best.get('tier'))}: {_esc(', '.join(best.get('refs') or []))}"
            f" &mdash; {_esc(best.get('why'))}")


def _line_row_html(row, currency):
    status = row.get("match_status") or "unmatched"
    tone = "warn" if status == "suggested" else ""
    return (
        "<tr>"
        f"<td>{_esc(row.get('posted_on'))}</td>"
        f"<td>{_esc(row.get('description'))}</td>"
        f"<td class=\"num\">{_money(row.get('amount_cents'), currency)}</td>"
        f"<td class=\"{tone}\">{_esc(status)}</td>"
        f"<td>{_suggestion_summary(row)}</td>"
        "</tr>"
    )


def _timing_row_html(row, currency):
    return (
        "<tr>"
        f"<td>{_esc(row.get('posted_on'))}</td>"
        f"<td>{_esc(row.get('description'))}</td>"
        f"<td class=\"num\">{_money(row.get('amount_cents'), currency)}</td>"
        "</tr>"
    )


def _account_list_html(accounts, selected_id):
    if not accounts:
        return '<p class="hint">No bank accounts yet.</p>'
    items = []
    for account in accounts:
        current = " class=\"current\"" if account.get("id") == selected_id else ""
        items.append(
            f'<li{current}><a href="/reconcile?account={_esc(account["id"])}">'
            f'{_esc(account.get("name") or account["id"])}</a>'
            f' <span class="hint">{_esc(account.get("institution"))}</span></li>'
        )
    return "<ul class=\"accountlist\">" + "".join(items) + "</ul>"


def _statement_html(account, result, open_lines, timing_lines):
    currency = account.get("currency") or "USD"
    tiles = f"""
<div class="tiles">
<div class="tile"><div class="n">{_money(result['bank_closing_cents'], currency)}</div><div class="l">Bank closing balance</div></div>
<div class="tile"><div class="n">{_money(result['book_balance_cents'], currency)}</div><div class="l">Book balance</div></div>
<div class="tile"><div class="n">{_money(result['difference_cents'], currency)}</div><div class="l">Difference</div></div>
<div class="tile"><div class="n">{_money(result['timing_cents'], currency)}</div><div class="l">Outstanding timing items</div></div>
</div>"""

    rec_rows = "".join(_line_row_html(row, currency) for row in open_lines) or \
        '<tr><td colspan="5" class="hint">Nothing open -- every line is matched or resolved.</td></tr>'
    timing_rows = "".join(_timing_row_html(row, currency) for row in timing_lines) or \
        '<tr><td colspan="3" class="hint">No outstanding timing items.</td></tr>'

    status_badge = ('<span class="badge ok">&#10003; Reconciled</span>' if result["reconciled"]
                    else '<span class="badge warn">Not yet reconciled</span>')

    statement_date = result.get("bank_statement_date") or "—"

    return f"""
<div class="pagehead"><h1>{_esc(account.get('name') or account['id'])}</h1>
<p class="hint">{_esc(account.get('institution'))} &middot; statement as of {_esc(statement_date)}
&middot; {_assurance_badge(result['assurance'])}</p></div>
{tiles}
<h2>The tie</h2>
<table class="rec">
<tbody>
<tr><td>Bank statement closing balance</td><td class="num">{_money(result['bank_closing_cents'], currency)}</td></tr>
<tr><td>Book balance (your ledger)</td><td class="num">{_money(result['book_balance_cents'], currency)}</td></tr>
<tr class="totals"><td>Difference</td><td class="num">{_money(result['difference_cents'], currency)}</td></tr>
<tr><td>Outstanding timing items (on the statement, not yet booked)</td><td class="num">{_money(result['timing_cents'], currency)}</td></tr>
<tr class="totals"><td>{status_badge}</td><td class="num">{result['unmatched_count']} unmatched
&middot; {result['suggested_count']} suggested</td></tr>
</tbody>
</table>
<p class="hint">Reconciled means the difference between the bank and the books is
fully EXPLAINED by outstanding timing items -- not merely that the numbers
happen to match. Any unmatched or suggested line below is unexplained slack.</p>
<h2>Open lines (unmatched &amp; suggested)</h2>
<table class="rec">
<thead><tr><th>Posted</th><th>Description</th><th class="num">Amount</th><th>Status</th><th>Suggestion</th></tr></thead>
<tbody>{rec_rows}</tbody>
</table>
<h2>Resolved timing items</h2>
<table class="rec">
<thead><tr><th>Posted</th><th>Description</th><th class="num">Amount</th></tr></thead>
<tbody>{timing_rows}</tbody>
</table>
"""


_STYLE = """
.wrap { max-width: 1100px; margin: 0 auto; padding: 1.25rem; }
table.rec { width: 100%; border-collapse: collapse; margin: 0.5rem 0 1.5rem; }
table.rec th, table.rec td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--line, #38384a); vertical-align: top; }
table.rec td.num, table.rec th.num { text-align: right; }
table.rec tr.totals td { font-weight: 700; border-top: 2px solid var(--line, #38384a); }
.tiles { display: flex; gap: 0.75rem; flex-wrap: wrap; margin: 1rem 0; }
.tile { background: var(--panel, #1a1a22); border: 1px solid var(--line, #333); border-radius: 8px; padding: 0.6rem 1rem; min-width: 160px; }
.tile .n { font-size: 1.2rem; font-weight: 600; }
.tile .l { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted, #999); }
h2 { font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted, #999); margin: 1.5rem 0 0.5rem; }
.hint { color: var(--muted, #999); font-size: 0.85rem; }
.badge { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 999px; font-size: 0.75rem; }
.badge.ok { background: rgba(82,210,115,0.15); color: var(--positive, #52d273); }
.badge.warn { background: rgba(241,183,71,0.15); color: var(--warning, #f1b747); }
.badge.bad { background: rgba(255,107,107,0.15); color: var(--danger, #ff6b6b); }
.badge.muted { background: rgba(153,153,153,0.15); color: var(--muted, #999); }
.warn { color: var(--warning, #f1b747); }
ul.accountlist { list-style: none; padding: 0; margin: 0.5rem 0 1.5rem; }
ul.accountlist li { padding: 0.4rem 0; border-bottom: 1px solid var(--line, #333); }
ul.accountlist li.current a { font-weight: 700; }
"""


def _page(body, *, title="Reconcile"):
    return {
        "content_type": "text/html; charset=utf-8",
        "body": f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<script src="/nav"></script>
<div class="wrap">
<h1>Reconcile</h1>
{body}
</div>
</body>
</html>""",
    }


def GET(request):
    identity = request.get("_identity") or {}
    user_id = identity.get("user_id")
    if not user_id:
        return _page('<p class="hint"><a href="/login?next=/reconcile">Sign in</a> to see '
                     "your bank accounts and their reconciliation statements.</p>")

    base = _base_dir()
    accounts = _my_accounts(base, user_id)
    requested_id = str(request.get("account") or "").strip()
    # Owner scoping happens right here: an id the caller does not own is
    # simply not in `accounts`, so it is silently treated as no selection
    # rather than looked up directly -- there is no code path from a
    # guessed/borrowed id to another owner's statement.
    selected = next((a for a in accounts if a.get("id") == requested_id), None)

    body = f'<h2>Your bank accounts</h2>{_account_list_html(accounts, requested_id)}'
    if selected is not None:
        as_of = str(request.get("as_of") or "").strip()
        result = object_banking.reconciliation(selected["id"], base_dir=base, as_of=as_of)
        lines = _my_lines(base, user_id, selected["id"])
        open_lines = sorted(
            (l for l in lines if (l.get("match_status") or "unmatched") in ("unmatched", "suggested")),
            key=lambda l: l.get("posted_on") or "")
        timing_lines = sorted(
            (l for l in lines if l.get("match_status") == "resolved"
             and (l.get("resolved_as") or "") == "timing"),
            key=lambda l: l.get("posted_on") or "")
        body += _statement_html(selected, result, open_lines, timing_lines)
    elif requested_id:
        body += '<p class="hint">That account was not found among yours.</p>'

    return _page(body)
