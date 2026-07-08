"""Links page: the visitor's own bookmarks as a table, with quick add and search."""

# Page-unique layout only; everything else comes from the shared /style sheet.
_STYLE = """
form.capture { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius-md);
               padding: var(--pad); display: grid; gap: var(--gap); margin-bottom: var(--gap);
               grid-template-columns: 1fr 1fr auto; }
form.capture .error { grid-column: 1 / -1; }
input.search { margin-bottom: var(--gap); }
td.tags { color: var(--muted); white-space: nowrap; }
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

// Realtime: auto-refresh when this collection changes (another tab, user, or agent).
(function () {
  let _lt = null;
  const reload = () => { clearTimeout(_lt); _lt = setTimeout(load, 150); };
  (function wait() {
    if (window.dbbasicSubscribe) window.dbbasicSubscribe("links", reload);
    else setTimeout(wait, 300);
  })();
})();
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
<button type="submit" class="btn primary" style="grid-column: 3; grid-row: 1">Add Link</button>
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
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header class="app"><h1>Links</h1><div class="who">{who}</div></header>
{body}
</div>
{script}
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
