"""Inbox page — built from metadata, not markup.

Lists the signed-in owner's message_threads via the shared list generator
(/list -> window.dbbasicList), same shape as app-invoices' invoices.py and
app-forum's forum.py: a breadcrumb, a toolbar, a mount point, nothing
hand-rendered per row. Sorting reuses the generator's built-in
newest/oldest toggle, which sorts by created_at (thread creation order) —
window.dbbasicList's sort is hardcoded to that field, not configurable per
page (see packages/app-theme/objects/site/list.py's sortList). Ordering by
last_message_at (most-recently-active thread first) would need a generator
enhancement; out of scope for a data-model package. last_message_at is
still carried on every thread record and shown in each row's subtitle.

The "Save Draft" button opens a message_drafts create form with no
thread_id preset (a brand-new compose, not yet attached to any thread) —
see dbbasic-package.json: nothing is ever sent from this package, drafts
are data only. Served through a site route /inbox -> site_inbox,
documented in dbbasic-package.json, not seeded, same precedent as
app-invoices' /invoices/{invoice_id:uuid}.

A mailbox is private (see permissions/rules.json: owner-scoped CRUD, no
public read on any collection in this package). Anonymous visitors get a
sign-in prompt only.
"""

_SCRIPT = """
const panel = document.getElementById("draftpanel");
const list = window.dbbasicList("message_threads", {
  mount: "#list", search: "#search", sort: "#sort", owner: OWNER_ID,
  title: (r) => r.subject || "(no subject)",
  href: (r) => "/inbox/" + r.id,
  subtitle: (r) => {
    const bits = [];
    if (r.participant_summary) bits.push(r.participant_summary);
    bits.push((r.message_count || "0") + " messages");
    if (r.is_read !== "true") bits.push("unread");
    if (r.is_starred === "true") bits.push("starred");
    if (r.is_archived === "true") bits.push("archived");
    return bits.join(" \\u00b7 ");
  },
  created: (r) => r.last_message_at || r.created_at,
});
function openDraftForm() {
  document.getElementById("drafttitle").textContent = "Save Draft";
  panel.style.display = "block";
  window.dbbasicForm("message_drafts", {
    mount: "#draftformmount", owner: OWNER_ID,
    onSaved: () => { panel.style.display = "none"; },
    onCancel: () => { panel.style.display = "none"; },
  });
}
document.getElementById("adddraft").addEventListener("click", openDraftForm);
"""


def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_inbox served", user_id=user_id or "anonymous")

    if not user_id:
        body = '<p class="hint"><a href="/login?next=/inbox">Sign in</a> to see your inbox.</p>'
        script = ""
    else:
        body = """
<div class="breadcrumb"><a href="/">Home</a> / Inbox</div>
<div class="pagehead"><h1>Inbox</h1><button class="btn primary" id="adddraft">+ Save Draft</button></div>
<p class="hint">Save Draft only saves a draft record -- nothing is sent. Composing and sending
a real message needs mail transport (IMAP/SMTP), which this package does not build.</p>
<div id="draftpanel" style="display:none; margin-bottom:1rem">
  <h2 id="drafttitle" style="font-size:1rem; margin:0 0 0.5rem">Save Draft</h2>
  <div id="draftformmount"></div>
</div>
<div class="toolbar">
  <input class="search grow" id="search" placeholder="Search threads&hellip;" autocomplete="off">
  <select id="sort"><option value="newest">Newest</option><option value="oldest">Oldest</option></select>
</div>
<div id="list"><div class="state">loading&hellip;</div></div>
"""
        script = (
            f"<script>const OWNER_ID = {user_id!r};</script>"
            '<script src="/list"></script><script src="/form"></script>'
            f"<script>{_SCRIPT}</script>"
        )

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else '<a href="/login?next=/inbox">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Inbox</title>
<link rel="stylesheet" href="/style">
</head>
<body>
<div class="wrap">
<header class="app"><h1><a href="/">DBBASIC</a></h1><div class="who">{who}</div></header>
{body}
</div>
{script}
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
