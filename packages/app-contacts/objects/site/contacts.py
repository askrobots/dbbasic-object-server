"""Contacts page: the visitor's own contacts as cards, with quick add and search.

The browser talks to /collections/contacts/records and /api/search with the
visitor's session cookie, so the permission policy decides what this page
can see and write — the page itself holds no data access.
"""

# Page-unique layout only; everything else comes from the shared /style sheet.
_STYLE = """
form.capture { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius-md);
               padding: var(--pad); display: grid; gap: var(--gap); margin-bottom: var(--gap);
               grid-template-columns: 1fr 1fr; }
form.capture .full { grid-column: 1 / -1; }
form.capture .btn { grid-column: 1 / -1; justify-self: start; }
form.capture .error { grid-column: 1 / -1; }
input.search { margin-bottom: var(--gap); }
"""

_SCRIPT = """
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
let orgNames = {};

function renderCards(records) {
  const cards = records.map((c) => {
    const bits = [c.email, c.phone, orgNames[c.organization_id] || c.organization_id, c.tags]
      .filter(Boolean).map(esc);
    return `<div class="card"><div class="name">${esc(c.first_name)} ${esc(c.last_name)}</div>` +
           `<div class="meta">${bits.join(" \\u00b7 ")}</div></div>`;
  });
  document.getElementById("cards").innerHTML =
    cards.join("") || '<p class="hint">No contacts yet.</p>';
}

async function loadOrgs() {
  const res = await fetch("/collections/organizations/records?limit=200",
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) return;
  const body = await res.json();
  const select = document.getElementById("org-select");
  for (const org of body.records || []) {
    orgNames[org.id] = org.name;
    const option = document.createElement("option");
    option.value = org.id;
    option.textContent = org.name;
    select.appendChild(option);
  }
}

async function load() {
  const res = await fetch("/collections/contacts/records?limit=200",
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  const body = await res.json();
  renderCards(body.records || []);
}

async function search(event) {
  const query = event.target.value.trim();
  if (!query) { load(); return; }
  const res = await fetch(`/api/search?q=${encodeURIComponent(query)}&collections=contacts&limit=50`,
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) return;
  const body = await res.json();
  renderCards((body.results || {}).contacts || []);
}

async function create(event) {
  event.preventDefault();
  const form = event.target;
  const fields = form.elements;
  const record = {id: crypto.randomUUID(), owner_id: OWNER_ID,
                  first_name: fields["first_name"].value.trim(),
                  last_name: fields["last_name"].value.trim(),
                  email: fields["email"].value.trim(),
                  phone: fields["phone"].value.trim()};
  if (fields["organization"].value) record.organization_id = fields["organization"].value;
  const res = await fetch("/collections/contacts/records", {
    method: "POST", credentials: "same-origin",
    headers: {"content-type": "application/json", accept: "application/json"},
    body: JSON.stringify(record),
  });
  const body = await res.json();
  document.getElementById("form-error").textContent = res.ok ? "" : (body.error || "Save failed");
  if (res.ok) { form.reset(); load(); }
}

document.getElementById("capture-form").addEventListener("submit", create);
document.getElementById("search-box").addEventListener("input", search);
loadOrgs();
load();
"""


def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_contacts served", user_id=user_id or "anonymous")

    if not user_id:
        body = '<p class="hint"><a href="/login?next=/contacts">Sign in</a> to see your contacts.</p>'
        script = ""
    else:
        body = """
<form class="capture" id="capture-form">
<input name="first_name" placeholder="First name" required maxlength="80">
<input name="last_name" placeholder="Last name" maxlength="80">
<input name="email" placeholder="Email" maxlength="254">
<input name="phone" placeholder="Phone" maxlength="40">
<select name="organization" id="org-select" class="full"><option value="">No organization</option></select>
<button type="submit" class="btn primary">Add Contact</button>
<div class="error" id="form-error"></div>
</form>
<input class="search" id="search-box" placeholder="Search contacts&hellip;" autocomplete="off">
<div class="cards" id="cards"><p class="hint">loading&hellip;</p></div>
"""
        script = f"<script>const OWNER_ID = {user_id!r};{_SCRIPT}</script>"

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else '<a href="/login?next=/contacts">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Contacts</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header class="app"><h1>Contacts</h1><div class="who">{who}</div></header>
{body}
</div>
{script}
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
