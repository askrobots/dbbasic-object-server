"""site_statements -- the profit & loss (income statement) and balance
sheet: the two financial statements plan/accounting-coverage-and-
usability.md's M1 calls the highest-value missing piece, and the two
reports object_finance.py's own module docstring (and dbbasic-
package.json's Deferred list) named as the natural next step after
trial_balance(). Both are FOLDS over posted journals -- the exact same
data trial_balance() already reads, re-bucketed by account-type sign
convention -- not a new engine, matching the "pick simpler, report
which" precedent every report page in this package has followed so far.

Identity posture, copied from packages/app-finance/objects/site/
trial_balance.py and packages/app-banking/objects/site/reconcile.py
exactly: this object is public execute, and every byte of financial
data is gated on a signed-in identity inside. An anonymous visitor gets
a sign-in prompt and object_finance is never even imported for that
request -- there is no anonymous or cross-owner path here at all.

Trust boundary, stated explicitly (same as trial_balance.py's own
docstring): object_finance.profit_and_loss() and .balance_sheet() read
fin_journals/fin_journal_lines/fin_accounts directly via object_records,
which is NOT subject to permissions/rules.json's row_filter
owner_id=$user_id the way the HTTP /collections/* API is. This handler
passes owner=user_id explicitly on every call, which is what keeps both
statements scoped to the signed-in visitor's own books -- removing that
argument would leak every owner's ledger into one report.

Query params: ?start=&end= bound the profit & loss period (either blank
= unbounded on that side); ?as_of= bounds the balance sheet (blank = every
posted journal, no upper bound). All three default to blank, i.e. "the
whole ledger" -- the same "blank means unfiltered" convention
object_finance.py documents for these functions.

The balance sheet's "balances" / "difference_cents" fields exist to catch
a broken ledger, and a broken ledger is not a subtle problem: when
difference_cents != 0, this page renders an unmissable warning banner
above the statement rather than a balance sheet that quietly fails to
foot. Money is formatted for display only (whole currency units, 2dp);
the stored and computed values stay integer cents everywhere else.
"""
from __future__ import annotations

import html
import os

DATA_DIR_ENV = "DBBASIC_DATA_DIR"

_STYLE = """
table.stmt { width: 100%; border-collapse: collapse; margin: 0.5rem 0 1.5rem; }
table.stmt th, table.stmt td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--line, #38384a); }
table.stmt th.num, table.stmt td.num { text-align: right; }
table.stmt tr.subtotal td { font-weight: 700; border-top: 1px solid var(--line, #38384a); }
table.stmt tr.total td { font-weight: 700; border-top: 2px solid var(--line, #38384a); }
table.stmt tr.derived td { font-style: italic; }
.tiles { display: flex; gap: 0.75rem; flex-wrap: wrap; margin: 1rem 0; }
.tile { background: var(--panel, #1a1a22); border: 1px solid var(--line, #333); border-radius: 8px; padding: 0.6rem 1rem; min-width: 160px; }
.tile .n { font-size: 1.2rem; font-weight: 600; }
.tile .l { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted, #999); }
h2 { font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted, #999); margin: 1.5rem 0 0.5rem; }
.hint { color: var(--muted, #999); font-size: 0.85rem; }
form.filters { display: flex; gap: 1rem; flex-wrap: wrap; align-items: flex-end; margin: 1rem 0; }
form.filters label { display: flex; flex-direction: column; font-size: 0.75rem; color: var(--muted, #999); gap: 0.25rem; }
.warnbox { border: 2px solid var(--danger, #ff6b6b); color: var(--danger, #ff6b6b); border-radius: 8px; padding: 0.75rem 1rem; margin: 1rem 0; font-weight: 600; }
"""


def _base_dir() -> str:
    # Same standalone pattern as trial_balance.py's own _data_dir():
    # object_finance is a plain library import, not a registered DBBASIC
    # object, so there is no request payload to read a base_dir override
    # from -- read the env var directly.
    import object_records
    return os.environ.get(DATA_DIR_ENV, object_records.DEFAULT_DATA_DIR)


