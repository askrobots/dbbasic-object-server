"""Files page: upload, list, share, download, delete.

Uploads post multipart to /api/files; downloads hit /api/files/{id}, which
authorizes against the file's metadata record — so the share toggle here
is just a record update, and a public file's URL works for anyone.
"""

_STYLE = """
:root { color-scheme: dark; --bg: #0b0b10; --panel: #17171f; --line: #2b2b37;
        --text: #f4f4f7; --muted: #a2a2ad; --blue: #5aa7ff; --green: #52d273;
        --red: #ff6b6b; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
.wrap { max-width: 860px; margin: 0 auto; padding: 1.5rem; }
header { display: flex; align-items: baseline; gap: 1rem; margin-bottom: 1.25rem; }
header h1 { font-size: 1.15rem; margin: 0; }
header .who { margin-left: auto; color: var(--muted); font-size: 0.85rem; }
a { color: var(--blue); text-decoration: none; }
form.upload { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
              padding: 1rem; display: flex; gap: 0.6rem; align-items: center;
              margin-bottom: 1rem; flex-wrap: wrap; }
form.upload input[type=file] { color: var(--muted); flex: 1; }
form.upload button { background: var(--blue); color: #0b0b10; border: 0; border-radius: 6px;
                     padding: 0.5rem 1rem; font: inherit; font-weight: 600; cursor: pointer; }
table { width: 100%; border-collapse: collapse; background: var(--panel);
        border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
th, td { text-align: left; font-size: 0.85rem; padding: 0.5rem 0.75rem;
         border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 500; }
tr:last-child td { border-bottom: 0; }
td.size { color: var(--muted); white-space: nowrap; }
td button { border: 1px solid var(--line); background: var(--bg); color: var(--text);
            border-radius: 5px; padding: 0.15rem 0.5rem; font-size: 0.75rem;
            cursor: pointer; margin-right: 0.25rem; }
td button.danger { color: var(--red); }
td .pub { color: var(--green); font-size: 0.75rem; }
.hint { color: var(--muted); font-size: 0.85rem; background: var(--panel);
        border: 1px solid var(--line); border-radius: 8px; padding: 0.9rem 1rem; }
.error { color: var(--red); font-size: 0.85rem; min-height: 1.2rem; width: 100%; }
"""

_SCRIPT = """
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

function fmtSize(n) {
  const size = Number(n) || 0;
  if (size > 1048576) return (size / 1048576).toFixed(1) + " MB";
  if (size > 1024) return (size / 1024).toFixed(1) + " KB";
  return size + " B";
}

function render(records) {
  const rows = records.slice().reverse().map((f) => {
    const mine = f.owner_id === OWNER_ID;
    const pub = f.is_public === "true" ? '<span class="pub">public</span>' : "";
    const actions = mine
      ? `<button data-id="${esc(f.id)}" data-act="share">` +
        `${f.is_public === "true" ? "make private" : "make public"}</button>` +
        `<button class="danger" data-id="${esc(f.id)}" data-act="delete">delete</button>`
      : "";
    return `<tr><td><a href="/api/files/${esc(f.id)}">${esc(f.filename)}</a> ${pub}</td>` +
           `<td class="size">${fmtSize(f.size)}</td><td>${esc(f.content_type)}</td>` +
           `<td>${actions}</td></tr>`;
  });
  document.getElementById("rows").innerHTML =
    rows.join("") || '<tr><td colspan="4">No files yet.</td></tr>';
}

async function load() {
  const res = await fetch("/collections/files/records?limit=500",
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  const body = await res.json();
  render(body.records || []);
}

document.getElementById("upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = event.target.elements["file"];
  if (!input.files.length) return;
  const data = new FormData();
  data.append("file", input.files[0]);
  const res = await fetch("/api/files", {method: "POST", credentials: "same-origin", body: data});
  const body = await res.json();
  document.getElementById("form-error").textContent = res.ok ? "" : (body.error || "Upload failed");
  if (res.ok) { event.target.reset(); load(); }
});

document.getElementById("rows").addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  const id = button.dataset.id;
  if (button.dataset.act === "delete") {
    if (!confirm("Delete this file?")) return;
    await fetch(`/api/files/${id}`, {method: "DELETE", credentials: "same-origin",
                                     headers: {accept: "application/json"}});
    load();
    return;
  }
  const res = await fetch(`/collections/files/records/${id}`,
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  const body = await res.json();
  const current = (body.record || {}).is_public === "true";
  await fetch(`/collections/files/records/${id}`, {
    method: "PUT", credentials: "same-origin",
    headers: {"content-type": "application/json", accept: "application/json"},
    body: JSON.stringify({is_public: current ? "false" : "true"}),
  });
  load();
});
load();
"""


def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_files served", user_id=user_id or "anonymous")

    if not user_id:
        body = '<p class="hint"><a href="/login?next=/files">Sign in</a> to see your files.</p>'
        script = ""
    else:
        body = """
<form class="upload" id="upload-form">
<input type="file" name="file" required>
<button type="submit">Upload</button>
<div class="error" id="form-error"></div>
</form>
<table>
<thead><tr><th>File</th><th>Size</th><th>Type</th><th></th></tr></thead>
<tbody id="rows"><tr><td colspan="4">loading&hellip;</td></tr></tbody>
</table>
"""
        script = f"<script>const OWNER_ID = {user_id!r};{_SCRIPT}</script>"

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else '<a href="/login?next=/files">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Files</title>
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header><h1>Files</h1><div class="who">{who}</div></header>
{body}
</div>
{script}
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
