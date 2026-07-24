"""Payments page — built from metadata, not markup.

The list (a live table: invoice, amount, method, status with filters) and the
record form both come from the schema via the shared generators; this page is
only chrome. The real business logic lives elsewhere by doctrine
(docs/business-logic-patterns.md): hook_payments gates overpayment,
hook_refunds gates and stamps refunds, and the invoice's paid/balance amounts
are rollups/formulas that update live as payments land.
"""

_SCRIPT = """
const panel = document.getElementById("formpanel");
const list = window.dbbasicList("payments", {
  mount: "#list", search: "#search", sort: "#sort", owner: OWNER_ID,
  title: (r) => (r.reference || r.method || "payment") + " — " + (r.amount_cents || "0") + "c",
  created: (r) => r.created_at, onEdit: (r) => openForm(r),
});
function openForm(record) {
  document.getElementById("formtitle").textContent = record ? "Edit Payment" : "New Payment";
  panel.style.display = "block";
  window.dbbasicForm("payments", {
    mount: "#formmount", record: record, owner: OWNER_ID,
    onSaved: () => { panel.style.display = "none"; list.reload(); },
    onCancel: () => { panel.style.display = "none"; },
  });
}
document.getElementById("add").addEventListener("click", () => openForm(null));
"""


def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_payments served", user_id=user_id or "anonymous")

    if not user_id:
        body = '<p class="hint"><a href="/login?next=/payments">Sign in</a> to see payments.</p>'
        script = ""
    else:
        body = """
<div class="breadcrumb"><a href="/">Home</a> / Payments</div>
<div class="pagehead"><h1>Payments</h1><button class="btn primary" id="add">+ New Payment</button></div>
<div id="formpanel" style="display:none; margin-bottom:1rem">
  <h2 id="formtitle" style="font-size:1rem; margin:0 0 0.5rem">New Payment</h2>
  <div id="formmount"></div>
</div>
<div class="toolbar">
  <input class="search grow" id="search" placeholder="Search payments&hellip;" autocomplete="off">
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
        else '<a href="/login?next=/payments">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Payments</title>
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
