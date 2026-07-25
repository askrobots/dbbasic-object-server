"""site_urls -- every URL this server answers, compiled: /urls.

The sibling of /flow. Routing here has three sources and all of them are
inspectable -- convention (a site_* object's file IS its URL), the
site_routes collection (patterned routes as data), and the views
collection (generated pages declare their route) -- plus the fixed core
API surface, whose paths are constants in http_api_contract by design.
Because none of it hides in code, the site map is compiled, not
maintained: install a package and its URLs appear here; delete a page
object and its convention URL vanishes. A hand-written sitemap is wrong
within a week; this one cannot be.

Each row says WHERE the URL comes from (convention | route | view | core),
what serves it, and -- for routes that shadow or are shadowed -- notes
that convention wins over data (docs/site-routing.md's precedence,
verified the hard way).

Signed-in users only, same courtesy as /flow: the complete map of a
server's surface is reconnaissance data.
"""

import html
import os
from pathlib import Path

import http_api_contract
import object_namespace
import object_records


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _esc(value):
    return html.escape(str(value or ""))


def _safe_records(collection):
    try:
        return object_records.read_collection_records(collection, base_dir=_base_dir())
    except Exception:
        return []


def _convention_pages():
    """site_* objects whose file IS their URL (/notes -> site_notes)."""
    out = []
    for root in object_namespace.get_object_roots():
        site_dir = Path(root) / "site"
        if not site_dir.exists():
            continue
        for path in sorted(site_dir.glob("*.py")):
            name = path.stem
            if name.startswith("_"):
                continue
            url = "/" if name == "home" else "/" + name.replace("_", "-")
            out.append({"url": url, "serves": f"site_{name}", "source": "convention"})
    return out


def _core_api_rows():
    """The fixed platform surface: contract constants, not discovery."""
    rows = [
        (http_api_contract.SEARCH_PATH, "global search"),
        (http_api_contract.MCP_PATH, "MCP: agents operate the server as tools"),
        (http_api_contract.WEBHOOKS_PATH + "/{name}", "raw-body webhooks (webhook_{name} objects)"),
        ("/api/schema/{collection}", "schema as contract: clients build their own UI"),
        ("/collections/{collection}/records", "the JSON records API"),
        ("/objects/{object_id}", "direct object execution"),
        ("/ws", "realtime push (permission-filtered signals)"),
        ("/login", "session login"),
        (http_api_contract.DAEMON_STATUS_PATH, "daemon posture (admin token)"),
        (http_api_contract.DAEMON_SCHEDULER_TASKS_PATH, "scheduler task CRUD (admin token)"),
    ]
    return [{"url": u, "serves": what, "source": "core"} for u, what in rows]


def GET(request):
    identity = request.get("_identity") or {}
    if not identity.get("user_id"):
        body = ('<p class="hint"><a href="/login?next=/urls">Sign in</a> to see the map of '
                "every URL this server answers.</p>")
    else:
        rows = []
        rows += _convention_pages()
        for r in _safe_records("site_routes"):
            rows.append({"url": r.get("pattern", ""), "serves": r.get("object_id", ""),
                         "source": "route"})
        for v in _safe_records("views"):
            if v.get("route"):
                rows.append({"url": v["route"], "serves": f"view: {v.get('id')}",
                             "source": "view"})
        rows += _core_api_rows()

        # Convention beats data: where both claim a URL, say so instead of
        # listing two rows that look equally live.
        convention_urls = {r["url"] for r in rows if r["source"] == "convention"}
        seen = set()
        table = []
        for r in sorted(rows, key=lambda x: (x["url"], x["source"])):
            key = (r["url"], r["serves"])
            if key in seen:
                continue
            seen.add(key)
            shadowed = (r["source"] in ("route", "view")
                        and r["url"] in convention_urls)
            note = ('<span class="warn">shadowed: a convention page object wins for this '
                    'URL</span>' if shadowed else "")
            table.append(
                f"<tr><td><code><a href='{_esc(r['url'])}'>{_esc(r['url'])}</a></code></td>"
                f"<td>{_esc(r['serves'])}</td><td>{_esc(r['source'])}</td><td>{note}</td></tr>")
        body = f"""
<div class="breadcrumb"><a href="/">Home</a> / URLs</div>
<h1>Every URL this server answers</h1>
<p class="muted">Compiled from convention (a page object's file is its URL), the
site_routes and views collections, and the fixed core API. Nothing here is
hand-maintained, so nothing here can be stale. {len(table)} entries.</p>
<table><thead><tr><th>URL</th><th>serves</th><th>source</th><th></th></tr></thead>
<tbody>{''.join(table)}</tbody></table>
<p class="muted">Precedence when sources overlap: convention page objects win, then
site_routes patterns match as fallback (docs/site-routing.md).</p>
"""

    return {"content_type": "text/html; charset=utf-8", "body": f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>URLs</title>
<link rel="stylesheet" href="/style">
<style>
.wrap {{ max-width: 960px; margin: 0 auto; padding: 1.25rem; }}
table {{ width: 100%; border-collapse: collapse; }}
td, th {{ text-align: left; padding: 0.35rem 0.6rem; border-bottom: 1px solid var(--line, #333); }}
.muted {{ color: var(--muted, #999); }}
.warn {{ color: var(--warning, #f1b747); font-size: 0.8rem; }}
</style>
</head>
<body>
<script src="/nav"></script>
<div class="wrap">{body}</div>
</body>
</html>"""}
