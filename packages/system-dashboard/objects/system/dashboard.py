"""Identity-aware operator dashboard, served as one DBBASIC object.

Anonymous visitors see basic server health. Signed-in admins get live
metrics, inventory, capability posture, and the recent change feed — the
browser polls the admin APIs directly with the visitor's session cookie, so
this page needs no data access of its own and shows exactly what the
caller is allowed to see.
"""

_STYLE = """
:root {
  color-scheme: dark;
  --bg: #0b0b10; --panel: #17171f; --line: #2b2b37;
  --text: #f4f4f7; --muted: #a2a2ad;
  --green: #52d273; --blue: #5aa7ff; --amber: #f1b747; --red: #ff6b6b;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
.wrap { max-width: 1100px; margin: 0 auto; padding: 1.5rem; }
header { display: flex; align-items: baseline; gap: 1rem; margin-bottom: 1.25rem; }
header h1 { font-size: 1.15rem; margin: 0; }
header .who { margin-left: auto; color: var(--muted); font-size: 0.85rem; }
header .who a { color: var(--blue); text-decoration: none; }
.grid { display: grid; gap: 0.75rem; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); }
.tile { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 0.75rem 0.9rem; }
.tile .label { font-size: 0.7rem; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); }
.tile .value { font-size: 1.35rem; font-weight: 600; margin-top: 0.15rem; }
.tile .sub { font-size: 0.75rem; color: var(--muted); }
.ok { color: var(--green); } .warn { color: var(--amber); } .bad { color: var(--red); }
section { margin-top: 1.5rem; }
section h2 { font-size: 0.8rem; letter-spacing: 0.06em; text-transform: uppercase;
             color: var(--muted); margin: 0 0 0.6rem; }
.bars { display: flex; gap: 1.25rem; align-items: flex-end; height: 76px;
        background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
        padding: 0.75rem 1rem 0.5rem; }
.bar { display: flex; flex-direction: column; justify-content: flex-end; align-items: center;
       gap: 0.3rem; width: 64px; height: 100%; }
.bar i { display: block; width: 100%; background: var(--blue); border-radius: 3px 3px 0 0; min-height: 2px; }
.bar span { font-size: 0.68rem; color: var(--muted); }
table { width: 100%; border-collapse: collapse; background: var(--panel);
        border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
th, td { text-align: left; font-size: 0.82rem; padding: 0.45rem 0.75rem;
         border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 500; }
tr:last-child td { border-bottom: 0; }
td.kind { color: var(--blue); white-space: nowrap; }
td.when { color: var(--muted); white-space: nowrap; }
.hint { color: var(--muted); font-size: 0.85rem; background: var(--panel);
        border: 1px solid var(--line); border-radius: 8px; padding: 0.9rem 1rem; }
.hint a { color: var(--blue); }
footer { margin-top: 2rem; color: var(--muted); font-size: 0.75rem; }
"""

