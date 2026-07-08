"""Files page: upload, list, share, download, delete.

Uploads post multipart to /api/files; downloads hit /api/files/{id}, which
authorizes against the file's metadata record — so the share toggle here
is just a record update, and a public file's URL works for anyone.
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
    const pub = f.is_public === "true" ? '<span class="badge positive">public</span>' : "";
    const actions = mine
      ? `<button class="btn ghost sm" data-id="${esc(f.id)}" data-act="share">` +
        `${f.is_public === "true" ? "make private" : "make public"}</button>` +
        `<button class="btn danger sm" data-id="${esc(f.id)}" data-act="delete">delete</button>`
      : "";
    return `<tr><td><a href="/api/files/${esc(f.id)}">${esc(f.filename)}</a> ${pub}</td>` +
           `<td class="num">${fmtSize(f.size)}</td><td>${esc(f.content_type)}</td>` +
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
<form class="toolbar" id="upload-form">
<input type="file" name="file" required class="grow">
<button type="submit" class="btn primary">Upload</button>
<div class="error" id="form-error"></div>
</form>
<table>
<thead><tr><th>File</th><th class="num">Size</th><th>Type</th><th></th></tr></thead>
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
<link rel="stylesheet" href="/style">
</head>
<body>
<div class="wrap">
<header class="app"><h1>Files</h1><div class="who">{who}</div></header>
{body}
</div>
{script}
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
