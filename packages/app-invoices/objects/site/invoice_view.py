"""Single-invoice detail page: owner sees full detail, edits the invoice,
and manages its line items. No public/anonymous path in this slice --
plan/vocabulary/20-invoice-spec.md's tokenized /i/{public_token} view is a
later slice (see dbbasic-package.json's description); this page only ever
serves the owner, the same way the row-filtered collection API only ever
returns the invoice to its owner (no public read rule is granted on the
invoices collection).

Served through a site route like /invoices/{invoice_id:uuid} -- see
app-notes/objects/site/note_view.py for the identical pattern this
mirrors: the browser fetches the record with the visitor's session
cookie, so the permission policy decides visibility.

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
let invoice = null;

function renderInvoice() {
  el("inv-title").textContent = invoice.number || "(no number)";
  el("inv-meta").textContent = [invoice.doc_type, invoice.status].filter(Boolean).join(" \\u00b7 ");
  el("inv-customer").innerHTML = "<strong>" + esc(invoice.customer_name || "") + "</strong>"
    + (invoice.customer_email ? "<br>" + esc(invoice.customer_email) : "")
    + (invoice.customer_address ? "<br>" + esc(invoice.customer_address).replace(/\\n/g, "<br>") : "");
  el("inv-dates").textContent = ["issued " + (invoice.issue_date || "\\u2014"),
    "due " + (invoice.due_date || "\\u2014")].join(" \\u00b7 ");
  el("t-subtotal").textContent = money(invoice.subtotal_cents, invoice.currency);
  el("t-tax").textContent = money(invoice.tax_cents, invoice.currency);
  el("t-total").textContent = money(invoice.total_cents, invoice.currency);
  el("t-paid").textContent = money(invoice.amount_paid_cents, invoice.currency);
  el("t-balance").textContent = money(invoice.balance_due_cents, invoice.currency);
  el("inv-notes").textContent = invoice.notes || "";
  const mine = VIEWER_ID && invoice.owner_id === VIEWER_ID;
  el("owner-tools").style.display = mine ? "block" : "none";
}

async function loadInvoice() {
  const res = await fetch(`/collections/invoices/records/${INVOICE_ID}`,
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) {
    el("card").innerHTML = VIEWER_ID
      ? '<p class="hint">This invoice does not exist or is not yours.</p>'
      : `<p class="hint"><a href="/login?next=/invoices/${INVOICE_ID}">Sign in</a> to view this invoice.</p>`;
    el("lines-section").style.display = "none";
    return;
  }
  const body = await res.json();
  invoice = body.record || body;
  renderInvoice();
  loadLines();
}

async function loadLines() {
  const res = await fetch("/collections/invoice_lines/records?limit=500",
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) return;
  const body = await res.json();
  const lines = (body.records || []).filter((r) => r.invoice_id === INVOICE_ID);
  const rows = lines.map((r) => `<tr>
    <td>${esc(r.description)}</td>
    <td class="num">${esc(r.quantity)}</td>
    <td class="num">${money(r.unit_price_cents, invoice && invoice.currency)}</td>
    <td class="num">${money(r.line_tax_cents, invoice && invoice.currency)}</td>
    <td class="num">${money(r.line_total_cents, invoice && invoice.currency)}</td>
  </tr>`).join("");
  el("lines-body").innerHTML = rows || '<tr><td colspan="5" class="hint">No line items yet.</td></tr>';
}

async function initAddLineForm() {
  await window.dbbasicForm("invoice_lines", {
    mount: "#addlinemount", owner: VIEWER_ID,
    onSaved: () => { loadLines(); },
  });
  // invoice_id is a required relation field on invoice_lines (see
  // schemas/invoice_lines.json) -- the generic form generator has no
  // "prefill and lock a field to the page context" hook, so this page
  // sets and hides it after render instead of building a bespoke create
  // object for one field (plan/vocabulary/20-invoice-spec.md's own
  // invoice_new is the fuller version of this same trade-off).
  const field = document.querySelector('#addlinemount select[name="invoice_id"]');
  if (field) {
    field.value = INVOICE_ID;
    const wrapper = field.closest(".field");
    if (wrapper) wrapper.dataset.hidden = "true";
  }
}

loadInvoice();
if (VIEWER_ID) initAddLineForm();

// Realtime: auto-refresh when either collection changes (another tab,
// user, or agent) -- most importantly, when invoice_totals restamps this
// invoice's own totals after a line change.
(function () {
  let _lt = null;
  const reload = () => { clearTimeout(_lt); _lt = setTimeout(loadInvoice, 150); };
  (function wait() {
    if (window.dbbasicSubscribe) {
      window.dbbasicSubscribe("invoices", reload);
      window.dbbasicSubscribe("invoice_lines", reload);
    } else setTimeout(wait, 300);
  })();
})();
"""


import re

_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def GET(request):
    invoice_id = str(request.get("invoice_id") or "").strip()
    if invoice_id and not _RECORD_ID_RE.fullmatch(invoice_id):
        invoice_id = ""
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_invoice_view served", invoice_id=invoice_id or "missing",
                 user_id=user_id or "anonymous")

    if not invoice_id:
        return {
            "content_type": "text/html; charset=utf-8",
            "body": "<p>Invoice not found. <a href='/invoices'>Back to invoices</a></p>",
            "status": 404,
        }

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else f'<a href="/login?next=/invoices/{invoice_id}">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Invoice</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header class="app"><h1><a href="/invoices">Invoices</a> / invoice</h1><div class="who">{who}</div></header>
<div class="card" id="card">
<h2 id="inv-title">loading&hellip;</h2>
<div class="meta" id="inv-meta"></div>
<div id="inv-customer" style="margin-top:0.75rem"></div>
<div class="meta" id="inv-dates"></div>
<div class="totals">
  <div class="row"><span>Subtotal</span><span id="t-subtotal"></span></div>
  <div class="row"><span>Tax</span><span id="t-tax"></span></div>
  <div class="row grand"><span>Total</span><span id="t-total"></span></div>
  <div class="row"><span>Paid</span><span id="t-paid"></span></div>
  <div class="row grand"><span>Balance Due</span><span id="t-balance"></span></div>
</div>
<p id="inv-notes" class="meta"></p>
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
<script>const INVOICE_ID = {invoice_id!r}; const VIEWER_ID = {(user_id or "")!r};</script>
<script src="/form"></script>
<script>{_SCRIPT}</script>
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
