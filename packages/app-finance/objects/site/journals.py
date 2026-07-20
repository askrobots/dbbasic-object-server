"""Journals page — built from metadata, not markup.

The form and the list both come from the schema via the shared generators
(/form -> window.dbbasicForm, /list -> window.dbbasicList). This page is
just the chrome: a breadcrumb, an Add button, a search/sort toolbar, and
two mount points. Add or edit opens the schema-driven form (including the
guarded status field, so posting a journal draft->posted happens through
this same generic form -- see schemas/fin_journals.json's status help for
why posting never checks the balance). The list auto-refreshes over the
websocket. No hand-written fields or rows. Same shape as
packages/app-invoices/objects/site/invoices.py.
"""

_SCRIPT = """
const panel = document.getElementById("formpanel");
const list = window.dbbasicList("fin_journals", {
  mount: "#list", search: "#search", sort: "#sort", owner: OWNER_ID,
  title: (r) => r.description || "(no description)",
  href: (r) => "/journals/" + r.id,
  subtitle: (r) => [r.date, r.status, r.currency].filter(Boolean).join(" \\u00b7 "),
  created: (r) => r.created_at, onEdit: (r) => openForm(r),
});
function openForm(record) {
  document.getElementById("formtitle").textContent = record ? "Edit Journal" : "New Journal";
  panel.style.display = "block";
  window.dbbasicForm("fin_journals", {
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
    _logger.info("site_journals served", user_id=user_id or "anonymous")

    if not user_id:
        body = '<p class="hint"><a href="/login?next=/journals">Sign in</a> to see your journals.</p>'
        script = ""
    else:
        body = """
<div class="breadcrumb"><a href="/">Home</a> / Journals</div>
<div class="pagehead"><h1>Journals</h1><button class="btn primary" id="add">+ New Journal</button></div>
<div id="formpanel" style="display:none; margin-bottom:1rem">
  <h2 id="formtitle" style="font-size:1rem; margin:0 0 0.5rem">New Journal</h2>
  <div id="formmount"></div>
</div>
<div class="toolbar">
  <input class="search grow" id="search" placeholder="Search journals&hellip;" autocomplete="off">
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
        else '<a href="/login?next=/journals">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Journals</title>
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
