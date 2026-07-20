"""Trial balance report page: the one report v1 of this package ships
(reconciled against a private predecessor-system audit, not part of this
repo, whose own reports "filter posted lines" -- profit & loss, balance
sheet, and cash flow are the same fold shape and are DEFERRED, see
dbbasic-package.json's description).

Unlike every other page in this package, this page computes its data
SERVER-SIDE by calling object_finance.trial_balance() directly in
GET(request), rather than having the browser fetch /collections/*
records and fold them in JS. That is a deliberate choice ("pick simpler,
report which" -- this is simpler): a trial balance is a fold over every
posted journal's lines across the whole owner, not a single record's
detail, so there is no natural single collection endpoint for the
browser to call the way invoice_view.py fetches one invoice and its
lines. Computing it here, once, and rendering plain HTML rows avoids
needing a bespoke API object (a fifth object in this package) just to
re-expose the same fold object_finance.py already provides.

Trust boundary, stated explicitly: object_finance.trial_balance() reads
fin_journals/fin_journal_lines/fin_accounts directly via object_records,
which is NOT subject to permissions/rules.json's row_filter
owner_id=$user_id the way the HTTP /collections/* API is (see
object_finance.trial_balance's own docstring). This handler passes
owner=user_id explicitly on every call, which is what keeps this report
scoped to the signed-in visitor's own books -- removing that argument
would leak every owner's journals into one report. There is no
anonymous or cross-owner path here at all: an anonymous visitor gets a
sign-in prompt and object_finance is never even imported for that
request.

Money is formatted for display only; the stored value stays an integer
number of cents everywhere else.
"""
from __future__ import annotations

import os

_STYLE = """
table.tb { width: 100%; border-collapse: collapse; margin: 1rem 0; }
table.tb th, table.tb td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--line, #38384a); }
table.tb th { font-weight: 600; }
table.tb td.num, table.tb th.num { text-align: right; }
table.tb tr.totals td { font-weight: 700; border-top: 2px solid var(--line, #38384a); }
"""

DATA_DIR_ENV = "DBBASIC_DATA_DIR"


def _data_dir() -> str:
    # Same pattern as packages/app-invoices/objects/system/invoice_totals.py's
    # own _data_dir(): standalone, reads os.environ directly rather than
    # depending on object_server for a base_dir. object_finance is a plain
    # library import (like object_records itself), not a registered
    # DBBASIC object, so there is no request payload to read a base_dir
    # override from.
    import object_records
    return os.environ.get(DATA_DIR_ENV, object_records.DEFAULT_DATA_DIR)


def _money(cents_value, currency: str = "USD") -> str:
    cents = int(cents_value or 0)
    sign = "-" if cents < 0 else ""
    amount = abs(cents)
    return f"{sign}{currency} {amount // 100}.{amount % 100:02d}"


def _row_html(row: dict) -> str:
    label = row["account_name"] or row["account_id"]
    code = f" ({row['account_code']})" if row["account_code"] else ""
    return (
        "<tr>"
        f"<td>{_esc(label)}{_esc(code)}</td>"
        f"<td>{_esc(row['account_type'])}</td>"
        f"<td class=\"num\">{_money(row['debit_total_cents'])}</td>"
        f"<td class=\"num\">{_money(row['credit_total_cents'])}</td>"
        "</tr>"
    )


def _esc(value) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_trial_balance served", user_id=user_id or "anonymous")

    if not user_id:
        body = '<p class="hint"><a href="/login?next=/trial-balance">Sign in</a> to see your trial balance.</p>'
    else:
        import object_finance

        rows = object_finance.trial_balance(base_dir=_data_dir(), owner=user_id)
        total_debits_cents = sum(row["debit_total_cents"] for row in rows)
        total_credits_cents = sum(row["credit_total_cents"] for row in rows)

        if rows:
            rows_html = "".join(_row_html(row) for row in rows)
        else:
            rows_html = '<tr><td colspan="4" class="hint">No posted journals yet.</td></tr>'

        totals_html = (
            '<tr class="totals">'
            "<td colspan=\"2\">Total</td>"
            f"<td class=\"num\">{_money(total_debits_cents)}</td>"
            f"<td class=\"num\">{_money(total_credits_cents)}</td>"
            "</tr>"
        )

        body = f"""
<div class="breadcrumb"><a href="/">Home</a> / Trial Balance</div>
<div class="pagehead"><h1>Trial Balance</h1></div>
<p class="hint">Posted journals only -- draft journals are excluded, matching
the predecessor system's own report semantics (a private predecessor-system
audit, not part of this repo).</p>
<table class="tb">
<thead><tr><th>Account</th><th>Type</th><th class="num">Debit</th><th class="num">Credit</th></tr></thead>
<tbody>
{rows_html}
{totals_html}
</tbody>
</table>
"""

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else '<a href="/login?next=/trial-balance">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trial Balance</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header class="app"><h1><a href="/">DBBASIC</a></h1><div class="who">{who}</div></header>
{body}
</div>
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
