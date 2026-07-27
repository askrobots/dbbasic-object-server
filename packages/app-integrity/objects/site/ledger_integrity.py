"""site_ledger_integrity -- the page somebody opens when they have been
asked whether the records can be trusted.

    GET /ledger-integrity        every anchored ledger, checked
    GET /ledger-integrity.json   the same fold, for a monitor

## It verifies; it does not anchor

Kept separate from system_publish_head on purpose. A pass that both took
anchors and pronounced them sound would be marking its own homework, and
the failure would be invisible: the digest it wrote is trivially the
digest it recomputes.

## The banner is the page

Everything below the banner is detail. The banner answers the only
question that matters -- **is any of this held by somebody other than this
server** -- and it answers it in plain words rather than a colour. With no
notary configured, an anchor is a bookkeeping entry: anybody able to
rewrite a ledger here can rewrite the row that describes it, sitting in
the same directory, with the same permissions. Rendering that as a green
tick would be the most misleading thing this package could do, so it
renders as a warning that says exactly what is missing and what to set.

The distinction the banner draws is not verified-versus-broken. It is
**checkable-by-a-stranger versus checkable-only-by-us**, because the first
is evidence and the second is a claim.

## What "verified" means here, exactly

That the first N rows of a ledger still hash to what an anchor recorded.
Not that the ledger is complete, not that the rows are true, not that
nothing was appended since -- appending is what a ledger is for and never
disturbs an earlier prefix. See object_ledger_head for the three outcomes
and why truncation is reported separately from a content mismatch.
"""

import html
import json
import os

import object_ledger_head
import object_notary
import object_records
import object_schemas

ANCHORS = "anchors"
SETTINGS_COLLECTION = "app_settings"
ENDPOINTS_KEY = "notary.endpoints"

_STYLE = """
.li { max-width: 52rem; }
.li h2 { font-size: 1rem; margin: 1.6rem 0 .4rem; }
.li .banner { border: 1px solid var(--line, #38384a); border-radius: 8px;
              padding: .9rem 1.1rem; margin: 1rem 0 1.6rem; }
.li .banner.weak { border-color: var(--accent, #b5713a); }
.li .banner h2 { margin: 0 0 .3rem; font-size: 1.05rem; }
.li table { width: 100%; border-collapse: collapse; margin: .4rem 0 1.2rem; }
.li th, .li td { text-align: left; padding: .35rem .5rem; vertical-align: top;
                 border-bottom: 1px solid var(--line, #38384a); font-size: .87rem; }
.li .digest { font-family: ui-monospace, Menlo, monospace; font-size: .76rem;
              word-break: break-all; }
.li .ok { color: var(--accent, #b5713a); font-weight: 600; }
.li .bad { color: #d05a5a; font-weight: 600; }
.li .note { border-left: 3px solid var(--line, #55556a); padding: .1rem 0 .1rem .7rem;
            margin: .6rem 0 1.2rem; font-size: .87rem; opacity: .85; }
"""


def _esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _settings(base):
    try:
        rows = object_records.read_collection_records(SETTINGS_COLLECTION,
                                                      base_dir=base)
    except Exception:
        return {}
    return {_text(row.get("key")): _text(row.get("value"))
            for row in rows if _text(row.get("key"))}


def _anchor_to_spec(row):
    """An anchor row as object_ledger_head.verify() wants it.

    The field list comes out of the ROW, which is the decision that lets a
    schema migration happen without every historical anchor turning red.
    """
    try:
        row_count = int(_text(row.get("row_count")) or 0)
    except ValueError:
        row_count = 0
    return {
        "collection": _text(row.get("collection")),
        "digest": _text(row.get("digest")),
        "row_count": row_count,
        "fields": [name for name in _text(row.get("covered_fields")).split(",")
                   if name],
    }


def report(*, base=None):
    """Every anchored ledger, its current head, and each anchor checked.

    One fold, two surfaces, so the page and the JSON cannot disagree.
    """
    base = _base_dir() if base is None else base
    settings = _settings(base)
    # The SAME parser the pass uses (object_notary), not a second one
    # spelled the same way. When these drifted, the page reported an
    # endpoint the pass was correctly ignoring.
    endpoints = object_notary.endpoints_from_setting(
        settings.get(ENDPOINTS_KEY, ""))

    try:
        anchor_rows = object_records.read_collection_records(ANCHORS,
                                                             base_dir=base)
    except Exception:
        anchor_rows = []

    ledgers = {}
    for row in anchor_rows:
        ledgers.setdefault(_text(row.get("collection")), []).append(row)

    entries = []
    total_independent = 0
    for collection in sorted(name for name in ledgers if name):
        rows_for = ledgers[collection]
        specs = [_anchor_to_spec(row) for row in rows_for]

        try:
            schema = object_schemas.get_schema(collection, base_dir=base)
            fields = [_text(f.get("name")) for f in (schema or {}).get("fields") or []
                      if _text(f.get("name")) and _text(f.get("name")) != "_op"]
        except Exception:
            fields = []
        try:
            live = object_records.read_collection_records(collection, base_dir=base)
        except Exception:
            live = []

        checked = []
        for row, spec in zip(rows_for, specs):
            verdict = object_ledger_head.verify(live, spec)
            try:
                count = int(_text(row.get("notary_count")) or 0)
            except ValueError:
                count = 0
            total_independent += count
            checked.append({
                "taken_at": _text(row.get("created_at")),
                "row_count": spec["row_count"],
                "digest": spec["digest"],
                "notary_count": count,
                "notaries": _text(row.get("notaries")),
                "status": _text(row.get("status")),
                "verdict": verdict,
            })
        checked.sort(key=lambda entry: entry["taken_at"], reverse=True)

        entries.append({
            "collection": collection,
            "present_rows": len(live),
            "current_head": (object_ledger_head.head(live, fields,
                                                     collection=collection)
                             if fields else None),
            "anchors": checked,
            "break": object_ledger_head.locate(live, specs),
            "independent_copies": max((c["notary_count"] for c in checked),
                                      default=0),
        })

    anchored_independently = [e for e in entries if e["independent_copies"] > 0]
    return {
        "endpoints": endpoints,
        "ledgers": entries,
        "ledger_count": len(entries),
        "independently_anchored": len(anchored_independently),
        "total_independent_lodgements": total_independent,
        "broken": [e["collection"] for e in entries if e["break"]["broken"]],
        "evidence": bool(anchored_independently),
    }


