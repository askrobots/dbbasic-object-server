"""Articles page: published writing for visitors, drafts and writing for owners.

Anonymous visitors see published articles because the public-read rule
returns them from /collections/articles/records — this page is a working
blog with zero visibility code of its own.
"""

_SCRIPT = """
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

function render(records) {
  const items = records.map((a) => {
    const published = a.is_public === "true";
    const label = published ? (a.published_on || "published") : "draft";
    const badge = `<span class="badge${published ? " positive" : ""}">${esc(label)}</span>`;
    const preview = esc(String(a.content || "").slice(0, 200));
    return `<div class="card"><h2 class="title"><a href="/articles/${encodeURIComponent(a.id)}">` +
           `${esc(a.title)}</a></h2><div class="meta">${badge}</div><p>${preview}</p></div>`;
  });
  document.getElementById("items").innerHTML =
    items.join("") || '<p class="hint">Nothing published yet.</p>';
}

async function load() {
  const res = await fetch("/collections/articles/records?limit=200",
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) { render([]); return; }
  const body = await res.json();
  render((body.records || []).slice().reverse());
}

const form = document.getElementById("capture-form");
if (form) form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const fields = form.elements;
  const record = {id: crypto.randomUUID(), title: fields["title"].value.trim(),
                  content: fields["content"].value, is_public: "false", owner_id: OWNER_ID};
  const res = await fetch("/collections/articles/records", {
    method: "POST", credentials: "same-origin",
    headers: {"content-type": "application/json", accept: "application/json"},
    body: JSON.stringify(record),
  });
  const body = await res.json();
  document.getElementById("form-error").textContent = res.ok ? "" : (body.error || "Save failed");
  if (res.ok) { window.location = `/articles/${record.id}`; }
});
load();
"""


def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_articles served", user_id=user_id or "anonymous")

    capture = ""
    owner_snippet = "const OWNER_ID = null;"
    if user_id:
        capture = """
<form class="card stack" id="capture-form">
<input name="title" placeholder="Title" required maxlength="200">
<textarea name="content" placeholder="Write&hellip;" rows="6" required></textarea>
<button type="submit" class="btn primary">Save Draft</button>
<div class="error" id="form-error"></div>
</form>
"""
        owner_snippet = f"const OWNER_ID = {user_id!r};"

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else '<a href="/login?next=/articles">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Articles</title>
<link rel="stylesheet" href="/style">
</head>
<body>
<div class="wrap narrow">
<header class="app"><h1>Articles</h1><div class="who">{who}</div></header>
{capture}
<div id="items" class="stack"><p class="hint">loading&hellip;</p></div>
</div>
<script>{owner_snippet}{_SCRIPT}</script>
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
