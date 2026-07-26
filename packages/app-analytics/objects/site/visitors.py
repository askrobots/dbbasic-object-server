"""/visitors -- did anyone actually turn up?

The existing /analytics page answers "what was requested" from the
rollups: top paths, top IPs, status codes. This one answers a different
question, and it is the one an operator asks after telling somebody about
their server: how many PEOPLE came, when, and from where.

Three things it refuses to do, each because the obvious version misleads:

**It does not report one number.** Unique addresses, undifferentiated,
are dominated by scanners -- on this server 45% of distinct paths were
hit exactly once by things probing for /wp-login.php. Visitors, bots and
our own traffic are counted apart and shown apart, so a quiet day reads
as a quiet day.

**It does not hide our own hits.** They are labelled, not excluded.
Seeing your own visit is how you confirm the page is recording anything;
a counter that silently drops the only traffic you can verify is a
counter you cannot trust.

**It does not pretend an IP is a person.** The page says so, in words, on
the page. An office behind one NAT is one visitor and a phone switching
networks is two, and that is the honest unit for a server that sets no
tracking cookie and fingerprints nobody.

Computed live rather than rolled up, deliberately: page_views is capped
(20k rows on the demo box) so a single pass is cheap, and an operator
checking whether their announcement worked wants NOW, not "as of the last
five-minute pass". The one-pass fold lives in object_visitors, which is
pure and tested; this object renders it.
"""

import html
import os

import object_analytics
import object_records
import object_visitors

DATA_DIR_ENV = "DBBASIC_DATA_DIR"

_STYLE = """
.vis-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: .75rem; margin: 1rem 0 1.5rem; }
.vis-card { border: 1px solid var(--line, #38384a); border-radius: 8px; padding: .8rem; }
.vis-card .n { font-size: 1.9rem; font-weight: 600; line-height: 1.1; }
.vis-card .k { font-size: .8rem; opacity: .7; }
.vis-table { width: 100%; border-collapse: collapse; margin: .4rem 0 1.5rem; }
.vis-table th, .vis-table td { text-align: left; padding: .3rem .5rem; border-bottom: 1px solid var(--line, #38384a); font-variant-numeric: tabular-nums; }
.vis-table td.num, .vis-table th.num { text-align: right; }
.bar { display: inline-block; height: .7rem; background: var(--accent, #b5713a); border-radius: 2px; vertical-align: middle; }
.bar.bot { background: var(--line, #55556a); }
.quiet td { opacity: .45; }
"""


def _esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def _data_dir():
    return os.environ.get(DATA_DIR_ENV, object_records.DEFAULT_DATA_DIR)


def _bar(count, peak, *, bot=False):
    if not count or not peak:
        return ""
    width = max(2, round(120 * count / peak))
    return f'<span class="bar{" bot" if bot else ""}" style="width:{width}px"></span>'


def _series_rows(series, *, label_slice=None, hide_empty=False):
    peak = max([row["visitors"] for row in series] or [0]) or 1
    out = []
    for row in series:
        if hide_empty and not (row["visitors"] or row["bots"] or row["operator_views"]):
            continue
        quiet = "" if row["visitors"] else ' class="quiet"'
        label = row["bucket"][label_slice] if label_slice else row["bucket"]
        out.append(
            f"<tr{quiet}><td>{_esc(label)}</td>"
            f'<td class="num">{row["visitors"]}</td>'
            f'<td>{_bar(row["visitors"], peak)}</td>'
            f'<td class="num">{row["views"]}</td>'
            f'<td class="num">{row["bots"]}</td>'
            f'<td class="num">{row["operator_views"]}</td></tr>')
    return "".join(out) or '<tr><td colspan="6" class="hint">Nothing recorded.</td></tr>'


