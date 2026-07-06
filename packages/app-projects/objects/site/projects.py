"""Projects page: a signed-in table of the visitor's own projects.

The browser talks to /collections/projects/records with the visitor's
session cookie, so the permission policy decides what this page can see
and write — the page itself holds no data access.
"""

_STYLE = """
:root { color-scheme: dark; --bg: #0b0b10; --panel: #17171f; --line: #2b2b37;
        --text: #f4f4f7; --muted: #a2a2ad; --blue: #5aa7ff; --green: #52d273; --red: #ff6b6b; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
.wrap { max-width: 860px; margin: 0 auto; padding: 1.5rem; }
header { display: flex; align-items: baseline; gap: 1rem; margin-bottom: 1.25rem; }
header h1 { font-size: 1.15rem; margin: 0; }
header .who { margin-left: auto; color: var(--muted); font-size: 0.85rem; }
header .who a, a { color: var(--blue); text-decoration: none; }
table { width: 100%; border-collapse: collapse; background: var(--panel);
        border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
th, td { text-align: left; font-size: 0.85rem; padding: 0.5rem 0.75rem;
         border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 500; }
tr:last-child td { border-bottom: 0; }
td.status { color: var(--green); white-space: nowrap; }
form.create { margin-top: 1.25rem; background: var(--panel); border: 1px solid var(--line);
              border-radius: 8px; padding: 1rem; display: grid; gap: 0.6rem; }
form.create input, form.create textarea, form.create select {
  background: var(--bg); color: var(--text); border: 1px solid var(--line);
  border-radius: 6px; padding: 0.45rem 0.6rem; font: inherit; width: 100%; }
form.create button { background: var(--blue); color: #0b0b10; border: 0; border-radius: 6px;
                     padding: 0.5rem 1rem; font: inherit; font-weight: 600; cursor: pointer;
                     justify-self: start; }
.hint { color: var(--muted); font-size: 0.85rem; background: var(--panel);
        border: 1px solid var(--line); border-radius: 8px; padding: 0.9rem 1rem; }
.error { color: var(--red); font-size: 0.85rem; min-height: 1.2rem; }
"""

_SCRIPT = """
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

async function load() {
  const res = await fetch("/collections/projects/records?limit=200",
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  const body = await res.json();
  const rows = (body.records || []).map((r) =>
    `<tr><td>${esc(r.name)}</td><td class="status">${esc(r.status)}</td>` +
    `<td>${esc(r.description)}</td></tr>`);
  document.getElementById("rows").innerHTML =
    rows.join("") || '<tr><td colspan="3">No projects yet.</td></tr>';
}

async function create(event) {
  event.preventDefault();
  const form = event.target;
  const fields = form.elements;
  const record = {
    id: crypto.randomUUID(),
    name: fields["name"].value.trim(),
    description: fields["description"].value.trim(),
    status: fields["status"].value,
    owner_id: OWNER_ID,
  };
  const res = await fetch("/collections/projects/records", {
    method: "POST", credentials: "same-origin",
    headers: {"content-type": "application/json", accept: "application/json"},
    body: JSON.stringify(record),
  });
  const body = await res.json();
  document.getElementById("form-error").textContent = res.ok ? "" : (body.error || "Create failed");
  if (res.ok) { form.reset(); load(); }
}

document.getElementById("create-form").addEventListener("submit", create);
load();
"""


def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_projects served", user_id=user_id or "anonymous")

    if not user_id:
        body = '<p class="hint"><a href="/login?next=/projects">Sign in</a> to see your projects.</p>'
        script = ""
    else:
        body = """
<table>
<thead><tr><th>Name</th><th>Status</th><th>Description</th></tr></thead>
<tbody id="rows"><tr><td colspan="3">loading&hellip;</td></tr></tbody>
</table>
<form class="create" id="create-form">
<input name="name" placeholder="Project name" required maxlength="120">
<textarea name="description" placeholder="Description" rows="2"></textarea>
<select name="status">
<option value="active">active</option>
<option value="completed">completed</option>
<option value="on_hold">on hold</option>
</select>
<button type="submit">Add Project</button>
<div class="error" id="form-error"></div>
</form>
"""
        script = f"<script>const OWNER_ID = {user_id!r};{_SCRIPT}</script>"

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else '<a href="/login?next=/projects">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Projects</title>
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header><h1>Projects</h1><div class="who">{who}</div></header>
{body}
</div>
{script}
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
