"""Single-note permalink page: public notes readable by anyone, owners can edit.

Served through a site route like /notes/{note_id:uuid}. The browser fetches
the record with the visitor's session cookie, so the permission policy
decides visibility: a public-read rule row-filtered on is_public serves
anonymous visitors, and owners get edit, share, and delete controls.
"""

# Page-unique layout only; everything else comes from the shared /style sheet.
_STYLE = """
.card { white-space: pre-wrap; }
.owner-tools { margin-top: 1rem; display: none; gap: 0.5rem; flex-wrap: wrap; }
textarea.edit { display: none; min-height: 8rem; margin-top: 1rem; }
"""

_SCRIPT = """
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const el = (id) => document.getElementById(id);
let note = null;

function render() {
  el("note-content").textContent = note.content;
  const bits = [];
  if (note.project_id) bits.push("project: " + note.project_id);
  bits.push(note.is_public === "true" ? "public" : "private");
  el("note-meta").textContent = bits.join(" \\u00b7 ");
  const mine = VIEWER_ID && note.owner_id === VIEWER_ID;
  el("owner-tools").style.display = mine ? "flex" : "none";
  if (mine) el("share-btn").textContent =
    note.is_public === "true" ? "Make Private" : "Make Public";
}

async function load() {
  const res = await fetch(`/collections/notes/records/${NOTE_ID}`,
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) {
    el("card").innerHTML = VIEWER_ID
      ? '<p class="hint">This note does not exist or is not shared with you.</p>'
      : `<p class="hint"><a href="/login?next=/notes/${NOTE_ID}">Sign in</a> to view this note.</p>`;
    return;
  }
  const body = await res.json();
  note = body.record || body;
  render();
}

async function save(changes) {
  const res = await fetch(`/collections/notes/records/${NOTE_ID}`, {
    method: "PUT", credentials: "same-origin",
    headers: {"content-type": "application/json", accept: "application/json"},
    body: JSON.stringify(changes),
  });
  const body = await res.json();
  el("page-error").textContent = res.ok ? "" : (body.error || "Save failed");
  if (res.ok) { note = body.record || note; render(); }
  return res.ok;
}

el("edit-btn").addEventListener("click", () => {
  const box = el("edit-box");
  box.style.display = "block";
  box.value = note.content;
  box.focus();
  el("save-btn").style.display = "inline-block";
});
el("save-btn").addEventListener("click", async () => {
  if (await save({content: el("edit-box").value})) {
    el("edit-box").style.display = "none";
    el("save-btn").style.display = "none";
  }
});
el("share-btn").addEventListener("click", () =>
  save({is_public: note.is_public === "true" ? "false" : "true"}));
el("delete-btn").addEventListener("click", async () => {
  if (!confirm("Delete this note?")) return;
  const res = await fetch(`/collections/notes/records/${NOTE_ID}`,
                          {method: "DELETE", credentials: "same-origin",
                           headers: {accept: "application/json"}});
  if (res.ok) window.location = "/notes";
  else el("page-error").textContent = "Delete failed";
});
load();
"""


import re

_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def GET(request):
    note_id = str(request.get("note_id") or "").strip()
    if note_id and not _RECORD_ID_RE.fullmatch(note_id):
        note_id = ""
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_note_view served", note_id=note_id or "missing",
                 user_id=user_id or "anonymous")

    if not note_id:
        return {
            "content_type": "text/html; charset=utf-8",
            "body": "<p>Note not found. <a href='/notes'>Back to notes</a></p>",
            "status": 404,
        }

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else f'<a href="/login?next=/notes/{note_id}">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Note</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap narrow">
<header class="app"><h1><a href="/notes">Notes</a> / note</h1><div class="who">{who}</div></header>
<div class="card" id="card">
<div id="note-content">loading&hellip;</div>
<div class="meta" id="note-meta"></div>
</div>
<textarea class="edit" id="edit-box"></textarea>
<div class="owner-tools" id="owner-tools">
<button id="edit-btn" class="btn">Edit</button>
<button id="save-btn" class="btn primary" style="display:none">Save</button>
<button id="share-btn" class="btn">Make Public</button>
<button id="delete-btn" class="btn danger">Delete</button>
</div>
<div class="error" id="page-error"></div>
</div>
<script>const NOTE_ID = {note_id!r}; const VIEWER_ID = {(user_id or "")!r};{_SCRIPT}</script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
