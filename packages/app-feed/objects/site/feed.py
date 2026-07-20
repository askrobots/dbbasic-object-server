"""64 - Feed page: a client-rendered view over an already-composed,
already-permission-gated read.

No new data access of its own. GET /api/feed (object_server.py's
_handle_feed) does the entire composition -- who the viewer follows, then
each declared feed-source collection's own permission-gated
/collections/{c}/records read filtered to that follow set -- so this page
only has to fetch that one endpoint with the visitor's own session cookie
and render the result. Same identity-aware, browser-polls-its-own-API
pattern as app-dashboard/objects/site/dashboard.py and
app-worker/objects/site/profile.py, and the same shared chrome (/style,
/nav) as every other /style page in this codebase.

Served at /feed by the single-segment site-object convention
(/feed -> site_feed), the same convention app-dashboard's site_dashboard
(-> /dashboard) and app-activity's site_activity (-> /activity) already
rely on -- no site_routes record is seeded or required. The JSON
composition itself lives at /api/feed, not bare /feed, because the bare
path is this page's own route: mirrors the existing /api/activity (data)
vs /activity (page, site_activity) split in this same dispatch, which the
plan/vocabulary/64-feed-spec.md's own citation of activity as feed's
closest sibling primitive makes the natural precedent to follow rather
than colliding both surfaces on one path.

Not rendered for anonymous visitors (per the spec's Surfaces section) --
an anonymous request to /api/feed comes back {authenticated: false,
items: []} rather than an error, and this page shows a sign-in prompt in
that case instead of an empty list, so "nothing to show yet" and "sign in
to see your feed" never look identical.
"""

_STYLE = """
.feedcard { border: 1px solid var(--line, #38384a); border-radius: 8px; padding: 0.9rem 1rem;
            margin-bottom: 0.75rem; }
.feedcard .feedtitle { font-weight: 600; margin-bottom: 0.2rem; }
.feedcard .feedtitle a { color: inherit; text-decoration: none; }
.feedcard .feedtitle a:hover { text-decoration: underline; }
.feedcard .feedsummary { color: var(--text); white-space: pre-wrap; word-break: break-word; }
.feedcard .feedmeta { color: var(--muted); font-size: 0.8rem; margin-top: 0.35rem; }
.feedmeta .coll { text-transform: uppercase; letter-spacing: 0.03em; }
.feed-truncated { color: var(--muted); font-size: 0.8rem; margin-bottom: 0.75rem; }
"""

_SCRIPT = r"""
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const el = (id) => document.getElementById(id);

// Only articles is wired as a feed source in this slice (see
// packages/app-articles/schemas/articles.json's blocks.feed); a future
// feed-source collection with no entry here still renders, just without
// a clickable title, same "degrade, don't error" posture as the server
// side of this feature.
const SOURCE_URL = {
  articles: (id) => "/articles/" + encodeURIComponent(id),
};

function relDate(iso) {
  if (!iso) return "";
  const d = new Date(iso); if (isNaN(d)) return String(iso);
  const ms = Date.now() - d.getTime();
  if (ms < 0) return d.toLocaleDateString(undefined, {month: "short", day: "numeric"});
  if (ms < 3600000) { const m = Math.floor(ms / 60000); return m < 1 ? "just now" : m + "m ago"; }
  if (ms < 86400000) return Math.floor(ms / 3600000) + "h ago";
  if (ms < 7 * 86400000) return Math.floor(ms / 86400000) + "d ago";
  return d.toLocaleDateString(undefined, {month: "short", day: "numeric"});
}

function feedCard(item) {
  const urlFn = SOURCE_URL[item.source_collection];
  const url = urlFn ? urlFn(item.source_id) : null;
  const titleText = esc(item.summary || item.source_id || "(untitled)");
  const title = url
    ? '<a href="' + esc(url) + '">' + titleText + "</a>"
    : titleText;
  return '<div class="feedcard">'
    + '<div class="feedtitle">' + title + "</div>"
    + '<div class="feedmeta">'
    + '<span class="who">' + esc(item.author_id || "unknown") + "</span> &middot; "
    + '<span class="when">' + esc(relDate(item.time)) + "</span> &middot; "
    + '<span class="coll">' + esc(item.source_collection || "") + "</span>"
    + "</div></div>";
}

async function loadFeed() {
  let res;
  try {
    res = await fetch("/api/feed", {credentials: "same-origin", headers: {accept: "application/json"}});
  } catch (e) {
    el("feed-body").innerHTML = '<p class="hint">Could not load your feed.</p>';
    return;
  }
  if (!res.ok) {
    el("feed-body").innerHTML = '<p class="hint">Could not load your feed.</p>';
    return;
  }
  const body = await res.json();
  if (body.enabled === false) {
    el("feed-body").innerHTML = '<p class="hint">The feed is turned off right now.</p>';
    return;
  }
  const items = body.items || [];
  let html = "";
  if (body.truncated_following) {
    html += '<div class="feed-truncated">You follow more accounts than one feed read can '
      + "scan at once, so this page only covers the first ones.</div>";
  }
  html += items.length
    ? items.map(feedCard).join("")
    : '<p class="hint">Nothing yet from the accounts you follow.</p>';
  el("feed-body").innerHTML = html;
}

loadFeed();

// Realtime: refresh when a declared feed-source collection changes. Only
// articles is wired in this slice; a future source collection just adds
// its name here alongside its blocks.feed schema entry.
(function () {
  let _lt = null;
  const reload = () => { clearTimeout(_lt); _lt = setTimeout(loadFeed, 150); };
  (function wait() {
    if (window.dbbasicSubscribe) {
      window.dbbasicSubscribe("follows", reload);
      window.dbbasicSubscribe("articles", reload);
    } else setTimeout(wait, 300);
  })();
})();
"""


def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_feed served", user_id=user_id or "anonymous")

    if not user_id:
        body = (
            '<div class="breadcrumb">Home / Feed</div>'
            '<div class="pagehead"><h1>Feed</h1></div>'
            '<div class="hint">Your feed shows recent public posts from the accounts you '
            'follow, once you sign in. <a href="/login?next=/feed">Sign in</a> to see yours.'
            "</div>"
        )
        script = ""
    else:
        body = """
<div class="breadcrumb"><a href="/">Home</a> / Feed</div>
<div class="pagehead"><h1>Feed</h1></div>
<div id="feed-body"><div class="state">loading&hellip;</div></div>
"""
        script = f"<script>{_SCRIPT}</script>"

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else '<a href="/login?next=/feed">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Feed</title>
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
