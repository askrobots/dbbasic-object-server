"""Notes page: the visitor's own notes as cards, with quick capture and search.

The browser talks to /collections/notes/records and /api/search with the
visitor's session cookie, so the permission policy decides what this page
can see and write — the page itself holds no data access.
"""

# Page-unique layout only; everything else comes from the shared /style sheet.
_STYLE = """
.card { white-space: pre-wrap; }
form.capture { background: var(--panel); border: 1px solid var(--line);
               border-radius: var(--radius-md); padding: var(--pad); margin-bottom: var(--gap); }
#search-box { margin-bottom: var(--gap); }
"""

_SCRIPT = """
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
let projectNames = {};

function renderCards(records) {
  const cards = records.map((note) => {
    const bits = [];
    if (note.project_id) bits.push(esc(projectNames[note.project_id] || note.project_id));
    if (note.is_public === "true") bits.push("public");
    bits.push(`<a href="/notes/${encodeURIComponent(note.id)}">open</a>`);
    return `<div class="card">${esc(note.content)}` +
           `<div class="meta">${bits.join(" \\u00b7 ")}</div></div>`;
  });
  document.getElementById("cards").innerHTML =
    cards.join("") || '<p class="hint">No notes yet.</p>';
}

async function loadProjects() {
  const res = await fetch("/collections/projects/records?limit=200",
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) return;
  const body = await res.json();
  const select = document.getElementById("project-select");
  for (const project of body.records || []) {
    projectNames[project.id] = project.name;
    const option = document.createElement("option");
    option.value = project.id;
    option.textContent = project.name;
    select.appendChild(option);
  }
}

async function load() {
  const res = await fetch("/collections/notes/records?limit=200",
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  const body = await res.json();
  renderCards((body.records || []).slice().reverse());
}

async function search(event) {
  const query = event.target.value.trim();
  if (!query) { load(); return; }
  const res = await fetch(`/api/search?q=${encodeURIComponent(query)}&collections=notes&limit=50`,
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) return;
  const body = await res.json();
  renderCards((body.results || {}).notes || []);
}

async function create(event) {
  event.preventDefault();
  const form = event.target;
  const fields = form.elements;
  const record = {id: crypto.randomUUID(), content: fields["content"].value.trim(),
                  owner_id: OWNER_ID, is_public: "false"};
  if (fields["project"].value) record.project_id = fields["project"].value;
  const res = await fetch("/collections/notes/records", {
    method: "POST", credentials: "same-origin",
    headers: {"content-type": "application/json", accept: "application/json"},
    body: JSON.stringify(record),
  });
  const body = await res.json();
  document.getElementById("form-error").textContent = res.ok ? "" : (body.error || "Save failed");
  if (res.ok) { form.reset(); load(); }
}

document.getElementById("capture-form").addEventListener("submit", create);
document.getElementById("search-box").addEventListener("input", search);
loadProjects();
load();
"""


def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_notes served", user_id=user_id or "anonymous")

    if not user_id:
        body = '<p class="hint"><a href="/login?next=/notes">Sign in</a> to see your notes.</p>'
        script = ""
    else:
        body = """
<form class="capture stack" id="capture-form">
<textarea name="content" placeholder="Write a note&hellip;" rows="3" required></textarea>
<select name="project" id="project-select"><option value="">No project</option></select>
<button type="submit" class="btn primary">Save Note</button>
<div class="error" id="form-error"></div>
</form>
<input class="search" id="search-box" placeholder="Search notes&hellip;" autocomplete="off">
<div class="cards" id="cards"><p class="hint">loading&hellip;</p></div>
"""
        script = f"<script>const OWNER_ID = {user_id!r};{_SCRIPT}</script>"

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else '<a href="/login?next=/notes">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Notes</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap narrow">
<header class="app"><h1>Notes</h1><div class="who">{who}</div></header>
{body}
</div>
{script}
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
