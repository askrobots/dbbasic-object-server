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
networks is two. The first-party visitor cookie improves the RETURNING
question without moving that ceiling at all, and the new-versus-returning
block says on the page that it is a floor rather than a census: a cleared
cookie is a new visitor, and this server fingerprints nobody.

Computed live rather than rolled up, deliberately: page_views is capped
(20k rows on the demo box) so a single pass is cheap, and an operator
checking whether their announcement worked wants NOW, not "as of the last
five-minute pass". The one-pass fold lives in object_visitors, which is
pure and tested; this object renders it.

## Why goals and funnels landed HERE and not on a /funnels of their own

A second page was the obvious move and it is the wrong one. "How many
came", "how many of them had been before" and "how many of them bought"
are one question asked at increasing depth, and splitting them puts the
denominator on one screen and the numerator on another -- a conversion
rate you have to hold in your head across a navigation is a conversion
rate nobody computes. The mechanical argument agrees: a /funnels page
would need its own sign-in refusal, its own capture-is-off branch, its own
host filter and its own copy of every honesty caveat on this page, and a
duplicated caveat is a caveat that goes stale on one of the two copies.

What a funnel adds is one settings row (`analytics.funnel_steps`), so a
shop describes its own without code. When nothing is configured this page
shows what to configure -- built from the event types actually recorded on
THIS server, so it can be copied rather than invented -- instead of an
empty table that reads as "nobody converted".
"""

import html
import json
import os

import object_analytics
import object_conversions
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
.vis-note { border-left: 3px solid var(--accent, #b5713a); padding: .1rem 0 .1rem .7rem; margin: .4rem 0 1.2rem; font-size: .85rem; opacity: .85; }
.vis-setup { border: 1px dashed var(--line, #38384a); border-radius: 8px; padding: .8rem 1rem; margin: .4rem 0 1.5rem; }
.vis-setup pre { overflow-x: auto; font-size: .8rem; }
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


# --- goals, funnels and the returning visitor ---------------------------------

def _rows(collection, base):
    """A collection's rows, or None when it cannot be read at all.

    None is not an empty list here. A missing `conversions` collection
    means app-analytics' schema was never installed, and rendering that as
    "0 conversions" would be the exact failure this whole page is written
    against: a confident zero where the truth is "nothing was ever
    recorded".
    """
    try:
        return object_records.read_collection_records(collection, base_dir=base)
    except Exception:
        return None


def _setting(base, key):
    """Duplicated on purpose, like every other package that reads
    app_settings -- see docs/logic-decisions.md #4."""
    try:
        for row in object_records.read_collection_records("app_settings",
                                                          base_dir=base):
            if row.get("key") == key and str(row.get("value") or "").strip():
                return str(row["value"]).strip()
    except Exception:
        pass
    return ""


def _cookie_note():
    """What this box asks a browser to remember, folded from the setting
    rather than asserted. The old wording said "the one thing this server
    does ask a browser to remember is a first-party cookie" as a flat
    fact; with the cookie defaulted off that sentence was false on every
    default install, and an analytics page that misdescribes its own
    collection is the failure this whole package is written against."""
    if not object_analytics.visitor_cookie_enabled():
        return ("This server asks the browser to remember nothing at all for "
                "analytics: the first-party "
                f"<code>{_esc(object_analytics.VISITOR_COOKIE_NAME)}</code> "
                "cookie is off, which is the default, so every visit looks new "
                "and no identifier is stored on anybody's device.")
    return ("The one thing this server does ask a browser to remember is a "
            "first-party "
            f"<code>{_esc(object_analytics.VISITOR_COOKIE_NAME)}</code> cookie — "
            "an opaque token, HttpOnly, SameSite, "
            f"{object_analytics.visitor_days()} days, never set for anyone "
            "sending Do Not Track or Global Privacy Control, and never joined "
            "to a signed-in account. It makes returning visitors countable and "
            "it does not raise the ceiling: it still cannot tell a phone from a "
            "laptop, and nobody is fingerprinted.")


