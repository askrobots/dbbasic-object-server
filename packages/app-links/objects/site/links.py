"""Links page: the visitor's own bookmarks as a table, with quick add and search."""

_STYLE = """
:root { color-scheme: dark; --bg: #0b0b10; --panel: #17171f; --line: #2b2b37;
        --text: #f4f4f7; --muted: #a2a2ad; --blue: #5aa7ff; --red: #ff6b6b; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
.wrap { max-width: 820px; margin: 0 auto; padding: 1.5rem; }
header { display: flex; align-items: baseline; gap: 1rem; margin-bottom: 1.25rem; }
header h1 { font-size: 1.15rem; margin: 0; }
header .who { margin-left: auto; color: var(--muted); font-size: 0.85rem; }
a { color: var(--blue); text-decoration: none; }
form.capture { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
               padding: 1rem; display: grid; gap: 0.6rem; margin-bottom: 1rem;
               grid-template-columns: 1fr 1fr auto; }
form.capture input { background: var(--bg); color: var(--text); border: 1px solid var(--line);
                     border-radius: 6px; padding: 0.45rem 0.6rem; font: inherit; width: 100%; }
form.capture button { background: var(--blue); color: #0b0b10; border: 0; border-radius: 6px;
                      padding: 0.5rem 1rem; font: inherit; font-weight: 600; cursor: pointer; }
input.search { background: var(--bg); color: var(--text); border: 1px solid var(--line);
               border-radius: 6px; padding: 0.45rem 0.6rem; font: inherit; width: 100%;
               margin-bottom: 1rem; }
table { width: 100%; border-collapse: collapse; background: var(--panel);
        border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
th, td { text-align: left; font-size: 0.85rem; padding: 0.5rem 0.75rem;
         border-bottom: 1px solid var(--line); word-break: break-all; }
th { color: var(--muted); font-weight: 500; }
tr:last-child td { border-bottom: 0; }
td.tags { color: var(--muted); white-space: nowrap; }
.hint { color: var(--muted); font-size: 0.85rem; background: var(--panel);
        border: 1px solid var(--line); border-radius: 8px; padding: 0.9rem 1rem; }
.error { color: var(--red); font-size: 0.85rem; min-height: 1.2rem; grid-column: 1 / -1; }
"""

_SCRIPT = """
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

function render(records) {
  const rows = records.map((l) =>
    `<tr><td><a href="${esc(l.url)}" rel="noopener noreferrer">${esc(l.title)}</a></td>` +
    `<td>${esc(l.url)}</td><td class="tags">${esc(l.tags)}</td></tr>`);
  document.getElementById("rows").innerHTML =
    rows.join("") || '<tr><td colspan="3">No links yet.</td></tr>';
}

async function load() {
  const res = await fetch("/collections/links/records?limit=200",
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  const body = await res.json();
  render((body.records || []).slice().reverse());
}

document.getElementById("search-box").addEventListener("input", async (event) => {
  const query = event.target.value.trim();
  if (!query) { load(); return; }
  const res = await fetch(`/api/search?q=${encodeURIComponent(query)}&collections=links&limit=50`,
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) return;
  const body = await res.json();
  render((body.results || {}).links || []);
});

document.getElementById("capture-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const fields = event.target.elements;
  const record = {id: crypto.randomUUID(), title: fields["title"].value.trim(),
                  url: fields["url"].value.trim(), tags: fields["tags"].value.trim(),
                  owner_id: OWNER_ID};
  const res = await fetch("/collections/links/records", {
    method: "POST", credentials: "same-origin",
    headers: {"content-type": "application/json", accept: "application/json"},
    body: JSON.stringify(record),
  });
  const body = await res.json();
  document.getElementById("form-error").textContent = res.ok ? "" : (body.error || "Save failed");
  if (res.ok) { event.target.reset(); load(); }
});
load();
"""


def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_links served", user_id=user_id or "anonymous")

    if not user_id:
        body = '<p class="hint"><a href="/login?next=/links">Sign in</a> to see your links.</p>'
        script = ""
    else:
        body = """
<form class="capture" id="capture-form">
<input name="title" placeholder="Title" required maxlength="200">
<input name="url" placeholder="https://" required maxlength="2000">
<input name="tags" placeholder="tags, comma, separated" style="grid-column: 1 / 3">
<button type="submit" style="grid-column: 3; grid-row: 1">Add Link</button>
<div class="error" id="form-error"></div>
</form>
<input class="search" id="search-box" placeholder="Search links&hellip;" autocomplete="off">
<table>
<thead><tr><th>Title</th><th>URL</th><th>Tags</th></tr></thead>
<tbody id="rows"><tr><td colspan="3">loading&hellip;</td></tr></tbody>
</table>
"""
        script = f"<script>const OWNER_ID = {user_id!r};{_SCRIPT}</script>"

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else '<a href="/login?next=/links">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Links</title>
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header><h1>Links</h1><div class="who">{who}</div></header>
{body}
</div>
{script}
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
