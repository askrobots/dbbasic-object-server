"""User home dashboard: a client-rendered fold over data this codebase
already has, not a new data model.

No new schema, no new collection, no new write path. Every number on this
page is a sum or a count over an *existing* collection's records, fetched
from the browser with the caller's own session cookie -- so permissions
and row-filters (see each source collection's own permissions/rules.json)
govern what shows up, and this object holds no data access of its own.
Same identity-aware, browser-polls-its-own-APIs pattern as
packages/app-activity/objects/site/activity.py and
packages/app-worker/objects/site/profile.py, and the same shared chrome
(/style, /nav) as every other /style page in this codebase.

This is deliberately NOT packages/system-dashboard/objects/system/
dashboard.py -- that object is the *operator* view (server health,
inventory, admin-only metrics, no shared chrome, its own inline dark
palette). This is the signed-in *user's own* dashboard: AI spend, task
counts, recent activity, and a couple of one-line collection counts, all
scoped to what that one user can see. It is served at /dashboard by the
single-segment site-object convention (/dashboard -> site_dashboard),
the same convention app-activity's site_activity (-> /activity) and
app-tasks's site_tasks (-> /tasks) already rely on -- no site_routes
record is seeded or required.

Ported from a private predecessor-system audit, not part of this repo:
that system's dashboard folded three things into one summary -- a wallet/
billing balance, AI/API usage, and task stats. Billing is infra-deferred
in this codebase (no Stripe integration is built), so this v1 ports only
the AI-usage and task-stats halves, plus the recent-activity feed
(already its own endpoint here, /api/activity) and one-line counts for a
few other collections that already have pages. No wallet balance, no
charts, no new metrics beyond simple sums and counts -- faithful and
minimal.
"""

_STYLE = """
.stats { display: grid; gap: var(--gap); grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); }
.stats .card { text-decoration: none; color: var(--text); }
.stats .card:hover { border-color: var(--accent); }
.stats .card .value { font-size: 1.4rem; font-weight: 700; }
.stats .card .label { color: var(--muted); font-size: 0.75rem; text-transform: uppercase;
                       letter-spacing: 0.03em; margin-top: 0.2rem; }
.dashsection { margin-top: 1.75rem; }
.dashsection h2 { font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.04em;
                   color: var(--muted); margin: 0 0 0.6rem; }
.feed .listrow .rowtitle .verb { color: var(--accent-strong); }
.feed .listrow .rowtitle .coll { color: var(--muted); font-weight: 400; }
"""

