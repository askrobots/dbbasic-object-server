"""Owner edit page for the signed-in user's own profile: /profile/edit ->
site_profile_edit. Documented route (not seeded), same pattern as every
other site route in this package -- see objects/site/profile.py's docstring.

schemas/profiles.json declares one profile per user with id == the owning
account's own id -- deliberately NOT a server-generated record id. The
shared /form generator (window.dbbasicForm, packages/app-theme/objects/site/
form.py) always assigns a fresh crypto.randomUUID() to a new record on
create -- it has no hook to pin the id to a caller-chosen value, and this
package does not modify app-theme. So the FIRST save (creating the profile)
is a small hand-rolled form built directly in this page's own script, POSTed
with an explicit id == the signed-in user's id; every save AFTER that uses
window.dbbasicForm normally in edit mode (a PUT against the existing
record's id, which dbbasicForm never touches). This is the same kind of
small client-side glue app-forum's forum_topic.py uses to lock topic_id on
its reply form -- reaching for a few lines of page-local JS where the
generic generator's contract does not fit, rather than special-casing the
shared generator for one page.
"""

_SCRIPT = """
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const el = (id) => document.getElementById(id);

const CREATE_FIELDS = [
  {name: "display_name", label: "Display Name", control: "text"},
  {name: "bio", label: "Bio", control: "textarea"},
  {name: "skills", label: "Skills", control: "text",
   help: "Free text, e.g. \\"Python, woodworking, tax prep\\""},
  {name: "experience", label: "Experience", control: "textarea"},
  {name: "location", label: "Location", control: "text"},
  {name: "education", label: "Education", control: "text"},
  {name: "website", label: "Website", control: "text"},
  {name: "social_links", label: "Social Links", control: "textarea",
   help: "One URL per line"},
  {name: "is_active", label: "Active", control: "checkbox"},
];

function createFormHtml() {
  const rows = CREATE_FIELDS.map((f) => {
    if (f.control === "checkbox") {
      return '<div class="field"><label class="switch"><input type="checkbox" name="'
        + f.name + '" checked> ' + esc(f.label) + '</label></div>';
    }
    const ctrl = f.control === "textarea"
      ? '<textarea name="' + f.name + '" rows="3"></textarea>'
      : '<input type="text" name="' + f.name + '">';
    return '<div class="field"><label>' + esc(f.label) + '</label>' + ctrl
      + (f.help ? '<div class="help">' + esc(f.help) + '</div>' : "") + '</div>';
  }).join("");
  return '<form class="genform stack" id="createform">' + rows
    + '<div class="formactions"><button type="submit" class="btn primary">Create Profile</button>'
    + '<span class="error" id="create-error"></span></div></form>';
}

async function initCreateForm() {
  el("formmount").innerHTML = createFormHtml();
  document.getElementById("createform").addEventListener("submit", async (e) => {
    e.preventDefault();
    const rec = {id: VIEWER_ID};
    for (const f of CREATE_FIELDS) {
      const input = e.target.elements[f.name];
      rec[f.name] = f.control === "checkbox" ? (input.checked ? "true" : "false") : input.value;
    }
    const res = await fetch("/collections/profiles/records", {
      method: "POST", credentials: "same-origin",
      headers: {"content-type": "application/json", accept: "application/json"},
      body: JSON.stringify(rec),
    });
    const body = await res.json();
    if (!res.ok) { el("create-error").textContent = body.error || "Save failed"; return; }
    initEditForm(body.record || rec);
  });
}

async function initEditForm(record) {
  el("pagehead-sub").textContent = "Editing your profile";
  el("viewlink").style.display = "inline";
  await window.dbbasicForm("profiles", {
    mount: "#formmount", record: record, owner: VIEWER_ID,
    onSaved: (saved) => { el("save-note").textContent = "Saved."; setTimeout(() => { el("save-note").textContent = ""; }, 2000); },
  });
}

async function load() {
  const res = await fetch(`/collections/profiles/records/${encodeURIComponent(VIEWER_ID)}`,
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (res.ok) {
    const body = await res.json();
    initEditForm(body.record || body);
  } else {
    el("pagehead-sub").textContent = "You don't have a profile yet -- create one below.";
    initCreateForm();
  }
}
load();
"""


def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_profile_edit served", user_id=user_id or "anonymous")

    if not user_id:
        html = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Edit Profile</title>
<link rel="stylesheet" href="/style">
</head>
<body>
<div class="wrap narrow">
<header class="app"><a href="/">DBBASIC</a></header>
<p class="hint"><a href="/login?next=/profile/edit">Sign in</a> to edit your profile.</p>
</div>
</body>
</html>"""
        return {"content_type": "text/html; charset=utf-8", "body": html}

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Edit Profile</title>
<link rel="stylesheet" href="/style">
</head>
<body>
<div class="wrap narrow">
<header class="app"><a href="/">DBBASIC</a><div class="who">signed in as <strong>{user_id}</strong></div></header>
<div class="pagehead"><h1>Your Profile</h1></div>
<p id="pagehead-sub" class="hint">loading&hellip;</p>
<p><a id="viewlink" style="display:none" href="/u/{user_id}">View your public profile &rarr;</a></p>
<div id="formmount"></div>
<span id="save-note" class="hint"></span>
</div>
<script>const VIEWER_ID = {user_id!r};</script>
<script src="/form"></script>
<script>{_SCRIPT}</script>
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
