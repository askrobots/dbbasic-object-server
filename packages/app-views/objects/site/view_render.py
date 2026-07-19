"""The universal view renderer: one object that draws any `views` record.

Served through a site route like /views/{view_id:uuid}. The browser fetches
the view record with the visitor's own session cookie, so the permission
policy decides visibility (owner + is_public, same as every other record) --
the renderer adds no data path of its own. Each block then re-fetches its
own data the same way, so a public view over a private collection renders
its frame plus an empty/denied block for anonymous visitors: it can never
show what the engine would not otherwise serve.

Blocks are DATA, not code: a closed v1 vocabulary of five kinds --
list, form, detail, count, markdown -- read from the record's `blocks`
JSON and rendered through the existing generators (window.dbbasicList,
window.dbbasicForm) or small renderers here. An unknown kind, a malformed
block, or invalid blocks JSON never becomes a blank page or raw markup: it
becomes a visible placeholder card, and the rest of the view still renders.

Two of the block options ask more of the generators than they currently
offer:
  - `list`'s `filters` and `limit` have no equivalent in window.dbbasicList
    (it always fetches its own /collections/{c}/records?limit=500 with no
    filter hook). Blocks that set either fall back to a small client-side
    fetch + filter + sort + slice here instead of pretending the option
    works -- see renderFilteredList. Filter-less, limit-less list blocks
    use the real window.dbbasicList and get its full feature set (search
    box, live edit/delete, row styling) for free.
  - `form`'s optional `form` key (an alternate form name) has no
    equivalent either -- window.dbbasicForm always renders
    schema.forms.default. Only `record_id` is honored.

The page subscribes to its own view record (dbbasicSubscribe("views", ...))
and reloads on change, so editing a view's blocks re-renders it live --
the same "surface changed, re-render" signal every other live list gets,
with zero new protocol.
"""

# Page-unique layout only; shared component styling comes from /style.
_STYLE = """
.blocks.layout-single { display: grid; gap: var(--gap); }
.blocks.layout-two_column { display: grid; gap: var(--gap); grid-template-columns: 1fr 1fr; }
.blocks.layout-grid { display: grid; gap: var(--gap); grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); }
@media (max-width: 720px) { .blocks.layout-two_column { grid-template-columns: 1fr; } }
.viewblock { min-width: 0; }
.viewblock-error { background: var(--panel); border: 1px solid var(--danger); color: var(--danger);
                    border-radius: var(--radius-md); padding: 0.75rem 1rem; font-size: 0.85rem; }
.blocktitle { margin: 0 0 0.5rem; font-size: 1rem; }
.countcard { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius-md);
             padding: 1rem; text-align: center; }
.countcard.danger { border-color: var(--danger); color: var(--danger); }
.countnum { font-size: 2.25rem; font-weight: 700; line-height: 1; }
.countlabel { color: var(--muted); font-size: 0.85rem; margin-top: 0.25rem; }
.detailcard { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius-md);
              padding: 0.25rem 1rem; }
.detailrow { display: grid; grid-template-columns: 10rem 1fr; gap: 0.5rem; padding: 0.5rem 0;
             border-bottom: 1px solid var(--line); }
.detailrow:last-child { border-bottom: 0; }
.detaillabel { color: var(--muted); font-size: 0.82rem; }
.detailvalue { word-break: break-word; }
.markdownblock { line-height: 1.6; }
"""

