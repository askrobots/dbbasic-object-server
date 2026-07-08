"""Projects page: a signed-in table of the visitor's own projects.

The browser talks to /collections/projects/records with the visitor's
session cookie, so the permission policy decides what this page can see
and write — the page itself holds no data access.
"""

# Page-specific: the create form's panel wrapper (shared form.stack has no panel chrome).
_STYLE = """
form.create { margin-top: var(--gap); background: var(--panel); border: 1px solid var(--line);
              border-radius: var(--radius-md); padding: var(--pad); display: grid; gap: var(--gap); }
form.create button { justify-self: start; }
"""

_SCRIPT = """
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

async function load() {
  const res = await fetch("/collections/projects/records?limit=200",
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  const body = await res.json();
  const rows = (body.records || []).map((r) =>
    `<tr><td>${esc(r.name)}</td><td><span class="badge positive">${esc(r.status)}</span></td>` +
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
<button type="submit" class="btn primary">Add Project</button>
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
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header class="app"><h1>Projects</h1><div class="who">{who}</div></header>
{body}
</div>
{script}
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