_SCRIPT = r"""
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const el = (id) => document.getElementById(id);
const money = (cents) => "$" + (Number(cents || 0) / 100).toFixed(2);

async function fetchJson(url) {
  try {
    const res = await fetch(url, {credentials: "same-origin", headers: {accept: "application/json"}});
    if (!res.ok) return null;
    return await res.json();
  } catch (e) {
    return null;
  }
}

// Same relative-time shape as app-theme/objects/site/list.py's relDate,
// copied verbatim -- see app-activity/objects/site/activity.py's docstring
// for why (list.py doesn't export it).
function relDate(iso) {
  if (!iso) return "";
  const d = new Date(iso); if (isNaN(d)) return "";
  const ms = Date.now() - d.getTime();
  if (ms < 3600000) { const m = Math.floor(ms / 60000); return m < 1 ? "just now" : m + "m ago"; }
  if (ms < 86400000) return Math.floor(ms / 3600000) + "h ago";
  if (ms < 7 * 86400000) return Math.floor(ms / 86400000) + "d ago";
  return d.toLocaleDateString(undefined, {month: "short", day: "numeric"});
}

function statTile(href, value, label) {
  return '<a class="card" href="' + esc(href) + '"><div class="value">' + esc(value) +
    '</div><div class="label">' + esc(label) + '</div></a>';
}

// --- AI spend: fold ai_usage.cost_cents, grouped by model --------------
async function loadAiUsage() {
  const body = await fetchJson("/collections/ai_usage/records?limit=500");
  const records = (body && body.records) || [];
  if (!records.length) {
    el("ai-usage").innerHTML = '<div class="state">No AI usage yet.</div>';
    return;
  }
  let totalCents = 0;
  let unpriced = 0;
  const byModel = {};
  for (const r of records) {
    if (r.cost_cents === null || r.cost_cents === undefined || r.cost_cents === "") {
      unpriced += 1;
    } else {
      totalCents += Number(r.cost_cents) || 0;
    }
    const key = r.model || "unknown";
    const stat = byModel[key] || (byModel[key] = {calls: 0, cents: 0});
    stat.calls += 1;
    stat.cents += Number(r.cost_cents) || 0;
  }
  const rows = Object.entries(byModel)
    .sort((a, b) => b[1].cents - a[1].cents)
    .map(([model, stat]) =>
      "<tr><td>" + esc(model) + "</td><td class=num>" + stat.calls +
      "</td><td class=num>" + money(stat.cents) + "</td></tr>")
    .join("");
  const note = unpriced
    ? '<div class="meta" style="margin-top:0.4rem">' + unpriced +
      " call" + (unpriced === 1 ? "" : "s") + " with no matched price, counted as $0</div>"
    : "";
  el("ai-usage").innerHTML =
    '<div class="stats"><div class="card"><div class="value">' + money(totalCents) +
    '</div><div class="label">Total AI Spend &middot; ' + records.length + " call" +
    (records.length === 1 ? "" : "s") + "</div></div></div>" + note +
    '<table style="margin-top:0.75rem"><thead><tr><th>Model</th><th class=num>Calls</th>' +
    "<th class=num>Cost</th></tr></thead><tbody>" + rows + "</tbody></table>";
}

// --- Task stats: fold tasks.status into counts per status --------------
const TASK_STATUS_ORDER = ["draft", "open", "assigned", "waiting_on_client",
  "approved", "disputed", "cancelled"];

async function loadTasks() {
  const body = await fetchJson("/collections/tasks/records?limit=500");
  const records = (body && body.records) || [];
  if (!records.length) {
    el("task-stats").innerHTML = '<div class="state">No tasks yet.</div>';
    return;
  }
  const counts = {};
  for (const r of records) {
    const status = r.status || "unknown";
    counts[status] = (counts[status] || 0) + 1;
  }
  const statuses = TASK_STATUS_ORDER.filter((s) => counts[s])
    .concat(Object.keys(counts).filter((s) => !TASK_STATUS_ORDER.includes(s)));
  el("task-stats").innerHTML = '<div class="stats">' +
    statuses.map((s) => statTile("/tasks", counts[s], s.replace(/_/g, " "))).join("") +
    "</div>";
}

// --- Recent activity: fold /api/activity, same row shape as app-activity
function activityRow(entry) {
  const actor = entry.actor || "unattributed";
  const verb = String(entry.action || "").toUpperCase();
  const av = actor.trim().charAt(0).toUpperCase() || "?";
  return '<div class="listrow"><div class="av">' + esc(av) + '</div><div class="body">'
    + '<div class="rowtitle">' + esc(actor) + ' <span class="verb">' + esc(verb) + '</span> '
    + '<span class="coll">' + esc(entry.collection) + '</span> ' + esc(entry.title) + '</div>'
    + '<div class="rowmeta"><span class="when">' + esc(relDate(entry.timestamp)) + '</span></div>'
    + '</div></div>';
}

async function loadActivity() {
  const body = await fetchJson("/api/activity?limit=8");
  const entries = (body && body.activity) || [];
  el("activity").innerHTML = entries.length
    ? entries.map(activityRow).join("")
    : '<div class="state">Nothing yet.</div>';
}

// --- One-line counts for a few other collections, opportunistic --------
// Not a dependency: dbbasic-package.json declares none of these, and a
// missing/empty collection just leaves its tile out, same posture as
// app-worker/objects/site/profile.py's article fetch.
const OTHER_COLLECTIONS = [
  {collection: "notes", label: "Notes", href: "/notes"},
  {collection: "contacts", label: "Contacts", href: "/contacts"},
  {collection: "invoices", label: "Invoices", href: "/invoices"},
];

async function loadOtherCounts() {
  const bodies = await Promise.all(
    OTHER_COLLECTIONS.map((spec) => fetchJson("/collections/" + spec.collection + "/records?limit=1")));
  const tiles = [];
  bodies.forEach((body, i) => {
    if (body && typeof body.total === "number") {
      tiles.push(statTile(OTHER_COLLECTIONS[i].href, body.total, OTHER_COLLECTIONS[i].label));
    }
  });
  const section = el("other-counts-section");
  if (!tiles.length) { section.style.display = "none"; return; }
  section.style.display = "block";
  el("other-counts").innerHTML = '<div class="stats">' + tiles.join("") + "</div>";
}

loadAiUsage();
loadTasks();
loadActivity();
loadOtherCounts();
"""


def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_dashboard served", user_id=user_id or "anonymous")

    if not user_id:
        body = (
            '<div class="breadcrumb">Home / Dashboard</div>'
            '<div class="pagehead"><h1>Dashboard</h1></div>'
            '<div class="hint">Your dashboard shows AI spend, task counts, and recent '
            "activity once you sign in &mdash; each fetched with your own session, so "
            'you only ever see your own data. <a href="/login?next=/dashboard">Sign in'
            "</a> to see yours.</div>"
        )
        script = ""
    else:
        body = """
<div class="breadcrumb"><a href="/">Home</a> / Dashboard</div>
<div class="pagehead"><h1>Dashboard</h1></div>

<div class="dashsection">
<h2>AI Spend</h2>
<div id="ai-usage"><div class="state">loading&hellip;</div></div>
</div>

<div class="dashsection">
<h2>Tasks</h2>
<div id="task-stats"><div class="state">loading&hellip;</div></div>
</div>

<div class="dashsection">
<h2>Recent Activity</h2>
<div class="feed" id="activity"><div class="state">loading&hellip;</div></div>
</div>

<div class="dashsection" id="other-counts-section" style="display:none">
<h2>Also Yours</h2>
<div id="other-counts"></div>
</div>
"""
        script = f"<script>{_SCRIPT}</script>"

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else '<a href="/login?next=/dashboard">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dashboard</title>
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