def GET(request):
    identity = request.get("_identity", {}) or {}
    user_id = identity.get("user_id")
    if not user_id:
        body = ('<p class="hint"><a href="/login?next=/visitors">Sign in</a> '
                'to see visitor numbers.</p>')
        return _page(body)

    if not object_analytics.analytics_enabled():
        return _page('<p class="hint">Analytics capture is off. Set '
                     '<code>DBBASIC_ANALYTICS=on</code> to start recording '
                     'page views; nothing before that moment can be recovered, '
                     'because nothing was written.</p>')

    try:
        rows = object_records.read_collection_records(
            object_analytics.PAGE_VIEWS_COLLECTION, base_dir=_data_dir())
    except Exception:
        return _page('<p class="hint">No page_views collection yet — install '
                     'app-analytics and let one request through.</p>')

    try:
        days = max(1, min(31, int(str(request.get("days") or 7))))
    except (TypeError, ValueError):
        days = 7

    summary = object_visitors.summarize(rows, days=days)
    totals = summary["totals"]
    retention = object_analytics.retention_days()

    cards = "".join(
        f'<div class="vis-card"><div class="n">{value}</div>'
        f'<div class="k">{_esc(label)}</div></div>'
        for label, value in (
            (f"unique visitors, {days}d", totals["visitor"]["unique"]),
            (f"visitor page views, {days}d", totals["visitor"]["views"]),
            ("bot addresses", totals["bot"]["unique"]),
            ("your own requests", totals["operator"]["views"]),
        ))

    referrers = "".join(
        f'<tr><td>{_esc(row["referrer"])}</td>'
        f'<td class="num">{row["visitors"]}</td></tr>'
        for row in summary["referrers"]
    ) or '<tr><td colspan="2" class="hint">No visitors yet in this window.</td></tr>'

    landing = "".join(
        f'<tr><td><code>{_esc(row["path"])}</code></td>'
        f'<td class="num">{row["visitors"]}</td></tr>'
        for row in summary["landing"]
    ) or '<tr><td colspan="2" class="hint">No pages loaded by a visitor yet.</td></tr>'

    head = ('<thead><tr><th>When</th><th class="num">Visitors</th><th></th>'
            '<th class="num">Views</th><th class="num">Bots</th>'
            '<th class="num">Yours</th></tr></thead>')

    body = f"""
<div class="breadcrumb"><a href="/">Home</a> / Visitors</div>
<div class="pagehead"><h1>Visitors</h1>
  <span class="muted">last {days} days &middot;
  <a href="/visitors?days=1">1</a> &middot; <a href="/visitors?days=7">7</a> &middot;
  <a href="/visitors?days=30">30</a></span></div>

<div class="vis-grid">{cards}</div>

<h2 style="font-size:1rem">Today, by hour (UTC)</h2>
<table class="vis-table">{head}<tbody>
{_series_rows(summary["hours"], label_slice=slice(11, 13), hide_empty=True)}
</tbody></table>

<h2 style="font-size:1rem">By day</h2>
<table class="vis-table">{head}<tbody>
{_series_rows(summary["days"])}
</tbody></table>

<h2 style="font-size:1rem">Where visitors came from</h2>
<table class="vis-table">
<thead><tr><th>Referrer</th><th class="num">Visitors</th></tr></thead>
<tbody>{referrers}</tbody></table>

<h2 style="font-size:1rem">What they landed on</h2>
<table class="vis-table">
<thead><tr><th>Page</th><th class="num">Visitors</th></tr></thead>
<tbody>{landing}</tbody></table>

<p class="hint">A <strong>visitor</strong> is an address that successfully
loaded a real page and did not announce itself as a crawler. A
<strong>bot</strong> is one that never did — mostly scanners walking a list
of URLs that do not exist here. <strong>Yours</strong> is traffic from an
owner address or carrying the admin token, shown rather than hidden so you
can check the page is recording at all.</p>
<p class="hint">Unique means distinct IP address, which is an
approximation and not a person: an office behind one connection counts
once, a phone moving between wifi and cellular counts twice. This server
sets no tracking cookie and fingerprints nobody, so that is the honest
unit available. Page views are kept for {retention} days
&middot; <a href="/analytics">path and status detail</a>.</p>
"""
    return _page(body)


def _page(body):
    return {
        "content_type": "text/html; charset=utf-8",
        "body": f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Visitors</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header class="app"><h1><a href="/">DBBASIC</a></h1></header>
{body}
</div>
<script src="/nav"></script>
</body>
</html>""",
    }