def _caveat_tail(caveat):
    """The caveat minus its own headline, so the page can bold the
    headline and keep the sentence. Read from the fold rather than
    retyped: two copies of an honesty note is one copy that goes stale."""
    tail = caveat.split(":", 1)[-1].strip()
    return tail[:1].upper() + tail[1:]


def _returning_block(rows, days):
    # New-versus-returning is the one number the visitor cookie buys, so
    # with the cookie off it is not a zero -- it is a question this box
    # deliberately does not ask. Rendering "0% returning" here would be
    # exactly the confident-zero failure the rest of this page is written
    # against, and it would also be the page quietly disagreeing with
    # /privacy, which says no identifier is stored.
    if not object_analytics.visitor_cookie_enabled():
        return f"""
<h2 style="font-size:1rem">New versus returning</h2>
<p class="vis-note"><strong>Not measured on this server.</strong> The
first-party <code>{_esc(object_analytics.VISITOR_COOKIE_NAME)}</code> cookie is
off (the default), so nothing is stored on a visitor's device and a returning
visitor is indistinguishable from a new one. Everything above still works —
this is the one question that goes away. Setting
<code>{_esc(object_analytics.VISITOR_COOKIE_ENV)}=on</code> answers it, and
{_esc(object_analytics.OBLIGATION)}. See <a href="/privacy">/privacy</a>, which
states whichever of the two you chose.</p>
"""
    fold = object_conversions.returning_visitors(rows, window_days=days)
    cards = "".join(
        f'<div class="vis-card"><div class="n">{value}</div>'
        f'<div class="k">{_esc(label)}</div></div>'
        for label, value in (
            ("new (cookie seen once)", fold["new"]),
            ("returning", fold["returning"]),
            ("% returning", f'{fold["returning_pct"]}%'),
            ("addresses with no cookie", fold["no_token_addresses"]),
        ))
    return f"""
<h2 style="font-size:1rem">New versus returning</h2>
<div class="vis-grid">{cards}</div>
<p class="vis-note"><strong>A floor, never a census.</strong>
{_esc(_caveat_tail(object_conversions.FLOOR_CAVEAT))}
The {fold["no_token_addresses"]} address(es) with no cookie are visitors who
sent Do Not Track or Global Privacy Control, clients that keep no cookies,
and traffic recorded before the cookie existed — they are counted as
visitors above and cannot appear on either side of this split.</p>
"""


def _goals_block(conversions, days):
    if conversions is None:
        return """
<h2 style="font-size:1rem">Goals</h2>
<p class="hint">No <code>conversions</code> collection on this server — install
app-analytics' schema. Nothing before that moment can be recovered, because
nothing was written.</p>
"""
    summary = object_conversions.by_event_type(conversions, window_days=days)
    body = "".join(
        f'<tr><td><code>{_esc(row["event_type"])}</code></td>'
        f'<td class="num">{row["count"]}</td>'
        f'<td class="num">{row["threaded"]}</td>'
        f'<td><time datetime="{_esc(row["last"])}">'
        f'{_esc(row["last"][:16].replace("T", " "))}</time></td></tr>'
        for row in summary
    ) or ('<tr><td colspan="4" class="hint">No conversions recorded in this '
          'window. Goals are written by a transition — an order confirmed, a '
          'payment received, a scan confirmed — so this stays empty until one '
          'happens with the change dispatcher running.</td></tr>')
    return f"""
<h2 style="font-size:1rem">Goals</h2>
<table class="vis-table">
<thead><tr><th>Event</th><th class="num">Count</th>
<th class="num">Threaded</th><th>Last</th></tr></thead>
<tbody>{body}</tbody></table>
<p class="hint"><strong>Threaded</strong> is how many carry a visitor token and
can therefore appear in a funnel. A goal recorded by a back-office transition
has no browser anywhere near it, so it counts here and cannot be attributed to
a visitor — said out loud rather than left to explain a funnel that looks
broken.</p>
"""