# --- rendering ---------------------------------------------------------------

def _banner_html(fold):
    broken = fold["broken"]
    if broken:
        names = ", ".join(_esc(name) for name in broken)
        return f"""
<div class="banner weak">
<h2 class="bad">An anchor does not verify</h2>
<p>{names} no longer hashes to what was recorded. That means rows an anchor
was taken over have been changed or removed since. Read the ledger's own
section below for the window it happened in.</p>
</div>"""

    if not fold["ledgers"]:
        return """
<div class="banner weak">
<h2>Nothing is anchored yet</h2>
<p>No anchors have been taken, so there is nothing to verify. The daily
pass (<code>system_publish_head</code>) writes one per ledger; run it by
hand to start.</p>
</div>"""

    if not fold["evidence"]:
        return f"""
<div class="banner weak">
<h2>Anchored, but only here</h2>
<p>Every anchor verifies — and <strong>no independent party holds any of
them</strong>. These rows sit in the same data directory as the ledgers
they describe, under the same permissions, so anybody able to rewrite a
ledger can rewrite its anchor beside it.</p>
<p>That makes this page a useful check against <em>accident</em> and no
check at all against <em>intent</em>. To change that, set
<code>{_esc(ENDPOINTS_KEY)}</code> in settings to one or more notaries run
by somebody else. Then rewriting history means also rewriting a record on
a machine this operator does not control.</p>
</div>"""

    return f"""
<div class="banner">
<h2 class="ok">Verified, and held elsewhere</h2>
<p>Every anchor verifies, and
<strong>{fold['independently_anchored']} of {fold['ledger_count']}</strong>
ledgers have a digest lodged with an independent notary
({fold['total_independent_lodgements']} lodgement{
    '' if fold['total_independent_lodgements'] == 1 else 's'} in all).
Altering the anchored history of those ledgers means also altering a
record held by somebody else.</p>
</div>"""


def _ledger_html(entry):
    head = entry["current_head"]
    head_line = (f'<p class="muted">Now: <strong>{entry["present_rows"]:,}</strong> '
                 f'rows &middot; <span class="digest">{_esc(head["digest"])}'
                 f'</span></p>' if head else
                 '<p class="muted">No schema on this box, so no current head '
                 'can be computed.</p>')

    rows = "".join(
        f'<tr><td><time datetime="{_esc(anchor["taken_at"])}">'
        f'{_esc(anchor["taken_at"])}</time></td>'
        f'<td>{anchor["row_count"]:,}</td>'
        f'<td class="digest">{_esc(anchor["digest"][:16])}&hellip;</td>'
        f'<td>{anchor["notary_count"] or "&mdash;"}</td>'
        f'<td class="{"ok" if anchor["verdict"]["verified"] else "bad"}">'
        f'{_esc(anchor["verdict"]["status"])}</td></tr>'
        for anchor in entry["anchors"][:20])

    window = (f'<p class="note bad">{_esc(entry["break"]["detail"])}</p>'
              if entry["break"]["broken"] else "")

    return f"""
<h2>{_esc(entry["collection"])}</h2>
{head_line}
{window}
<table>
<thead><tr><th>Taken</th><th>Rows</th><th>Digest</th>
<th>Independent copies</th><th>Checked</th></tr></thead>
<tbody>{rows}</tbody></table>
"""


def _page(fold):
    body = "".join(_ledger_html(entry) for entry in fold["ledgers"])
    return f"""
<div class="breadcrumb"><a href="/">Home</a> / Ledger integrity</div>
<h1>Ledger integrity</h1>
<p>Each ledger below has been digested at various points and those digests
recorded. This page recomputes them against the ledgers as they stand now.
A ledger that has grown is expected — appending never disturbs an earlier
prefix, which is what makes an anchor checkable a year later.</p>
{_banner_html(fold)}
{body}
<p class="note">"Verified" means the first N rows still hash to what was
anchored. It does not mean the rows are true, that the ledger is complete,
or that nothing was added since. What it rules out is a row being edited
or removed after the fact — and it rules that out only as far as the
anchor itself is beyond reach of whoever would do the editing.</p>
<p><a href="/ledger-integrity.json">This page as JSON</a> &middot;
<a href="/anchors">every anchor</a></p>
"""


def GET(request):
    request = request or {}
    path = _text(request.get("_path") or "/ledger-integrity").lower()
    fold = report()

    if path.endswith(".json"):
        return {"status": 200,
                "content_type": "application/json; charset=utf-8",
                "body": json.dumps(fold, indent=2, sort_keys=True, default=str)}

    return {
        "status": 200,
        "content_type": "text/html; charset=utf-8",
        "body": f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ledger integrity</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap li">
<header class="app"><h1><a href="/">DBBASIC</a></h1></header>
{_page(fold)}
</div>
<script src="/nav"></script>
</body>
</html>""",
    }


def POST(request):
    return GET(request)
