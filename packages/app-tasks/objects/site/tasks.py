"""Tasks page: the visitor's tasks as a table with quick add and status moves.

Status buttons offer only the transitions the schema allows from the
row's current status — and the server enforces the same map, so the UI
is a convenience, not the boundary.
"""

# Page-specific layout the shared sheet lacks: the quick-add form's column grid.
_STYLE = """
form.capture { background: var(--panel); border: 1px solid var(--line);
               border-radius: var(--radius-md); padding: var(--pad); display: grid;
               gap: var(--gap); margin-bottom: var(--gap);
               grid-template-columns: 2fr 1fr 1fr auto; align-items: start; }
form.capture .error { grid-column: 1 / -1; }
"""

_SCRIPT = """
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const TRANSITIONS = {open: ["assigned", "cancelled"],
                     assigned: ["waiting_on_client", "open", "cancelled"],
                     waiting_on_client: ["approved", "disputed", "assigned"],
                     disputed: ["assigned", "cancelled"]};
const BADGE = {approved: "positive", assigned: "warning", waiting_on_client: "warning",
               disputed: "danger", cancelled: "danger"};
let projectNames = {};

function render(records) {
  const rows = records.map((t) => {
    const moves = (TRANSITIONS[t.status] || []).map((next) =>
      `<button class="move btn ghost sm" data-id="${esc(t.id)}" data-next="${esc(next)}">${esc(next)}</button>`);
    return `<tr><td>${esc(t.title)}<div style="color:var(--muted);font-size:0.75rem">` +
           `${esc(projectNames[t.project_id] || t.project_id || "")}</div></td>` +
           `<td><span class="badge ${BADGE[t.status] || ""}">${esc(t.status)}</span><br>${moves.join("")}</td>` +
           `<td>${esc(t.urgency)}</td><td>${esc(t.due_date)}</td><td>${esc(t.assigned_to)}</td></tr>`;
  });
  document.getElementById("rows").innerHTML =
    rows.join("") || '<tr><td colspan="5">No tasks yet.</td></tr>';
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
  const res = await fetch("/collections/tasks/records?limit=200",
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  const body = await res.json();
  render(body.records || []);
}

document.getElementById("rows").addEventListener("click", async (event) => {
  const button = event.target.closest("button.move");
  if (!button) return;
  const res = await fetch(`/collections/tasks/records/${button.dataset.id}`, {
    method: "PUT", credentials: "same-origin",
    headers: {"content-type": "application/json", accept: "application/json"},
    body: JSON.stringify({status: button.dataset.next}),
  });
  const body = await res.json();
  document.getElementById("page-error").textContent = res.ok ? "" : (body.error || "Move failed");
  if (res.ok) load();
});

document.getElementById("capture-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const fields = event.target.elements;
  const record = {id: crypto.randomUUID(), title: fields["title"].value.trim(),
                  urgency: fields["urgency"].value, owner_id: OWNER_ID};
  if (fields["project"].value) record.project_id = fields["project"].value;
  const res = await fetch("/collections/tasks/records", {
    method: "POST", credentials: "same-origin",
    headers: {"content-type": "application/json", accept: "application/json"},
    body: JSON.stringify(record),
  });
  const body = await res.json();
  document.getElementById("page-error").textContent = res.ok ? "" : (body.error || "Save failed");
  if (res.ok) { event.target.reset(); load(); }
});
loadProjects();
load();

// Realtime: auto-refresh when this collection changes (another tab, user, or agent).
(function () {
  let _lt = null;
  const reload = () => { clearTimeout(_lt); _lt = setTimeout(load, 150); };
  (function wait() {
    if (window.dbbasicSubscribe) window.dbbasicSubscribe("tasks", reload);
    else setTimeout(wait, 300);
  })();
})();
"""


def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_tasks served", user_id=user_id or "anonymous")

    if not user_id:
        body = '<p class="hint"><a href="/login?next=/tasks">Sign in</a> to see your tasks.</p>'
        script = ""
    else:
        body = """
<form class="capture" id="capture-form">
<input name="title" placeholder="Task title" required maxlength="200">
<select name="project" id="project-select"><option value="">No project</option></select>
<select name="urgency">
<option value="low">low</option>
<option value="normal" selected>normal</option>
<option value="high">high</option>
<option value="critical">critical</option>
</select>
<button type="submit" class="btn primary">Add Task</button>
<div class="error" id="page-error"></div>
</form>
<table>
<thead><tr><th>Task</th><th>Status</th><th>Urgency</th><th>Due</th><th>Assigned</th></tr></thead>
<tbody id="rows"><tr><td colspan="5">loading&hellip;</td></tr></tbody>
</table>
"""
        script = f"<script>const OWNER_ID = {user_id!r};{_SCRIPT}</script>"

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else '<a href="/login?next=/tasks">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tasks</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header class="app"><h1>Tasks</h1><div class="who">{who}</div></header>
{body}
</div>
{script}
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
