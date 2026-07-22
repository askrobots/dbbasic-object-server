"""Projects page — built from metadata, not markup.

Previously this page hand-rolled its own table, create form, fetch, and
realtime wiring (~60 lines). Everything it did now comes from the shared
generators: the schema's `views.list_mode: "table"` renders the list via
/list -> window.dbbasicList (sortable columns, status badge, the 50-row
cap, row -> detail, realtime), and the create/edit form via /form ->
window.dbbasicForm. This page is just the chrome — breadcrumb, an Add
button, a search/sort toolbar, two mount points — identical in shape to
every other list page. No hand-written rows or fields; the bespoke copy
that had drifted (no breadcrumb, always-open form, no cap) is gone.
"""

_SCRIPT = """
const panel = document.getElementById("formpanel");
const list = window.dbbasicList("projects", {
  mount: "#list", search: "#search", sort: "#sort", owner: OWNER_ID,
  title: (r) => r.name || "(untitled project)", href: (r) => "/projects/" + r.id,
  created: (r) => r.created_at, onEdit: (r) => openForm(r),
});
function openForm(record) {
  document.getElementById("formtitle").textContent = record ? "Edit Project" : "New Project";
  panel.style.display = "block";
  window.dbbasicForm("projects", {
    mount: "#formmount", record: record, owner: OWNER_ID,
    submitLabel: "Add Project",
    onSaved: () => { panel.style.display = "none"; list.reload(); },
    onCancel: () => { panel.style.display = "none"; },
  });
}
document.getElementById("add").addEventListener("click", () => openForm(null));
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
<div class="breadcrumb"><a href="/">Home</a> / Projects</div>
<div class="pagehead"><h1>Projects</h1><button class="btn primary" id="add">+ New Project</button></div>
<div id="formpanel" style="display:none; margin-bottom:1rem">
  <h2 id="formtitle" style="font-size:1rem; margin:0 0 0.5rem">New Project</h2>
  <div id="formmount"></div>
</div>
<div class="toolbar">
  <input class="search grow" id="search" placeholder="Search projects&hellip;" autocomplete="off">
  <select id="sort"><option value="newest">Newest</option><option value="oldest">Oldest</option></select>
</div>
<div id="list"><div class="state">loading&hellip;</div></div>
"""
        script = (
            f"<script>const OWNER_ID = {user_id!r};</script>"
            '<script src="/list"></script><script src="/form"></script>'
            f"<script>{_SCRIPT}</script>"
        )

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
