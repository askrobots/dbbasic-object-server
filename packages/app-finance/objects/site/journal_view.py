"""Single-journal detail page: owner sees full detail, edits the journal
(including posting it draft->posted through the generic form, guarded by
schemas/fin_journals.json's status transition), and manages its lines.

Served through a site route like /journals/{journal_id:uuid} -- see
app-invoices/objects/site/invoice_view.py for the identical pattern this
mirrors: the browser fetches the record with the visitor's session
cookie, so the permission policy decides visibility. Not seeded by this
package (same precedent as app-invoices' /invoices/{invoice_id:uuid} --
see dbbasic-package.json's description).

The debit/credit totals and is_balanced shown here are computed in this
page's own JS by summing the already-fetched lines -- the exact same
fold object_finance.journal_totals() performs, just done client-side so
the totals refresh instantly as lines are added, with no extra round
trip. Nothing writes these numbers back onto the journal record: there
is no totals-stamping handler in this package (unlike app-invoices'
invoice_totals), matching the source's own computed-property design (see
object_finance.py's module docstring and schemas/fin_journals.json's
status field help). Money is formatted for display only; the stored
value stays an integer number of cents everywhere else.
"""

_STYLE = """
.totals { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.25rem 1.5rem;
  margin: 1rem 0; max-width: 26rem; }
.totals .row { display: flex; justify-content: space-between; border-top: 1px solid var(--line, #38384a);
  padding: 0.35rem 0; }
.totals .row:first-child { border-top: none; }
.totals .row.grand { font-weight: 700; }
.balanced { color: #52d273; }
.unbalanced { color: #ff6b6b; }
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
let journal = null;

function renderJournal() {
  el("j-title").textContent = journal.description || "(no description)";
  el("j-meta").textContent = [journal.date, journal.status].filter(Boolean).join(" \\u00b7 ");
  el("j-ref").textContent = [journal.contact_id ? "contact " + journal.contact_id : "",
    journal.reference ? "ref " + journal.reference : ""].filter(Boolean).join(" \\u00b7 ");
  const mine = VIEWER_ID && journal.owner_id === VIEWER_ID;
  el("owner-tools").style.display = mine ? "block" : "none";
  el("owner-tools-lines").style.display = mine ? "block" : "none";
}

async function loadJournal() {
  const res = await fetch(`/collections/fin_journals/records/${JOURNAL_ID}`,
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) {
    el("card").innerHTML = VIEWER_ID
      ? '<p class="hint">This journal does not exist or is not yours.</p>'
      : `<p class="hint"><a href="/login?next=/journals/${JOURNAL_ID}">Sign in</a> to view this journal.</p>`;
    el("lines-section").style.display = "none";
    return;
  }
  const body = await res.json();
  journal = body.record || body;
  renderJournal();
  loadLines();
}

async function loadLines() {
  const res = await fetch("/collections/fin_journal_lines/records?limit=500",
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) return;
  const body = await res.json();
  const lines = (body.records || []).filter((r) => r.journal_id === JOURNAL_ID);

  // The same fold object_finance.journal_totals() performs server-side,
  // done here so the totals refresh instantly as lines are added.
  let totalDebitsCents = 0;
  let totalCreditsCents = 0;
  for (const line of lines) {
    totalDebitsCents += Number(line.debit_cents || 0);
    totalCreditsCents += Number(line.credit_cents || 0);
  }
  const isBalanced = totalDebitsCents === totalCreditsCents;
  el("t-debits").textContent = money(totalDebitsCents, journal && journal.currency);
  el("t-credits").textContent = money(totalCreditsCents, journal && journal.currency);
  const balEl = el("t-balanced");
  balEl.textContent = isBalanced ? "Balanced" : "Not balanced";
  balEl.className = isBalanced ? "balanced" : "unbalanced";

  const rows = lines.map((r) => `<tr>
    <td>${esc(r.account_id)}</td>
    <td>${esc(r.memo)}</td>
    <td class="num">${money(r.debit_cents, journal && journal.currency)}</td>
    <td class="num">${money(r.credit_cents, journal && journal.currency)}</td>
  </tr>`).join("");
  el("lines-body").innerHTML = rows || '<tr><td colspan="4" class="hint">No lines yet.</td></tr>';
}

async function initAddLineForm() {
  await window.dbbasicForm("fin_journal_lines", {
    mount: "#addlinemount", owner: VIEWER_ID,
    onSaved: () => { loadLines(); },
  });
  // journal_id is a required relation field on fin_journal_lines -- the
  // generic form generator has no "prefill and lock a field to the page
  // context" hook, so this page sets and hides it after render instead
  // of building a bespoke create object for one field. Same trade-off as
  // app-invoices/objects/site/invoice_view.py's initAddLineForm.
  const field = document.querySelector('#addlinemount select[name="journal_id"]');
  if (field) {
    field.value = JOURNAL_ID;
    const wrapper = field.closest(".field");
    if (wrapper) wrapper.dataset.hidden = "true";
  }
}

async function initEditForm() {
  await window.dbbasicForm("fin_journals", {
    mount: "#editmount", record: journal, owner: VIEWER_ID,
    onSaved: () => { loadJournal(); },
  });
}

loadJournal().then(() => { if (VIEWER_ID && journal) { initAddLineForm(); initEditForm(); } });

// Realtime: auto-refresh when either collection changes (another tab,
// user, or agent).
(function () {
  let _lt = null;
  const reload = () => { clearTimeout(_lt); _lt = setTimeout(loadJournal, 150); };
  (function wait() {
    if (window.dbbasicSubscribe) {
      window.dbbasicSubscribe("fin_journals", reload);
      window.dbbasicSubscribe("fin_journal_lines", reload);
    } else setTimeout(wait, 300);
  })();
})();
"""


