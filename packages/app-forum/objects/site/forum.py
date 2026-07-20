"""Forum landing page — built from metadata, not markup.

Two sections, each just a mount point for the shared list generator
(/list -> window.dbbasicList): the category board list, and a feed of
recent topics across every category. New Category / New Topic both open
the schema-driven form (/form -> window.dbbasicForm); category_id on the
topic form renders as a select over forum_categories automatically,
because it is declared as a relation field in schemas/forum_topics.json.

The forum is public read (see permissions/rules.json): anonymous visitors
see both lists. Only signed-in users get the "+ New Category" / "+ New
Topic" buttons and the forms; dbbasicList's own owner match still decides
per-row edit/delete controls.
"""

_SCRIPT = """
const catPanel = document.getElementById("catformpanel");
const topicPanel = document.getElementById("topicformpanel");

const categories = window.dbbasicList("forum_categories", {
  mount: "#categories", owner: OWNER_ID,
  title: (r) => r.name, subtitle: (r) => r.description,
  created: (r) => r.created_at, onEdit: (r) => openCatForm(r),
});
function openCatForm(record) {
  document.getElementById("catformtitle").textContent = record ? "Edit Category" : "New Category";
  catPanel.style.display = "block";
  window.dbbasicForm("forum_categories", {
    mount: "#catformmount", record: record, owner: OWNER_ID,
    onSaved: () => { catPanel.style.display = "none"; categories.reload(); },
    onCancel: () => { catPanel.style.display = "none"; },
  });
}
const addCat = document.getElementById("addcat");
if (addCat) addCat.addEventListener("click", () => openCatForm(null));

const topics = window.dbbasicList("forum_topics", {
  mount: "#topics", search: "#search", sort: "#sort", owner: OWNER_ID,
  title: (r) => r.title, href: (r) => "/forum/topics/" + r.id,
  subtitle: (r) => {
    const bits = [];
    if (r.is_pinned === "true") bits.push("pinned");
    if (r.is_solved === "true") bits.push("solved");
    bits.push((r.views || "0") + " views");
    return bits.join(" \\u00b7 ");
  },
  created: (r) => r.created_at, onEdit: (r) => openTopicForm(r),
});
function openTopicForm(record) {
  document.getElementById("topicformtitle").textContent = record ? "Edit Topic" : "New Topic";
  topicPanel.style.display = "block";
  window.dbbasicForm("forum_topics", {
    mount: "#topicformmount", record: record, owner: OWNER_ID,
    onSaved: () => { topicPanel.style.display = "none"; topics.reload(); },
    onCancel: () => { topicPanel.style.display = "none"; },
  });
}
const addTopic = document.getElementById("addtopic");
if (addTopic) addTopic.addEventListener("click", () => openTopicForm(null));
"""


def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_forum served", user_id=user_id or "anonymous")

    add_cat_btn = (
        '<button class="btn" id="addcat">+ New Category</button>' if user_id else ""
    )
    add_topic_btn = (
        '<button class="btn primary" id="addtopic">+ New Topic</button>' if user_id else ""
    )
    cat_form_panel = (
        """
<div id="catformpanel" style="display:none; margin-bottom:1rem">
  <h2 id="catformtitle" style="font-size:1rem; margin:0 0 0.5rem">New Category</h2>
  <div id="catformmount"></div>
</div>"""
        if user_id
        else ""
    )
    topic_form_panel = (
        """
<div id="topicformpanel" style="display:none; margin-bottom:1rem">
  <h2 id="topicformtitle" style="font-size:1rem; margin:0 0 0.5rem">New Topic</h2>
  <div id="topicformmount"></div>
</div>"""
        if user_id
        else ""
    )
    body = f"""
<div class="breadcrumb"><a href="/">Home</a> / Forum</div>
<div class="pagehead"><h1>Forum</h1></div>

<div class="pagehead"><h2 style="font-size:1.1rem; margin:0">Categories</h2>{add_cat_btn}</div>
{cat_form_panel}
<div id="categories"><div class="state">loading&hellip;</div></div>

<div class="pagehead" style="margin-top:2rem"><h2 style="font-size:1.1rem; margin:0">Recent Topics</h2>{add_topic_btn}</div>
{topic_form_panel}
<div class="toolbar">
  <input class="search grow" id="search" placeholder="Search topics&hellip;" autocomplete="off">
  <select id="sort"><option value="newest">Newest</option><option value="oldest">Oldest</option></select>
</div>
<div id="topics"><div class="state">loading&hellip;</div></div>
"""

    script = (
        f"<script>const OWNER_ID = {user_id or ''!r};</script>"
        '<script src="/list"></script><script src="/form"></script>'
        f"<script>{_SCRIPT}</script>"
    )

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else '<a href="/login?next=/forum">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Forum</title>
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
