"""Orders page — built from metadata, not markup.

The form and the list both come from the schema via the shared generators
(/form -> window.dbbasicForm, /list -> window.dbbasicList). This page is
just the chrome: a breadcrumb, an Add button, a search/sort toolbar, and
two mount points. Add or edit opens the schema-driven form; the list
auto-refreshes over the websocket. No hand-written fields or rows. Mirrors
packages/app-invoices/objects/site/invoices.py.

Money is formatted for display only in this page's own JS (cents -> a
"$123.45"-shaped string); the stored value stays an integer number of
cents everywhere else, per 00-doctrine-and-contract.md.
"""

_SCRIPT = """
function money(cents, currency) {
  const n = Number(cents || 0);
  const amount = (n / 100).toFixed(2);
  return (currency || "USD") + " " + amount;
}
const panel = document.getElementById("formpanel");
const list = window.dbbasicList("orders", {
  mount: "#list", search: "#search", sort: "#sort", owner: OWNER_ID,
  title: (r) => r.number || "(no number)",
  href: (r) => "/orders/" + r.id,
  subtitle: (r) => [r.customer_name, r.status, money(r.total_cents, r.currency)]
    .filter(Boolean).join(" \\u00b7 "),
  created: (r) => r.created_at, onEdit: (r) => openForm(r),
});
function openForm(record) {
  document.getElementById("formtitle").textContent = record ? "Edit Order" : "New Order";
  panel.style.display = "block";
  window.dbbasicForm("orders", {
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
    _logger.info("site_orders served", user_id=user_id or "anonymous")

    if not user_id:
        body = '<p class="hint"><a href="/login?next=/orders">Sign in</a> to see your orders.</p>'
        script = ""
    else:
        body = """
<div class="breadcrumb"><a href="/">Home</a> / Orders</div>
<div class="pagehead"><h1>Orders</h1><button class="btn primary" id="add">+ New Order</button></div>
<div id="formpanel" style="display:none; margin-bottom:1rem">
  <h2 id="formtitle" style="font-size:1rem; margin:0 0 0.5rem">New Order</h2>
  <div id="formmount"></div>
</div>
<div class="toolbar">
  <input class="search grow" id="search" placeholder="Search orders&hellip;" autocomplete="off">
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
        else '<a href="/login?next=/orders">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Orders</title>
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