def _funnel_setup(conversions, summary):
    """What to configure, built from what this server has actually seen.

    An empty funnel table would read as "nobody converted". An example
    made of paths and event types this box has really recorded can be
    copied straight into the setting, which is the difference between
    documentation and a next step.
    """
    landing = [row["path"] for row in summary["landing"][:2]] or ["/"]
    events = [row["event_type"]
              for row in object_conversions.by_event_type(conversions or [])[:1]]
    example = json.dumps(landing + (events or ["order_confirmed"]))
    return f"""
<h2 style="font-size:1rem">Funnel</h2>
<div class="vis-setup">
<p style="margin-top:0">No funnel is configured, so there is nothing to show —
which is not the same as nobody converting. A funnel is a settings row, not
code: add <code>{_esc(object_conversions.FUNNEL_STEPS_SETTING)}</code> to
<a href="/collections/app_settings/records">app settings</a> as a JSON list of
ordered steps. A step beginning with <code>/</code> is a path prefix; anything
else is a conversion <code>event_type</code>.</p>
<pre><code>{_esc(example)}</code></pre>
<p class="hint" style="margin-bottom:0">Steps built from what this server has
actually recorded, so it can be copied rather than invented. The long form
takes labels: <code>[{{"label": "Browsed", "path": "/shop"}}, …]</code>.</p>
</div>
"""


def _timing_block(page_views, conversions):
    fold = object_conversions.time_to_conversion(page_views, conversions or [])
    if not fold["count"]:
        return ""
    cards = "".join(
        f'<div class="vis-card"><div class="n">{value}</div>'
        f'<div class="k">{_esc(label)}</div></div>'
        for label, value in (
            ("median days to convert", fold["median_days"]),
            ("quarter convert within", fold["p25_days"]),
            ("three quarters within", fold["p75_days"]),
            ("same-day conversions", fold["same_day"]),
        ))
    caveats = "".join(f'<p class="vis-note">{_esc(text)}</p>'
                      for text in fold["caveats"])
    return f"""
<h2 style="font-size:1rem">Time to conversion</h2>
<div class="vis-grid">{cards}</div>
<p class="hint">Days between a visitor's first recorded page and their
conversion, over the {fold["count"]} conversion(s) that carry a visitor token
AND still have a first page view in the log. A median of zero is a shop people
buy from on arrival; a median of nine is a decision somebody goes away and
thinks about.</p>
{caveats}
"""


def _funnel_row(row):
    """One step. The first step has no drop-off — it is the denominator,
    and rendering "0% lost" there invites reading it as a step everyone
    passed rather than as the population everything else is measured
    against."""
    lost = "" if row["drop_off_pct"] is None else str(row["drop_off"])
    pct = "" if row["drop_off_pct"] is None else f'{row["drop_off_pct"]}%'
    return (f'<tr><td>{_esc(row["label"])}</td>'
            f'<td><code>{_esc(row["match"])}</code></td>'
            f'<td class="num">{row["visitors"]}</td>'
            f'<td class="num">{row["of_entered_pct"]}%</td>'
            f'<td class="num">{lost}</td>'
            f'<td class="num">{pct}</td>'
            f'<td class="num">{row["ip_stitched"]}</td></tr>')