def _esc(value) -> str:
    return html.escape(str(value or ""))


def _money(cents_value, currency: str = "USD") -> str:
    """Whole-currency-unit display, 2dp. The stored/computed value stays
    integer cents everywhere else in object_finance.py and this module.
    """
    cents = int(cents_value or 0)
    sign = "-" if cents < 0 else ""
    amount = abs(cents)
    return f"{sign}{currency} {amount // 100:,}.{amount % 100:02d}"


def _row_html(row: dict, *, derived: bool = False) -> str:
    label = row["account_name"] or row["account_id"] or "(unlabeled)"
    code = f" ({_esc(row['account_code'])})" if row.get("account_code") else ""
    cls = ' class="derived"' if derived else ""
    return (
        f"<tr{cls}>"
        f"<td>{_esc(label)}{code}</td>"
        f"<td class=\"num\">{_money(row['amount_cents'])}</td>"
        "</tr>"
    )


def _section_html(title: str, rows: list[dict], total_cents: int, *, empty_hint: str) -> str:
    if rows:
        # The synthetic "Current period earnings" line (empty account_id,
        # appended last by object_finance.balance_sheet) renders in
        # italics via the "derived" row class so a reader can tell at a
        # glance that it is not a stored account -- see that function's
        # docstring for why the line exists at all.
        body_rows = "".join(
            _row_html(row, derived=not row.get("account_id"))
            for row in rows
        )
    else:
        body_rows = f'<tr><td colspan="2" class="hint">{_esc(empty_hint)}</td></tr>'
    return f"""
<table class="stmt">
<thead><tr><th>{_esc(title)}</th><th class="num">Amount</th></tr></thead>
<tbody>
{body_rows}
<tr class="total"><td>Total {_esc(title.lower())}</td><td class="num">{_money(total_cents)}</td></tr>
</tbody>
</table>"""


def _profit_and_loss_html(pl: dict) -> str:
    period = pl["period"]
    period_label = (
        f"{_esc(period['start'])} &ndash; {_esc(period['end'])}"
        if (period["start"] or period["end"])
        else "all posted journals"
    )
    net = pl["net_income_cents"]
    net_label = "Net income" if net >= 0 else "Net loss"
    tiles = f"""
<div class="tiles">
<div class="tile"><div class="n">{_money(pl['total_income_cents'])}</div><div class="l">Total income</div></div>
<div class="tile"><div class="n">{_money(pl['total_expenses_cents'])}</div><div class="l">Total expenses</div></div>
<div class="tile"><div class="n">{_money(net)}</div><div class="l">{_esc(net_label)}</div></div>
</div>"""
    return f"""
<div class="pagehead"><h1>Profit &amp; Loss</h1>
<p class="hint">Period: {period_label} &middot; posted journals only -- draft journals are excluded.</p></div>
{tiles}
{_section_html("Income", pl["income"], pl["total_income_cents"], empty_hint="No posted income this period.")}
{_section_html("Expenses", pl["expenses"], pl["total_expenses_cents"], empty_hint="No posted expenses this period.")}
<table class="stmt">
<tbody>
<tr class="total"><td>{_esc(net_label)}</td><td class="num">{_money(net)}</td></tr>
</tbody>
</table>
"""


def _balance_warning_html(bs: dict) -> str:
    """Render an unmissable warning when the balance sheet does not
    balance -- see object_finance.balance_sheet's docstring: this is not
    a cosmetic imbalance, it means the posted ledger itself is broken
    (a partial write, a bypass of the balanced-by-construction posting
    gate), and this page must say so loudly rather than render the
    statement as if everything is fine.
    """
    if bs["balances"]:
        return ""
    diff = bs["difference_cents"]
    direction = "more assets than liabilities + equity" if diff > 0 else "fewer assets than liabilities + equity"
    return (
        '<div class="warnbox">'
        "&#9888; This balance sheet does NOT balance. "
        f"Assets minus (liabilities + equity) = {_money(diff)} ({_esc(direction)}). "
        "This indicates the posted journals are unbalanced -- a broken ledger, "
        "not a rounding artifact -- and should be investigated before this "
        "statement is relied on."
        "</div>"
    )