_SCRIPT = r"""
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const human = (n) => String(n || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
const el = (id) => document.getElementById(id);

const KNOWN_KINDS = ["list", "form", "detail", "count", "markdown"];

function unsupportedCard(msg) {
  return '<div class="viewblock-error">' + esc(msg || "unsupported block") + "</div>";
}

function matchesFilters(record, filters) {
  if (!filters) return true;
  return Object.entries(filters).every(([k, v]) => String(record[k] ?? "") === String(v));
}

// Fallback for list blocks that need filters/limit -- options window.dbbasicList
// does not expose. Fetches the collection itself, filters/sorts/slices
// client-side, and renders plain rows styled with the shared .listrow classes.
function renderFilteredList(block, mount) {
  async function load() {
    const res = await fetch("/collections/" + encodeURIComponent(block.collection) + "/records?limit=500",
      {credentials: "same-origin", headers: {accept: "application/json"}});
    if (!res.ok) { mount.innerHTML = unsupportedCard("could not load " + block.collection); return; }
    const body = await res.json();
    let list = (body.records || []).filter((r) => matchesFilters(r, block.filters));
    list = list.slice().sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")));
    if (!(block.sort === "oldest" || block.sort === "asc")) list.reverse();
    if (block.limit != null) list = list.slice(0, Number(block.limit) || 0);
    mount.innerHTML = list.length
      ? list.map((r) => '<div class="listrow"><div class="body"><div class="rowtitle">'
          + esc(r.title || r.name || r.id) + "</div></div></div>").join("")
      : '<div class="state">Nothing yet.</div>';
  }
  (function sub() {
    if (window.dbbasicSubscribe) window.dbbasicSubscribe(block.collection, load);
    else setTimeout(sub, 400);
  })();
  load();
}

function renderList(block, mount) {
  if (!block.collection) { mount.innerHTML = unsupportedCard("list block needs a collection"); return; }
  const heading = block.title ? '<h3 class="blocktitle">' + esc(block.title) + "</h3>" : "";
  mount.innerHTML = heading + '<div class="listmount"></div>';
  const listMount = mount.querySelector(".listmount");
  const hasFilters = block.filters && Object.keys(block.filters).length;
  const hasLimit = block.limit != null;
  if (hasFilters || hasLimit || !window.dbbasicList) {
    renderFilteredList(block, listMount);
    return;
  }
  const cfg = {mount: listMount};
  if (block.sort === "oldest" || block.sort === "asc") {
    // dbbasicList's real sort option is a bound <select> element, not a
    // value -- synthesize a hidden one already set to "oldest" so the
    // real option still applies instead of inventing a new one.
    const sel = document.createElement("select");
    sel.style.display = "none";
    sel.innerHTML = '<option value="oldest" selected>oldest</option>';
    mount.appendChild(sel);
    cfg.sort = sel;
  }
  window.dbbasicList(block.collection, cfg);
}

function renderForm(block, mount) {
  if (!block.collection) { mount.innerHTML = unsupportedCard("form block needs a collection"); return; }
  if (!window.dbbasicForm) { mount.innerHTML = unsupportedCard("form generator unavailable"); return; }
  const go = (record) => window.dbbasicForm(block.collection, {mount, record});
  if (block.record_id) {
    fetch("/collections/" + encodeURIComponent(block.collection) + "/records/" + encodeURIComponent(block.record_id),
      {credentials: "same-origin", headers: {accept: "application/json"}})
      .then((res) => (res.ok ? res.json() : null))
      .then((body) => go(body && (body.record || body)))
      .catch(() => go(null));
  } else {
    go(null);
  }
}

async function renderDetail(block, mount) {
  if (!block.collection || !block.record_id) { mount.innerHTML = unsupportedCard("detail block needs a collection and record_id"); return; }
  const [schemaRes, recordRes] = await Promise.all([
    fetch("/api/schema/" + encodeURIComponent(block.collection), {credentials: "same-origin", headers: {accept: "application/json"}}),
    fetch("/collections/" + encodeURIComponent(block.collection) + "/records/" + encodeURIComponent(block.record_id),
      {credentials: "same-origin", headers: {accept: "application/json"}}),
  ]);
  if (!schemaRes.ok || !recordRes.ok) { mount.innerHTML = unsupportedCard("could not load record"); return; }
  const schemaBody = await schemaRes.json();
  const recordBody = await recordRes.json();
  const schema = schemaBody.schema;
  const record = recordBody.record || recordBody;
  if (!schema || !record) { mount.innerHTML = unsupportedCard("could not load record"); return; }
  const rows = (schema.fields || [])
    .filter((f) => !f.hidden && !f.internal)
    .map((f) => '<div class="detailrow"><div class="detaillabel">' + esc(f.label || human(f.name))
      + '</div><div class="detailvalue">' + esc(record[f.name]) + "</div></div>")
    .join("");
  mount.innerHTML = '<div class="detailcard">' + (rows || '<div class="state">No fields.</div>') + "</div>";
}

function renderCount(block, mount) {
  if (!block.collection) { mount.innerHTML = unsupportedCard("count block needs a collection"); return; }
  async function load() {
    const res = await fetch("/collections/" + encodeURIComponent(block.collection) + "/records?limit=500",
      {credentials: "same-origin", headers: {accept: "application/json"}});
    if (!res.ok) { mount.innerHTML = unsupportedCard("could not load " + block.collection); return; }
    const body = await res.json();
    const n = (body.records || []).filter((r) => matchesFilters(r, block.filters)).length;
    const warn = block.warn_over != null && n > Number(block.warn_over);
    mount.innerHTML = '<div class="countcard' + (warn ? " danger" : "") + '"><div class="countnum">' + n
      + '</div><div class="countlabel">' + esc(block.label || block.collection) + "</div></div>";
  }
  (function sub() {
    if (window.dbbasicSubscribe) window.dbbasicSubscribe(block.collection, load);
    else setTimeout(sub, 400);
  })();
  load();
}

// Safe subset: escape ALL html first, then apply bold/italic/links/line
// breaks on the escaped text. Never innerHTML the raw block text.
function renderMarkdown(block, mount) {
  let html = esc(block.text);
  html = html.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
  html = html.replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
  html = html.replace(/\n/g, "<br>");
  mount.innerHTML = '<div class="markdownblock">' + html + "</div>";
}

const RENDERERS = {list: renderList, form: renderForm, detail: renderDetail, count: renderCount, markdown: renderMarkdown};

function renderBlocks(view) {
  const container = el("blocks");
  container.className = "blocks layout-" + esc(view.layout || "single");
  let blocks;
  try {
    blocks = JSON.parse(view.blocks || "[]");
    if (!Array.isArray(blocks)) throw new Error("blocks must be a JSON array");
  } catch (e) {
    container.innerHTML = unsupportedCard("Invalid blocks JSON");
    return;
  }
  if (!blocks.length) { container.innerHTML = '<div class="state">This view has no blocks yet.</div>'; return; }
  container.innerHTML = "";
  blocks.forEach((block) => {
    const wrap = document.createElement("div");
    wrap.className = "viewblock";
    container.appendChild(wrap);
    if (!block || typeof block !== "object" || KNOWN_KINDS.indexOf(block.kind) === -1) {
      wrap.innerHTML = unsupportedCard("Unsupported block kind" + (block && block.kind ? ": " + esc(block.kind) : ""));
      return;
    }
    try {
      RENDERERS[block.kind](block, wrap);
    } catch (e) {
      wrap.innerHTML = unsupportedCard("Invalid block");
    }
  });
}

async function load() {
  const res = await fetch("/collections/views/records/" + VIEW_ID,
    {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) {
    el("blocks").innerHTML = VIEWER_ID
      ? '<p class="hint">This view does not exist or is not shared with you.</p>'
      : '<p class="hint"><a href="/login?next=/views/' + VIEW_ID + '">Sign in</a> to view this page.</p>';
    return;
  }
  const body = await res.json();
  const view = body.record || body;
  document.title = view.title || "View";
  el("viewtitle").textContent = view.title || "View";
  renderBlocks(view);
}
load();

// Realtime: re-render when this view record changes (edited in another
// tab, by another user, or by an agent) -- the "surface changed" signal
// falls out of existing realtime with zero new protocol.
(function () {
  let _lt = null;
  const reload = () => { clearTimeout(_lt); _lt = setTimeout(load, 150); };
  (function wait() {
    if (window.dbbasicSubscribe) window.dbbasicSubscribe("views", reload);
    else setTimeout(wait, 300);
  })();
})();
"""


