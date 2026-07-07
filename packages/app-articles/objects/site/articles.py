"""Articles page: published writing for visitors, drafts and writing for owners.

Anonymous visitors see published articles because the public-read rule
returns them from /collections/articles/records — this page is a working
blog with zero visibility code of its own.
"""

_STYLE = """
:root { color-scheme: dark; --bg: #0b0b10; --panel: #17171f; --line: #2b2b37;
        --text: #f4f4f7; --muted: #a2a2ad; --blue: #5aa7ff; --red: #ff6b6b; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
.wrap { max-width: 720px; margin: 0 auto; padding: 1.5rem; }
header { display: flex; align-items: baseline; gap: 1rem; margin-bottom: 1.25rem; }
header h1 { font-size: 1.15rem; margin: 0; }
header .who { margin-left: auto; color: var(--muted); font-size: 0.85rem; }
a { color: var(--blue); text-decoration: none; }
form.capture { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
               padding: 1rem; display: grid; gap: 0.6rem; margin-bottom: 1.25rem; }
form.capture input, form.capture textarea {
  background: var(--bg); color: var(--text); border: 1px solid var(--line);
  border-radius: 6px; padding: 0.45rem 0.6rem; font: inherit; width: 100%; }
form.capture button { background: var(--blue); color: #0b0b10; border: 0; border-radius: 6px;
                      padding: 0.5rem 1rem; font: inherit; font-weight: 600; cursor: pointer;
                      justify-self: start; }
.item { padding: 1rem 0; border-bottom: 1px solid var(--line); }
.item h2 { margin: 0 0 0.25rem; font-size: 1.05rem; }
.item .meta { color: var(--muted); font-size: 0.78rem; }
.item p { margin: 0.5rem 0 0; color: var(--muted); }
.hint { color: var(--muted); font-size: 0.85rem; background: var(--panel);
        border: 1px solid var(--line); border-radius: 8px; padding: 0.9rem 1rem; }
.error { color: var(--red); font-size: 0.85rem; min-height: 1.2rem; }
"""

_SCRIPT = """
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

function render(records) {
  const items = records.map((a) => {
    const badge = a.is_public === "true" ? (a.published_on || "published") : "draft";
    const preview = esc(String(a.content || "").slice(0, 200));
    return `<div class="item"><h2><a href="/articles/${encodeURIComponent(a.id)}">` +
           `${esc(a.title)}</a></h2><div class="meta">${esc(badge)}</div><p>${preview}</p></div>`;
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
<form class="capture" id="capture-form">
<input name="title" placeholder="Title" required maxlength="200">
<textarea name="content" placeholder="Write&hellip;" rows="6" required></textarea>
<button type="submit">Save Draft</button>
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
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header><h1>Articles</h1><div class="who">{who}</div></header>
{capture}
<div id="items"><p class="hint">loading&hellip;</p></div>
</div>
<script>{owner_snippet}{_SCRIPT}</script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
