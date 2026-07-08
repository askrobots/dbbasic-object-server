"""Calendar page over the events collection.

The collection is named events (matching the q9 app); this page lives at
/calendar because /events is the server's built-in event-delivery API.
"""

# Page-unique layout only; everything else comes from the shared /style sheet.
_STYLE = """
form.capture { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius-md);
               padding: var(--pad); display: grid; gap: var(--gap); margin-bottom: var(--gap);
               grid-template-columns: 2fr 1fr 1fr; }
form.capture .btn { justify-self: start; }
form.capture .error { grid-column: 1 / -1; }
tr.past td { color: var(--muted); }
td.when { white-space: nowrap; }
td .purpose { color: var(--positive); font-size: 0.75rem; }
"""

_SCRIPT = """
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

function render(records) {
  const now = new Date().toISOString();
  const sorted = records.slice().sort((a, b) => (a.starts_at || "").localeCompare(b.starts_at || ""));
  const rows = sorted.map((e) => {
    const past = (e.ends_at || e.starts_at || "") < now;
    const when = (e.starts_at || "").replace("T", " ").slice(0, 16);
    const link = e.url ? ` <a href="${esc(e.url)}" rel="noopener noreferrer">link</a>` : "";
    return `<tr class="${past ? "past" : ""}"><td>${esc(e.title)}` +
           `<div class="purpose">${esc(e.purpose)}</div></td>` +
           `<td class="when">${esc(when)}</td><td>${esc(e.location)}${link}</td></tr>`;
  });
  document.getElementById("rows").innerHTML =
    rows.join("") || '<tr><td colspan="3">No events yet.</td></tr>';
}

async function load() {
  const res = await fetch("/collections/events/records?limit=500",
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  const body = await res.json();
  render(body.records || []);
}

document.getElementById("capture-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const fields = event.target.elements;
  const starts = fields["starts"].value;
  const record = {id: crypto.randomUUID(), title: fields["title"].value.trim(),
                  starts_at: starts ? starts + ":00" : "",
                  location: fields["location"].value.trim(),
                  purpose: fields["purpose"].value, owner_id: OWNER_ID};
  const res = await fetch("/collections/events/records", {
    method: "POST", credentials: "same-origin",
    headers: {"content-type": "application/json", accept: "application/json"},
    body: JSON.stringify(record),
  });
  const body = await res.json();
  document.getElementById("form-error").textContent = res.ok ? "" : (body.error || "Save failed");
  if (res.ok) { event.target.reset(); load(); }
});
load();

// Realtime: auto-refresh when this collection changes (another tab, user, or agent).
(function () {
  let _lt = null;
  const reload = () => { clearTimeout(_lt); _lt = setTimeout(load, 150); };
  (function wait() {
    if (window.dbbasicSubscribe) window.dbbasicSubscribe("events", reload);
    else setTimeout(wait, 300);
  })();
})();
"""


def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_calendar served", user_id=user_id or "anonymous")

    if not user_id:
        body = '<p class="hint"><a href="/login?next=/calendar">Sign in</a> to see your events.</p>'
        script = ""
    else:
        body = """
<form class="capture" id="capture-form">
<input name="title" placeholder="Event title" required maxlength="255">
<input name="starts" type="datetime-local" required>
<input name="location" placeholder="Location" maxlength="255">
<select name="purpose">
<option value="meeting">meeting</option>
<option value="workshop">workshop</option>
<option value="recreation">recreation</option>
<option value="others">others</option>
</select>
<button type="submit" class="btn primary">Add Event</button>
<div class="error" id="form-error"></div>
</form>
<table>
<thead><tr><th>Event</th><th>When</th><th>Where</th></tr></thead>
<tbody id="rows"><tr><td colspan="3">loading&hellip;</td></tr></tbody>
</table>
"""
        script = f"<script>const OWNER_ID = {user_id!r};{_SCRIPT}</script>"

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else '<a href="/login?next=/calendar">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Calendar</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header class="app"><h1>Calendar</h1><div class="who">{who}</div></header>
{body}
</div>
{script}
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
