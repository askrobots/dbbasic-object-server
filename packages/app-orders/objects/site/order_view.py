"""Single-order detail page: owner sees full detail, edits the order, and
manages its line items. No public/anonymous path in this slice -- same
posture as app-invoices' invoice_view.py: this page only ever serves the
owner, the same way the row-filtered collection API only ever returns the
order to its owner (no public read rule is granted on the orders
collection).

Served through a site route like /orders/{order_id:uuid} -- see
packages/app-invoices/objects/site/invoice_view.py for the identical
pattern this mirrors (itself following app-notes/objects/site/note_view.py):
the browser fetches the record with the visitor's session cookie, so the
permission policy decides visibility.

Money is formatted for display only in this page's own JS; the stored
value stays an integer number of cents everywhere else.
"""

_STYLE = """
.totals { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.25rem 1.5rem;
  margin: 1rem 0; max-width: 26rem; }
.totals .row { display: flex; justify-content: space-between; border-top: 1px solid var(--line, #38384a);
  padding: 0.35rem 0; }
.totals .row:first-child { border-top: none; }
.totals .row.grand { font-weight: 700; }
.lines-table { width: 100%; border-collapse: collapse; margin: 0.5rem 0 1.5rem; }
.lines-table th, .lines-table td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--line, #38384a); }
.lines-table th { font-weight: 600; }
.lines-table td.num, .lines-table th.num { text-align: right; }
#addline .field[data-hidden="true"] { display: none; }
"""

_SCRIPT = """
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const el = (id) => document.getElementById(id);
function money(cents, currency) {
  const n = Number(cents || 0);
  const amount = (n / 100).toFixed(2);
  return (currency || "USD") + " " + amount;
}
let order = null;

function renderOrder() {
  el("ord-title").textContent = order.number || "(no number)";
  el("ord-meta").textContent = [order.doc_type, order.status].filter(Boolean).join(" \\u00b7 ");
  el("ord-customer").innerHTML = "<strong>" + esc(order.customer_name || "") + "</strong>"
    + (order.customer_email ? "<br>" + esc(order.customer_email) : "");
  el("ord-dates").textContent = ["ordered " + (order.order_date || "\\u2014"),
    "expected " + (order.expected_date || "\\u2014")].join(" \\u00b7 ");
  el("t-subtotal").textContent = money(order.subtotal_cents, order.currency);
  el("t-tax").textContent = money(order.tax_cents, order.currency);
  el("t-total").textContent = money(order.total_cents, order.currency);
  el("ord-notes").textContent = order.notes || "";
  const mine = VIEWER_ID && order.owner_id === VIEWER_ID;
  el("owner-tools").style.display = mine ? "block" : "none";
}

async function loadOrder() {
  const res = await fetch(`/collections/orders/records/${ORDER_ID}`,
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) {
    el("card").innerHTML = VIEWER_ID
      ? '<p class="hint">This order does not exist or is not yours.</p>'
      : `<p class="hint"><a href="/login?next=/orders/${ORDER_ID}">Sign in</a> to view this order.</p>`;
    el("lines-section").style.display = "none";
    return;
  }
  const body = await res.json();
  order = body.record || body;
  renderOrder();
  loadLines();
}

async function loadLines() {
  const res = await fetch("/collections/order_lines/records?limit=500",
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) return;
  const body = await res.json();
  const lines = (body.records || []).filter((r) => r.order_id === ORDER_ID);
  const rows = lines.map((r) => `<tr>
    <td>${esc(r.description)}</td>
    <td class="num">${esc(r.quantity)}</td>
    <td class="num">${money(r.unit_price_cents, order && order.currency)}</td>
    <td class="num">${money(r.line_tax_cents, order && order.currency)}</td>
    <td class="num">${money(r.line_total_cents, order && order.currency)}</td>
  </tr>`).join("");
  el("lines-body").innerHTML = rows || '<tr><td colspan="5" class="hint">No line items yet.</td></tr>';
}

async function initAddLineForm() {
  await window.dbbasicForm("order_lines", {
    mount: "#addlinemount", owner: VIEWER_ID,
    onSaved: () => { loadLines(); },
  });
  // order_id is a required relation field on order_lines (see
  // schemas/order_lines.json) -- the generic form generator has no
  // "prefill and lock a field to the page context" hook, so this page
  // sets and hides it after render instead of building a bespoke create
  // object for one field (same trade-off app-invoices' invoice_view.py
  // documents for invoice_id).
  const field = document.querySelector('#addlinemount select[name="order_id"]');
  if (field) {
    field.value = ORDER_ID;
    const wrapper = field.closest(".field");
    if (wrapper) wrapper.dataset.hidden = "true";
  }
}

loadOrder();
if (VIEWER_ID) initAddLineForm();

// Realtime: auto-refresh when either collection changes (another tab,
// user, or agent) -- most importantly, when order_totals restamps this
// order's own totals after a line change.
(function () {
  let _lt = null;
  const reload = () => { clearTimeout(_lt); _lt = setTimeout(loadOrder, 150); };
  (function wait() {
    if (window.dbbasicSubscribe) {
      window.dbbasicSubscribe("orders", reload);
      window.dbbasicSubscribe("order_lines", reload);
    } else setTimeout(wait, 300);
  })();
})();
"""


import re

_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def GET(request):
    order_id = str(request.get("order_id") or "").strip()
    if order_id and not _RECORD_ID_RE.fullmatch(order_id):
        order_id = ""
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_order_view served", order_id=order_id or "missing",
                 user_id=user_id or "anonymous")

    if not order_id:
        return {
            "content_type": "text/html; charset=utf-8",
            "body": "<p>Order not found. <a href='/orders'>Back to orders</a></p>",
            "status": 404,
        }

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else f'<a href="/login?next=/orders/{order_id}">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Order</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header class="app"><h1><a href="/orders">Orders</a> / order</h1><div class="who">{who}</div></header>
<div class="card" id="card">
<h2 id="ord-title">loading&hellip;</h2>
<div class="meta" id="ord-meta"></div>
<div id="ord-customer" style="margin-top:0.75rem"></div>
<div class="meta" id="ord-dates"></div>
<div class="totals">
  <div class="row"><span>Subtotal</span><span id="t-subtotal"></span></div>
  <div class="row"><span>Tax</span><span id="t-tax"></span></div>
  <div class="row grand"><span>Total</span><span id="t-total"></span></div>
</div>
<p id="ord-notes" class="meta"></p>
</div>
<div id="lines-section">
  <h3>Line Items</h3>
  <table class="lines-table">
    <thead><tr><th>Description</th><th class="num">Qty</th><th class="num">Unit</th>
      <th class="num">Tax</th><th class="num">Line Total</th></tr></thead>
    <tbody id="lines-body"><tr><td colspan="5" class="hint">loading&hellip;</td></tr></tbody>
  </table>
  <div class="owner-tools" id="owner-tools" style="display:none">
    <h4>Add Line</h4>
    <div id="addlinemount"></div>
  </div>
</div>
</div>
<script>const ORDER_ID = {order_id!r}; const VIEWER_ID = {(user_id or "")!r};</script>
<script src="/form"></script>
<script>{_SCRIPT}</script>
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
