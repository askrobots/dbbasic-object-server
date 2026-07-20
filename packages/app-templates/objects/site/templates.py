"""Templates page — built from metadata, not markup.

Mirrors packages/app-tasks/objects/site/tasks.py: the form and the list
both come from the schema via the shared generators (/form ->
window.dbbasicForm, /list -> window.dbbasicList). This page is just the
chrome: a breadcrumb, an Add button, a search/sort toolbar, and two mount
points. Add or edit opens the schema-driven form; the list auto-refreshes
over the websocket. No hand-written fields or rows.

Template execution -- seeding a new record from a template's `schema` /
`default_values` on create -- is not built here. That would be a second
generator (something that reads one collection's stored JSON Schema and
uses it to drive a form for a *different* collection), which is more than
the small, additive slice this page covers; it's deferred and left for a
later pass rather than built half-done.
"""

_SCRIPT = """
const panel = document.getElementById("formpanel");
const list = window.dbbasicList("templates", {
  mount: "#list", search: "#search", sort: "#sort", owner: OWNER_ID,
  title: (r) => r.name,
  subtitle: (r) => [r.category, r.ai_assistance].filter(Boolean).join(" · "),
  created: (r) => r.created_at, onEdit: (r) => openForm(r),
});
function openForm(record) {
  document.getElementById("formtitle").textContent = record ? "Edit Template" : "New Template";
  panel.style.display = "block";
  window.dbbasicForm("templates", {
    mount: "#formmount", record: record, owner: OWNER_ID,
    onSaved: () => { panel.style.display = "none"; list.reload(); },
    onCancel: () => { panel.style.display = "none"; },
  });
}
document.getElementById("add").addEventListener("click", () => openForm(null));
"""


def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_templates served", user_id=user_id or "anonymous")

    if not user_id:
        body = '<p class="hint"><a href="/login?next=/templates">Sign in</a> to see your templates.</p>'
        script = ""
    else:
        body = """
<div class="breadcrumb"><a href="/">Home</a> / Templates</div>
<div class="pagehead"><h1>Templates</h1><button class="btn primary" id="add">+ New Template</button></div>
<div id="formpanel" style="display:none; margin-bottom:1rem">
  <h2 id="formtitle" style="font-size:1rem; margin:0 0 0.5rem">New Template</h2>
  <div id="formmount"></div>
</div>
<div class="toolbar">
  <input class="search grow" id="search" placeholder="Search templates&hellip;" autocomplete="off">
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
        else '<a href="/login?next=/templates">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Templates</title>
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