def _balance_sheet_html(bs: dict) -> str:
    as_of_label = _esc(bs["as_of"]) if bs["as_of"] else "all posted journals (no end date)"
    status_badge = (
        '<span class="hint">Assets = Liabilities + Equity &#10003;</span>'
        if bs["balances"]
        else '<span class="warnbox" style="display:inline-block;padding:0.2rem 0.6rem;margin:0">Does not balance</span>'
    )
    tiles = f"""
<div class="tiles">
<div class="tile"><div class="n">{_money(bs['total_assets_cents'])}</div><div class="l">Total assets</div></div>
<div class="tile"><div class="n">{_money(bs['total_liabilities_cents'])}</div><div class="l">Total liabilities</div></div>
<div class="tile"><div class="n">{_money(bs['total_equity_cents'])}</div><div class="l">Total equity</div></div>
<div class="tile"><div class="n">{_money(bs['difference_cents'])}</div><div class="l">Difference</div></div>
</div>"""
    return f"""
<div class="pagehead"><h1>Balance Sheet</h1>
<p class="hint">As of: {as_of_label} &middot; {status_badge}</p></div>
{_balance_warning_html(bs)}
{tiles}
{_section_html("Assets", bs["assets"], bs["total_assets_cents"], empty_hint="No posted assets.")}
{_section_html("Liabilities", bs["liabilities"], bs["total_liabilities_cents"], empty_hint="No posted liabilities.")}
{_section_html("Equity", bs["equity"], bs["total_equity_cents"], empty_hint="No posted equity.")}
<p class="hint">"Current period earnings" (in Equity) is a DERIVED line -- income minus
expenses over every posted journal up to the as-of date -- not a stored account. A
real year-end close would move it into retained earnings; that close (fin_journals
kind=closing) is reserved but not yet built, so this line is how the equation stays
honest in the meantime.</p>
"""


def _filters_html(start: str, end: str, as_of: str) -> str:
    return f"""
<form class="filters" method="get" action="/statements">
<label>P&amp;L start<input type="date" name="start" value="{_esc(start)}"></label>
<label>P&amp;L end<input type="date" name="end" value="{_esc(end)}"></label>
<label>Balance sheet as of<input type="date" name="as_of" value="{_esc(as_of)}"></label>
<label>&nbsp;<button type="submit">Apply</button></label>
</form>"""


def GET(request):
    identity = request.get("_identity") or {}
    user_id = identity.get("user_id")
    _logger.info("site_statements served", user_id=user_id or "anonymous")

    if not user_id:
        body = ('<p class="hint"><a href="/login?next=/statements">Sign in</a> to see '
                "your profit &amp; loss and balance sheet.</p>")
        return _page(body)

    import object_finance

    start = str(request.get("start") or "").strip()
    end = str(request.get("end") or "").strip()
    as_of = str(request.get("as_of") or "").strip()
    base = _base_dir()

    pl = object_finance.profit_and_loss(base_dir=base, owner=user_id, start=start, end=end)
    bs = object_finance.balance_sheet(base_dir=base, owner=user_id, as_of=as_of)

    body = (
        '<div class="breadcrumb"><a href="/">Home</a> / Statements</div>'
        + _filters_html(start, end, as_of)
        + _profit_and_loss_html(pl)
        + _balance_sheet_html(bs)
    )
    return _page(body, user_id=user_id)


def _page(body: str, *, user_id: str | None = None) -> dict:
    who = (
        f"signed in as <strong>{_esc(user_id)}</strong>"
        if user_id
        else '<a href="/login?next=/statements">sign in</a>'
    )
    html_out = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Financial Statements</title>
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
    return {"content_type": "text/html; charset=utf-8", "body": html_out}