_ADMIN_SCRIPT = """
const fmt = (n) => typeof n === "number" ? n.toLocaleString() : (n ?? "-");
const el = (id) => document.getElementById(id);

function setTile(id, value, sub) {
  const tile = el(id);
  if (!tile) return;
  tile.querySelector(".value").textContent = value;
  if (sub !== undefined) tile.querySelector(".sub").textContent = sub;
}

function renderBars(times) {
  const keys = ["avg", "p50", "p95", "p99"];
  const max = Math.max(...keys.map((k) => times[k] || 0), 1);
  for (const key of keys) {
    const bar = el("bar-" + key);
    if (!bar) continue;
    bar.querySelector("i").style.height = Math.max(3, (times[key] || 0) / max * 52) + "px";
    bar.querySelector("span").textContent = key + " " + (times[key] ?? 0) + "ms";
  }
}

async function fetchJson(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(url + " -> " + response.status);
  return response.json();
}

function describeTarget(target) {
  if (!target) return "";
  if (typeof target === "string") return target;
  if (target.object_id) {
    return target.object_id + (target.version_id ? " v" + target.version_id : "");
  }
  if (target.collection) {
    return target.collection + (target.record_id ? "/" + target.record_id : "");
  }
  if (target.package_id) return target.package_id;
  if (target.file) return target.file;
  return Object.values(target).filter((v) => typeof v === "string").join(" ");
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

let changeDetails = [];

function toggleChange(index) {
  const detail = el("change-detail-" + index);
  if (detail) { detail.remove(); return; }
  const row = el("change-row-" + index);
  if (!row) return;
  const detailRow = document.createElement("tr");
  detailRow.id = "change-detail-" + index;
  const cell = document.createElement("td");
  cell.colSpan = 5;
  const pre = document.createElement("pre");
  pre.style.cssText = "margin:0;font-size:0.72rem;color:var(--muted);white-space:pre-wrap;";
  pre.textContent = JSON.stringify(changeDetails[index], null, 2);
  cell.appendChild(pre);
  detailRow.appendChild(cell);
  row.after(detailRow);
}

async function refresh() {
  try {
    const health = await fetchJson("/health?metrics=true");
    setTile("tile-uptime", health.uptime, "pid " + health.pid);
    setTile("tile-requests", fmt(health.requests), health.rps + " req/s");
    const errValue = el("tile-errors").querySelector(".value");
    errValue.textContent = fmt(health.errors);
    errValue.className = "value " + (health.errors > 0 ? "bad" : "ok");
    el("tile-errors").querySelector(".sub").textContent =
      (health.error_rate * 100).toFixed(1) + "% error rate";
    setTile("tile-capacity",
      health.capacity.requests.in_flight + "/" + health.capacity.requests.max,
      "exec " + health.capacity.object_executions.in_flight + "/" +
      health.capacity.object_executions.max);
    renderBars(health.response_time_ms || {});

    const status = await fetchJson("/admin/status");
    const inv = status.inventory || {};
    setTile("tile-objects", fmt(inv.objects), inv.packages + " packages");
    setTile("tile-collections", fmt(inv.collections), inv.schemas + " schemas");
    const enforced = (status.permissions || {}).enforcement_enabled;
    const enfValue = el("tile-enforcement").querySelector(".value");
    enfValue.textContent = enforced ? "ON" : "off";
    enfValue.className = "value " + (enforced ? "ok" : "warn");

    const users = await fetchJson("/admin/identity/users");
    setTile("tile-users", fmt(users.count), "registered users");

    const sessions = await fetchJson("/admin/identity/sessions");
    const active = (sessions.sessions || []).filter((s) => s.active);
    setTile("tile-sessions", fmt(active.length), "active sessions");
    el("sessions-body").innerHTML = active.map((s) =>
      "<tr><td>" + esc(s.label || "-") + "</td><td>" + esc(s.user_id) +
      "</td><td class=when>" + esc((s.created_at || "").replace("T", " ").slice(0, 19)) +
      "</td><td class=when>" + esc((s.expires_at || "").replace("T", " ").slice(0, 19)) +
      "</td></tr>").join("") ||
      "<tr><td colspan=4 class=when>no active sessions</td></tr>";

    const changes = await fetchJson("/admin/changes?limit=12");
    changeDetails = (changes.changes || []).map((change) => change.change || change);
    const rows = (changes.changes || []).map((change, index) => {
      const when = (change.timestamp || "").replace("T", " ").slice(0, 19);
      return "<tr id=change-row-" + index + " style=cursor:pointer " +
        "onclick=toggleChange(" + index + ")><td class=kind>" + esc(change.kind) +
        "</td><td>" + esc(change.summary || change.action) +
        "</td><td>" + esc(describeTarget(change.target)) +
        "</td><td>" + esc(change.actor) + "</td><td class=when>" + when + "</td></tr>";
    });
    el("changes-body").innerHTML = rows.join("") ||
      "<tr><td colspan=5 class=when>no recent changes</td></tr>";
    el("updated").textContent = "updated " + new Date().toLocaleTimeString();
  } catch (error) {
    el("updated").textContent = "refresh failed: " + error.message;
  }
}

refresh();
setInterval(refresh, 10000);
"""


