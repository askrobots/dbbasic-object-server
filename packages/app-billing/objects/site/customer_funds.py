"""Customer funds reconciliation: does what we owe match what we booked?

The report that makes prepaid balances honest. Wallet balances are a
LIABILITY -- money customers handed over before the service was
rendered -- and this page answers the one question that liability has to
survive: does the sum of every wallet equal the credit balance of the
customer-funds account in the books?

Two facts about the arithmetic, both of which a naive version gets wrong:

**Outstanding holds are added back.** A wallet with a hold in flight
reports a balance BELOW what its owner is actually owed; the money is
still theirs until the run settles, and system_wallet_books deliberately
composes nothing for a hold (a hold is money that was never spent, not a
debit that got undone). Without adding holds back, this report would
show a discrepancy every time a template run was in progress -- a
reconciliation that cries wolf is one nobody reads.

**A liability's natural balance is a CREDIT.** Booked liability is
credits minus debits, not the trial balance's raw column totals.

Computed server-side, like app-finance's trial_balance page and for the
same reason: it is a fold across every wallet and every posted journal,
not one record's detail, so there is no natural collection endpoint for
the browser to call.

Trust boundary: this is an OPERATOR report. Unlike trial_balance, which
scopes to the signed-in user's own books, reconciling customer funds is
inherently cross-owner -- the question is whether OUR books match ALL
customers' balances. It therefore requires an admin identity and says so
rather than quietly showing a signed-in customer a fold over everyone's
money.
"""
from __future__ import annotations

import os

_STYLE = """
table.cf { width: 100%; border-collapse: collapse; margin: 1rem 0; max-width: 40rem; }
table.cf th, table.cf td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--line, #38384a); }
table.cf td.num { text-align: right; font-variant-numeric: tabular-nums; }
table.cf tr.total td { font-weight: 700; border-top: 2px solid var(--line, #38384a); }
.verdict { padding: 0.6rem 0.9rem; border-radius: 6px; margin: 1rem 0; max-width: 40rem; }
.verdict.ok { border: 1px solid #2f7d4f; }
.verdict.off { border: 1px solid #b5493a; }
"""

DATA_DIR_ENV = "DBBASIC_DATA_DIR"


def _data_dir() -> str:
    import object_records
    return os.environ.get(DATA_DIR_ENV, object_records.DEFAULT_DATA_DIR)


def _money(minor, currency: str = "USD") -> str:
    value = int(minor or 0)
    sign = "-" if value < 0 else ""
    amount = abs(value)
    return f"{sign}{currency} {amount // 100}.{amount % 100:02d}"


def _esc(value) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _report(base):
    import object_billing
    import object_finance
    import object_records

    wallets = object_records.read_collection_records("wallets", base_dir=base)
    entries = object_records.read_collection_records("wallet_entries", base_dir=base)

    balances = []
    for wallet in wallets:
        try:
            balances.append(int(wallet.get("balance_minor") or 0))
        except (TypeError, ValueError):
            continue

    funds_account = ""
    for row in object_records.read_collection_records("app_settings", base_dir=base):
        if row.get("key") == "billing.journal.customer_funds_account":
            funds_account = str(row.get("value") or "").strip()
            break

    debit = credit = 0
    if funds_account:
        for row in object_finance.trial_balance(base_dir=base):
            if row["account_id"] == funds_account:
                debit = int(row["debit_total_cents"] or 0)
                credit = int(row["credit_total_cents"] or 0)
                break

    result = object_billing.customer_funds_reconciliation(
        balances, debit, credit,
        outstanding_holds=object_billing.outstanding_holds_minor(entries),
    )
    result["wallet_count"] = len(balances)
    result["funds_account"] = funds_account
    return result


def GET(request):
    identity = request.get("_identity", {}) or {}
    user_id = identity.get("user_id")
    is_admin = bool(identity.get("is_admin")) or "admin" in (identity.get("roles") or [])

    if not user_id:
        body = ('<p class="hint"><a href="/login?next=/customer-funds">Sign in</a>'
                " to see this report.</p>")
    elif not is_admin:
        body = ('<p class="hint">Customer funds reconciliation is an operator '
                "report: it folds every customer's balance against the books, "
                "so it is deliberately not scoped to one signed-in account. "
                "An admin identity is required.</p>")
    else:
        try:
            result = _report(_data_dir())
        except Exception as exc:
            result = None
            body = ('<p class="hint">Could not compute: '
                    f"{_esc(str(exc)[:200])}</p>")
        if result is not None:
            if not result["funds_account"]:
                verdict = ('<div class="verdict off"><strong>Not configured.</strong> '
                           "Set <code>billing.journal.customer_funds_account</code> in "
                           "app_settings so wallet money reaches the books. Until then "
                           "nothing is booked and there is nothing to reconcile "
                           "against.</div>")
            elif result["balanced"]:
                verdict = ('<div class="verdict ok"><strong>Balanced.</strong> '
                           "Every cent owed to a customer is on the books as a "
                           "liability.</div>")
            else:
                verdict = ('<div class="verdict off"><strong>Off by '
                           f"{_esc(_money(result['difference_minor']))}.</strong> "
                           "What customers are owed does not match the booked "
                           "liability. Entries created before "
                           "system_wallet_books was installed compose nothing "
                           "retroactively -- that is the usual cause, and it is a "
                           "backfill, not a bug.</div>")
            body = f"""
<div class="breadcrumb"><a href="/">Home</a> / Customer Funds</div>
<div class="pagehead"><h1>Customer Funds Reconciliation</h1></div>
<p class="hint">Prepaid balances are money owed, not money earned. This
compares what customers are owed against what the books say we owe.</p>
{verdict}
<table class="cf">
<tbody>
<tr><td>Wallet balances ({result['wallet_count']} wallets)</td>
    <td class="num">{_esc(_money(result['wallet_balances_minor']))}</td></tr>
<tr><td>Outstanding holds (ring-fenced, still the customer's)</td>
    <td class="num">{_esc(_money(result['outstanding_holds_minor']))}</td></tr>
<tr class="total"><td>Owed to customers</td>
    <td class="num">{_esc(_money(result['owed_to_customers_minor']))}</td></tr>
<tr><td>Booked liability (credits &minus; debits)</td>
    <td class="num">{_esc(_money(result['booked_liability_minor']))}</td></tr>
<tr class="total"><td>Difference</td>
    <td class="num">{_esc(_money(result['difference_minor']))}</td></tr>
</tbody>
</table>
"""

    who = (f"signed in as <strong>{_esc(user_id)}</strong>" if user_id
           else '<a href="/login?next=/customer-funds">sign in</a>')
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Customer Funds</title>
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
