"""Single-product detail page: owner sees full detail and edits the
product. No public/anonymous read path in this slice -- v1 keeps products
owner-scoped like app-invoices (see dbbasic-package.json); a public
storefront view is deferred, same posture app-invoices documents for its
own tokenized public view. This page only ever serves the owner, the same
way the row-filtered collection API only ever returns the product to its
owner (no public read rule is granted on the products collection).

Served through a site route like /products/{product_id:uuid} -- see
app-notes/objects/site/note_view.py and app-invoices/objects/site/
invoice_view.py for the identical pattern this mirrors: the browser
fetches the record with the visitor's session cookie, so the permission
policy decides visibility.

Money is formatted for display only in this page's own JS; the stored
value stays an integer number of cents everywhere else.
"""

_STYLE = """
.facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.25rem 1.5rem;
  margin: 1rem 0; max-width: 30rem; }
.facts .row { display: flex; justify-content: space-between; border-top: 1px solid var(--line, #38384a);
  padding: 0.35rem 0; }
.facts .row:first-child { border-top: none; }
.asset-facts { display: none; }
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
let product = null;

function renderProduct() {
  el("p-title").textContent = product.name || "(no name)";
  el("p-meta").textContent = [product.sku, product.product_type,
    product.is_active === "false" ? "inactive" : "active"].filter(Boolean).join(" \\u00b7 ");
  el("p-description").textContent = product.description || "";
  el("f-price").textContent = money(product.price_cents, product.currency);
  el("f-cost").textContent = money(product.cost_cents, product.currency);
  el("f-unit").textContent = product.unit || "\\u2014";
  el("f-income").textContent = product.income_account || "\\u2014";
  el("f-expense").textContent = product.expense_account || "\\u2014";
  el("f-digital").textContent = product.digital_file_id || "\\u2014";
  const isAsset = product.product_type === "asset";
  el("asset-facts").style.display = isAsset ? "grid" : "none";
  if (isAsset) {
    el("f-life").textContent = product.useful_life_months || "\\u2014";
    el("f-purchased").textContent = product.purchase_date || "\\u2014";
    el("f-salvage").textContent = money(product.salvage_value_cents, product.currency);
    el("f-method").textContent = product.depreciation_method || "\\u2014";
    el("f-status").textContent = product.asset_status || "\\u2014";
  }
  const mine = VIEWER_ID && product.owner_id === VIEWER_ID;
  el("owner-tools").style.display = mine ? "block" : "none";
}

async function loadProduct() {
  const res = await fetch(`/collections/products/records/${PRODUCT_ID}`,
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) {
    el("card").innerHTML = VIEWER_ID
      ? '<p class="hint">This product does not exist or is not yours.</p>'
      : `<p class="hint"><a href="/login?next=/products/${PRODUCT_ID}">Sign in</a> to view this product.</p>`;
    return;
  }
  const body = await res.json();
  product = body.record || body;
  renderProduct();
}

async function initEditForm() {
  await window.dbbasicForm("products", {
    mount: "#editmount", record: product, owner: VIEWER_ID,
    onSaved: () => { loadProduct(); },
  });
}

loadProduct().then(() => { if (VIEWER_ID && product) initEditForm(); });

// Realtime: auto-refresh when this collection changes (another tab,
// user, or agent).
(function () {
  let _lt = null;
  const reload = () => { clearTimeout(_lt); _lt = setTimeout(loadProduct, 150); };
  (function wait() {
    if (window.dbbasicSubscribe) window.dbbasicSubscribe("products", reload);
    else setTimeout(wait, 300);
  })();
})();
"""


import re

_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def GET(request):
    product_id = str(request.get("product_id") or "").strip()
    if product_id and not _RECORD_ID_RE.fullmatch(product_id):
        product_id = ""
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_product_view served", product_id=product_id or "missing",
                 user_id=user_id or "anonymous")

    if not product_id:
        return {
            "content_type": "text/html; charset=utf-8",
            "body": "<p>Product not found. <a href='/products'>Back to products</a></p>",
            "status": 404,
        }

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else f'<a href="/login?next=/products/{product_id}">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Product</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header class="app"><h1><a href="/products">Products</a> / product</h1><div class="who">{who}</div></header>
<div class="card" id="card">
<h2 id="p-title">loading&hellip;</h2>
<div class="meta" id="p-meta"></div>
<p id="p-description"></p>
<div class="facts">
  <div class="row"><span>Price</span><span id="f-price"></span></div>
  <div class="row"><span>Cost</span><span id="f-cost"></span></div>
  <div class="row"><span>Unit</span><span id="f-unit"></span></div>
  <div class="row"><span>Income Account</span><span id="f-income"></span></div>
  <div class="row"><span>Expense Account</span><span id="f-expense"></span></div>
  <div class="row"><span>Digital File</span><span id="f-digital"></span></div>
</div>
<div class="facts asset-facts" id="asset-facts">
  <div class="row"><span>Useful Life (months)</span><span id="f-life"></span></div>
  <div class="row"><span>Purchased</span><span id="f-purchased"></span></div>
  <div class="row"><span>Salvage Value</span><span id="f-salvage"></span></div>
  <div class="row"><span>Depreciation Method</span><span id="f-method"></span></div>
  <div class="row"><span>Asset Status</span><span id="f-status"></span></div>
</div>
<div class="owner-tools" id="owner-tools" style="display:none">
  <h4>Edit</h4>
  <div id="editmount"></div>
</div>
</div>
</div>
<script>const PRODUCT_ID = {product_id!r}; const VIEWER_ID = {(user_id or "")!r};</script>
<script src="/form"></script>
<script>{_SCRIPT}</script>
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