import re

_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def GET(request):
    journal_id = str(request.get("journal_id") or "").strip()
    if journal_id and not _RECORD_ID_RE.fullmatch(journal_id):
        journal_id = ""
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_journal_view served", journal_id=journal_id or "missing",
                 user_id=user_id or "anonymous")

    if not journal_id:
        return {
            "content_type": "text/html; charset=utf-8",
            "body": "<p>Journal not found. <a href='/journals'>Back to journals</a></p>",
            "status": 404,
        }

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else f'<a href="/login?next=/journals/{journal_id}">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Journal</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header class="app"><h1><a href="/journals">Journals</a> / journal</h1><div class="who">{who}</div></header>
<div class="card" id="card">
<h2 id="j-title">loading&hellip;</h2>
<div class="meta" id="j-meta"></div>
<div class="meta" id="j-ref"></div>
<div class="totals">
  <div class="row"><span>Total Debits</span><span id="t-debits"></span></div>
  <div class="row"><span>Total Credits</span><span id="t-credits"></span></div>
  <div class="row grand"><span>Balance</span><span id="t-balanced"></span></div>
</div>
<div class="owner-tools" id="owner-tools" style="display:none">
  <h4>Edit Journal</h4>
  <div id="editmount"></div>
</div>
</div>
<div id="lines-section">
  <h3>Lines</h3>
  <table class="lines-table">
    <thead><tr><th>Account</th><th>Memo</th><th class="num">Debit</th><th class="num">Credit</th></tr></thead>
    <tbody id="lines-body"><tr><td colspan="4" class="hint">loading&hellip;</td></tr></tbody>
  </table>
  <div class="owner-tools" id="owner-tools-lines" style="display:none">
    <h4>Add Line</h4>
    <div id="addlinemount"></div>
  </div>
</div>
</div>
<script>const JOURNAL_ID = {journal_id!r}; const VIEWER_ID = {(user_id or "")!r};</script>
<script src="/form"></script>
<script>{_SCRIPT}</script>
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