def _tile(tile_id, label, value="-", sub=""):
    return (
        f'<div class="tile" id="{tile_id}"><div class="label">{label}</div>'
        f'<div class="value">{value}</div><div class="sub">{sub}</div></div>'
    )


def GET(request):
    count = int(_state_manager.get("served", 0) or 0) + 1
    _state_manager.set("served", count)

    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    is_admin = "admin" in (identity.get("roles") or [])
    _logger.info(
        "system_dashboard served",
        count=count,
        user_id=user_id or "anonymous",
        admin=is_admin,
    )

    if user_id:
        who = (
            f"signed in as <strong>{user_id}</strong> &middot; "
            '<form method="post" action="/logout" style="display:inline">'
            '<button style="background:none;border:0;color:var(--blue);cursor:pointer;'
            'padding:0;font:inherit">sign out</button></form>'
        )
    else:
        who = '<a href="/login?next=/dashboard">sign in</a> for live metrics'

    if is_admin:
        body = f"""
<div class="grid">
{_tile("tile-uptime", "Uptime")}
{_tile("tile-requests", "Requests")}
{_tile("tile-errors", "Errors")}
{_tile("tile-capacity", "In Flight")}
{_tile("tile-objects", "Objects")}
{_tile("tile-collections", "Collections")}
{_tile("tile-users", "Users")}
{_tile("tile-sessions", "Sessions")}
{_tile("tile-enforcement", "Enforcement")}
</div>
<section>
<h2>Response Time</h2>
<div class="bars">
<div class="bar" id="bar-avg"><i></i><span>avg</span></div>
<div class="bar" id="bar-p50"><i></i><span>p50</span></div>
<div class="bar" id="bar-p95"><i></i><span>p95</span></div>
<div class="bar" id="bar-p99"><i></i><span>p99</span></div>
</div>
</section>
<section>
<h2>Active Sessions</h2>
<table>
<thead><tr><th>Label</th><th>User</th><th>Created</th><th>Expires</th></tr></thead>
<tbody id="sessions-body"><tr><td colspan="4" class="when">loading&hellip;</td></tr></tbody>
</table>
</section>
<section>
<h2>Recent Changes <span style="text-transform:none;letter-spacing:0">(click a row for detail)</span></h2>
<table>
<thead><tr><th>Kind</th><th>Summary</th><th>Target</th><th>Actor</th><th>When</th></tr></thead>
<tbody id="changes-body"><tr><td colspan="5" class="when">loading&hellip;</td></tr></tbody>
</table>
</section>
<footer id="updated">loading&hellip;</footer>
<script>{_ADMIN_SCRIPT}</script>"""
    elif user_id:
        body = (
            '<div class="hint">You are signed in, but live metrics need an '
            "admin role. The server itself is healthy if you can read this "
            "page &mdash; it was rendered by a live object.</div>"
        )
    else:
        body = (
            '<div class="hint">This dashboard is a running DBBASIC object. '
            'The server is <span class="ok">healthy</span> if you can read '
            'this page. <a href="/login?next=/dashboard">Sign in</a> to see '
            "live metrics, inventory, and the change feed. Curious how this "
            'page works? It is one Python file with a <code>GET(request)</code> '
            "method, editable without a deploy.</div>"
        )

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DBBASIC Dashboard</title>
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header>
<h1>DBBASIC Dashboard</h1>
<span class="who">{who}</span>
</header>
{body}
</div>
</body>
</html>"""

    return {"content_type": "text/html; charset=utf-8", "body": html}