import re

_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def GET(request):
    view_id = str(request.get("view_id") or "").strip()
    if view_id and not _RECORD_ID_RE.fullmatch(view_id):
        view_id = ""
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_view_render served", view_id=view_id or "missing",
                 user_id=user_id or "anonymous")

    if not view_id:
        return {
            "content_type": "text/html; charset=utf-8",
            "body": "<p>View not found.</p>",
            "status": 404,
        }

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else f'<a href="/login?next=/views/{view_id}">sign in</a>'
    )
    # ?embed=1: chromeless rendition for stages/iframes (Talk, shell embeds,
    # someday in-world screens) -- same page, no app bar, no who-row.
    embed = str(request.get("embed") or "").strip() not in ("", "0", "false")
    header_html = (
        f'<header class="app"><h1 id="viewtitle">Loading&hellip;</h1>'
        f'<div class="who">{who}</div></header>'
        if not embed
        else '<h1 id="viewtitle" class="embed-title">Loading&hellip;</h1>'
    )
    nav_html = "" if embed else '<script src="/nav"></script>'
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>View</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
{header_html}
<div class="blocks" id="blocks"><div class="state">loading&hellip;</div></div>
</div>
<script src="/list"></script>
<script src="/form"></script>
<script>const VIEW_ID = {view_id!r}; const VIEWER_ID = {(user_id or "")!r};{_SCRIPT}</script>
{nav_html}
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
