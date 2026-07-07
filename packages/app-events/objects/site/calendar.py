"""Calendar page over the events collection.

The collection is named events (matching the q9 app); this page lives at
/calendar because /events is the server's built-in event-delivery API.
"""

_STYLE = """
:root { color-scheme: dark; --bg: #0b0b10; --panel: #17171f; --line: #2b2b37;
        --text: #f4f4f7; --muted: #a2a2ad; --blue: #5aa7ff; --green: #52d273;
        --red: #ff6b6b; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
.wrap { max-width: 860px; margin: 0 auto; padding: 1.5rem; }
header { display: flex; align-items: baseline; gap: 1rem; margin-bottom: 1.25rem; }
header h1 { font-size: 1.15rem; margin: 0; }
header .who { margin-left: auto; color: var(--muted); font-size: 0.85rem; }
a { color: var(--blue); text-decoration: none; }
form.capture { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
               padding: 1rem; display: grid; gap: 0.6rem; margin-bottom: 1rem;
               grid-template-columns: 2fr 1fr 1fr; }
form.capture input, form.capture select {
  background: var(--bg); color: var(--text); border: 1px solid var(--line);
  border-radius: 6px; padding: 0.45rem 0.6rem; font: inherit; width: 100%; }
form.capture button { background: var(--blue); color: #0b0b10; border: 0; border-radius: 6px;
                      padding: 0.5rem 1rem; font: inherit; font-weight: 600; cursor: pointer;
                      justify-self: start; }
table { width: 100%; border-collapse: collapse; background: var(--panel);
        border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
th, td { text-align: left; font-size: 0.85rem; padding: 0.5rem 0.75rem;
         border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 500; }
tr:last-child td { border-bottom: 0; }
tr.past td { color: var(--muted); }
td.when { white-space: nowrap; }
td .purpose { color: var(--green); font-size: 0.75rem; }
.hint { color: var(--muted); font-size: 0.85rem; background: var(--panel);
        border: 1px solid var(--line); border-radius: 8px; padding: 0.9rem 1rem; }
.error { color: var(--red); font-size: 0.85rem; min-height: 1.2rem; grid-column: 1 / -1; }
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
<button type="submit">Add Event</button>
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
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header><h1>Calendar</h1><div class="who">{who}</div></header>
{body}
</div>
{script}
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