def _funnel_block(page_views, conversions, summary, base):
    raw = _setting(base, object_conversions.FUNNEL_STEPS_SETTING)
    steps, error = object_conversions.parse_funnel_steps(raw)
    if error:
        return f"""
<h2 style="font-size:1rem">Funnel</h2>
<p class="vis-note"><code>{_esc(object_conversions.FUNNEL_STEPS_SETTING)}</code>
could not be read: {_esc(error)}. Said out loud rather than shown as an empty
funnel — a misconfigured funnel and a funnel nobody entered look identical, and
only one of them is your fault.</p>
"""
    if not steps:
        return _funnel_setup(conversions, summary)

    fold = object_conversions.funnel(page_views, conversions or [], steps)
    body = "".join(_funnel_row(row) for row in fold["steps"])
    caveats = "".join(f'<p class="vis-note">{_esc(text)}</p>'
                      for text in fold["caveats"])
    return f"""
<h2 style="font-size:1rem">Funnel</h2>
<table class="vis-table">
<thead><tr><th>Step</th><th>Matches</th><th class="num">Visitors</th>
<th class="num">Of first</th><th class="num">Lost</th>
<th class="num">Drop-off</th><th class="num">IP-stitched</th></tr></thead>
<tbody>{body}</tbody></table>
<p class="hint">Threaded by the visitor cookie, falling back to the IP address
for rows that carry none — the <strong>IP-stitched</strong> column is how much
of each step is that fallback. An IP is not a person: an office behind one
connection is one very decisive shopper. Steps are a sequence, so a visitor
counts at a step only if they reached the one before it and did this one
afterwards.</p>
{caveats}
"""


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

    host = str(request.get("host") or "").strip().lower()
    summary = object_visitors.summarize(rows, days=days, host=host)
    totals = summary["totals"]
    retention = object_analytics.retention_days()

    # Everything below the traffic tables is scoped to the same host, so a
    # per-site view cannot show one site's visitors above another site's
    # funnel. Conversions carry no host of their own -- they are recorded
    # by a transition, not a request -- so they are not filtered, and the
    # funnel says how much of itself it could attribute.
    base = _data_dir()
    scoped = ([row for row in rows if str(row.get("host") or "") == host]
              if host else rows)
    conversions = _rows(object_conversions.CONVERSIONS_COLLECTION, base)

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

    hosts = "".join(
        f'<tr><td><a href="/visitors?days={days}&host={_esc(row["host"])}">'
        f'{_esc(row["host"])}</a></td>'
        f'<td class="num">{row["visitors"]}</td></tr>'
        for row in summary["hosts"]
    ) or '<tr><td colspan="2" class="hint">No visitors yet in this window.</td></tr>'

    head = ('<thead><tr><th>When</th><th class="num">Visitors</th><th></th>'
            '<th class="num">Views</th><th class="num">Bots</th>'
            '<th class="num">Yours</th></tr></thead>')

    body = f"""
<div class="breadcrumb"><a href="/">Home</a> / Visitors</div>
<div class="pagehead"><h1>Visitors{" &middot; " + _esc(host) if host else ""}</h1>
  <span class="muted">last {days} days &middot;
  <a href="/visitors?days=1">1</a> &middot; <a href="/visitors?days=7">7</a> &middot;
  <a href="/visitors?days=30">30</a></span></div>

<div class="vis-grid">{cards}</div>

{'<p class="hint"><a href="/visitors?days=' + str(days) + '">&larr; all sites</a></p>' if host else ''}
<h2 style="font-size:1rem">Which site</h2>
<p class="hint" style="margin-top:0">One process answers for more than one
hostname here, so a combined figure answers neither "did anyone read the
pitch" nor "did anyone try the product".</p>
<table class="vis-table">
<thead><tr><th>Host</th><th class="num">Visitors</th></tr></thead>
<tbody>{hosts}</tbody></table>

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

{_returning_block(scoped, days)}
{_goals_block(conversions, days)}
{_funnel_block(scoped, conversions, summary, base)}
{_timing_block(scoped, conversions)}

<p class="hint">A <strong>visitor</strong> is an address that loaded a real
page <em>and</em> did one thing a prober does not — opened a second page,
arrived from a referrer, or carried a session. A lone hit on
<code>/</code> is not counted, because <code>/</code> answers 200 to
everyone. A <strong>bot</strong> is everything else: scanners walking a
list of URLs that do not exist here, declared crawlers, and anything
sending an attack payload in a header. <strong>Yours</strong> is traffic from an
owner address or carrying the admin token, shown rather than hidden so you
can check the page is recording at all.</p>
<p class="hint">Unique means distinct IP address, which is an
approximation and not a person: an office behind one connection counts
once, a phone moving between wifi and cellular counts twice. {_cookie_note()}
Page views are kept for {retention} days
&middot; <a href="/analytics">path and status detail</a>
&middot; <a href="/privacy">what visitors are told</a>.</p>
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
