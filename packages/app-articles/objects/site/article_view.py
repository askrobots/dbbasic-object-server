"""Single-article permalink: published articles readable by anyone, owners edit.

Served through a site route like /articles/{article_id:uuid} — the q9 URL
shape. The permission policy decides visibility; owners get edit,
publish, and delete controls.
"""

import re

_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

_STYLE = """
:root { color-scheme: dark; --bg: #0b0b10; --panel: #17171f; --line: #2b2b37;
        --text: #f4f4f7; --muted: #a2a2ad; --blue: #5aa7ff; --red: #ff6b6b; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
.wrap { max-width: 680px; margin: 0 auto; padding: 1.5rem; }
header { display: flex; align-items: baseline; gap: 1rem; margin-bottom: 1.5rem; }
header .who { margin-left: auto; color: var(--muted); font-size: 0.8rem; }
a { color: var(--blue); text-decoration: none; }
h1#title { font-size: 1.6rem; margin: 0 0 0.25rem; }
.meta { color: var(--muted); font-size: 0.8rem; margin-bottom: 1.25rem; }
#content { white-space: pre-wrap; word-break: break-word; }
.owner-tools { margin-top: 1.5rem; display: none; gap: 0.5rem; flex-wrap: wrap; }
.owner-tools button { border: 1px solid var(--line); background: var(--panel);
                      color: var(--text); border-radius: 6px; padding: 0.4rem 0.9rem;
                      font: inherit; cursor: pointer; }
.owner-tools button.primary { background: var(--blue); color: #0b0b10; border: 0; font-weight: 600; }
.owner-tools button.danger { color: var(--red); }
textarea.edit, input.edit-title { display: none; width: 100%; background: var(--bg);
    color: var(--text); border: 1px solid var(--line); border-radius: 6px;
    padding: 0.6rem; font: inherit; margin-top: 0.75rem; }
textarea.edit { min-height: 14rem; }
.hint { color: var(--muted); font-size: 0.9rem; background: var(--panel);
        border: 1px solid var(--line); border-radius: 8px; padding: 0.9rem 1rem; }
.error { color: var(--red); font-size: 0.85rem; min-height: 1.2rem; margin-top: 0.5rem; }
"""

_SCRIPT = """
const el = (id) => document.getElementById(id);
let article = null;

function render() {
  el("title").textContent = article.title;
  el("content").textContent = article.content;
  const published = article.is_public === "true";
  el("meta").textContent = published
    ? ("published" + (article.published_on ? " " + article.published_on : ""))
    : "draft";
  const mine = VIEWER_ID && article.owner_id === VIEWER_ID;
  el("owner-tools").style.display = mine ? "flex" : "none";
  if (mine) el("publish-btn").textContent = published ? "Unpublish" : "Publish";
  document.title = article.title;
}

async function load() {
  const res = await fetch(`/collections/articles/records/${ARTICLE_ID}`,
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) {
    el("body").innerHTML = VIEWER_ID
      ? '<p class="hint">This article does not exist or is not published.</p>'
      : `<p class="hint"><a href="/login?next=/articles/${ARTICLE_ID}">Sign in</a> to view this article.</p>`;
    return;
  }
  const body = await res.json();
  article = body.record || body;
  render();
}

async function save(changes) {
  const res = await fetch(`/collections/articles/records/${ARTICLE_ID}`, {
    method: "PUT", credentials: "same-origin",
    headers: {"content-type": "application/json", accept: "application/json"},
    body: JSON.stringify(changes),
  });
  const body = await res.json();
  el("page-error").textContent = res.ok ? "" : (body.error || "Save failed");
  if (res.ok) { article = body.record || article; render(); }
  return res.ok;
}

el("edit-btn").addEventListener("click", () => {
  el("edit-title").style.display = "block";
  el("edit-title").value = article.title;
  el("edit-box").style.display = "block";
  el("edit-box").value = article.content;
  el("save-btn").style.display = "inline-block";
});
el("save-btn").addEventListener("click", async () => {
  if (await save({title: el("edit-title").value, content: el("edit-box").value})) {
    el("edit-title").style.display = "none";
    el("edit-box").style.display = "none";
    el("save-btn").style.display = "none";
  }
});
el("publish-btn").addEventListener("click", () => {
  const publishing = article.is_public !== "true";
  const changes = {is_public: publishing ? "true" : "false"};
  if (publishing && !article.published_on) {
    changes.published_on = new Date().toISOString().slice(0, 10);
  }
  save(changes);
});
el("delete-btn").addEventListener("click", async () => {
  if (!confirm("Delete this article?")) return;
  const res = await fetch(`/collections/articles/records/${ARTICLE_ID}`,
                          {method: "DELETE", credentials: "same-origin",
                           headers: {accept: "application/json"}});
  if (res.ok) window.location = "/articles";
  else el("page-error").textContent = "Delete failed";
});
load();
"""


def GET(request):
    article_id = str(request.get("article_id") or "").strip()
    if article_id and not _RECORD_ID_RE.fullmatch(article_id):
        article_id = ""
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_article_view served", article_id=article_id or "missing",
                 user_id=user_id or "anonymous")

    if not article_id:
        return {
            "content_type": "text/html; charset=utf-8",
            "body": "<p>Article not found. <a href='/articles'>Back to articles</a></p>",
            "status": 404,
        }

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else f'<a href="/login?next=/articles/{article_id}">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Article</title>
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header><a href="/articles">Articles</a><div class="who">{who}</div></header>
<div id="body">
<h1 id="title">loading&hellip;</h1>
<div class="meta" id="meta"></div>
<div id="content"></div>
</div>
<input class="edit-title" id="edit-title">
<textarea class="edit" id="edit-box"></textarea>
<div class="owner-tools" id="owner-tools">
<button id="edit-btn">Edit</button>
<button id="save-btn" class="primary" style="display:none">Save</button>
<button id="publish-btn">Publish</button>
<button id="delete-btn" class="danger">Delete</button>
</div>
<div class="error" id="page-error"></div>
</div>
<script>const ARTICLE_ID = {article_id!r}; const VIEWER_ID = {(user_id or "")!r};{_SCRIPT}</script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
