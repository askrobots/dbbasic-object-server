"""Activity feed page -- served at /activity, showing the signed-in user's
own recent activity: a client-rendered fold over the record-change ledger.

The server renders only the thin shell (breadcrumb, /style, /nav); the
browser fetches /api/activity (scoped to the caller by session, see
object_activity.recent_activity and object_server._handle_activity) and
renders rows "actor ACTION collection title  relative-time", reusing the
same .listrow markup/CSS the /list generator uses (app-theme/objects/site/
list.py) so a feed row looks like any other row in the app, and the same
relative-time algorithm as that generator's `relDate` helper (not imported
-- list.py doesn't export it -- but copied verbatim; see that file's
docstring for the shape being matched).

No live websocket subscription: the feed can include any collection that
has a record-change log, and that set grows as packages install. Rather
than hardcode (and keep in sync) a list of "high-traffic" collections to
subscribe to -- which would silently miss activity in every collection not
named -- this polls /api/activity every 30s. Simpler, and correct for any
collection without the page needing to know the collection set.
"""

_STYLE = """
.feed .listrow .rowtitle .verb { color: var(--accent-strong); }
.feed .listrow .rowtitle .coll { color: var(--muted); font-weight: 400; }
"""

_SCRIPT = r"""
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const el = (id) => document.getElementById(id);

// Same relative-time shape as app-theme/objects/site/list.py's relDate.
function relDate(iso) {
  if (!iso) return "";
  const d = new Date(iso); if (isNaN(d)) return "";
  const ms = Date.now() - d.getTime();
  if (ms < 3600000) { const m = Math.floor(ms / 60000); return m < 1 ? "just now" : m + "m ago"; }
  if (ms < 86400000) return Math.floor(ms / 3600000) + "h ago";
  if (ms < 7 * 86400000) return Math.floor(ms / 86400000) + "d ago";
  return d.toLocaleDateString(undefined, {month: "short", day: "numeric"});
}

// Best-effort permalink for collections known (by convention, see each
// package's docstring) to have a /collection/{id} record page. Not
// exhaustive -- collections missing here just render as plain text.
const RECORD_LINKS = {
  notes: (id) => "/notes/" + id,
  invoices: (id) => "/invoices/" + id,
  articles: (id) => "/articles/" + id,
  views: (id) => "/views/" + id,
};

function row(entry) {
  const actor = entry.actor || "unattributed";
  const verb = String(entry.action || "").toUpperCase();
  const av = actor.trim().charAt(0).toUpperCase() || "?";
  const link = RECORD_LINKS[entry.collection];
  const titleHtml = link
    ? '<a href="' + esc(link(entry.record_id)) + '" target="_blank" rel="noopener">' + esc(entry.title) + '</a>'
    : esc(entry.title);
  return '<div class="listrow"><div class="av">' + esc(av) + '</div><div class="body">'
    + '<div class="rowtitle">' + esc(actor) + ' <span class="verb">' + esc(verb) + '</span> '
    + '<span class="coll">' + esc(entry.collection) + '</span> ' + titleHtml + '</div>'
    + '<div class="rowmeta"><span class="when">' + esc(relDate(entry.timestamp)) + '</span></div>'
    + '</div></div>';
}

async function load() {
  const res = await fetch("/api/activity?limit=50",
    {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) { el("feed").innerHTML = '<div class="state">Could not load activity.</div>'; return; }
  const body = await res.json();
  const entries = body.activity || [];
  el("feed").innerHTML = entries.length
    ? entries.map(row).join("")
    : '<div class="state">Nothing yet.</div>';
}

load();
setInterval(load, 30000);
"""


def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_activity served", user_id=user_id or "anonymous")

    if not user_id:
        body = '<p class="hint"><a href="/login?next=/activity">Sign in</a> to see your activity.</p>'
        script = ""
    else:
        body = """
<div class="breadcrumb"><a href="/">Home</a> / Activity</div>
<div class="pagehead"><h1>Activity</h1></div>
<div class="feed" id="feed"><div class="state">loading&hellip;</div></div>
"""
        script = f"<script>{_SCRIPT}</script>"

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else '<a href="/login?next=/activity">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Activity</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header class="app"><h1><a href="/">DBBASIC</a></h1><div class="who">{who}</div></header>
{body}
</div>
{script}
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
