"""The universal view renderer: one object that draws any `views` record.

Served through a site route like /views/{view_id:uuid}. The browser fetches
the view record with the visitor's own session cookie, so the permission
policy decides visibility (owner + is_public, same as every other record) --
the renderer adds no data path of its own. Each block then re-fetches its
own data the same way, so a public view over a private collection renders
its frame plus an empty/denied block for anonymous visitors: it can never
show what the engine would not otherwise serve.

Blocks are DATA, not code: a closed v1 vocabulary of eight kinds --
list, form, detail, related, thread, count, markdown, reader -- read from
the record's `blocks` JSON and rendered through the existing generators
(window.dbbasicList, window.dbbasicForm, window.dbbasicDetail,
window.dbbasicThread) or small renderers here. An unknown kind, a
malformed block, or invalid blocks JSON never becomes a blank page or raw
markup: it becomes a visible placeholder card, and the rest of the view
still renders.

`reader` ({kind:'reader', url}) is client-side like every other block
here: it POSTs the url to /api/read (object_server's flag-gated,
signed-in-only fetch -- see DBBASIC_ENABLE_READER and object_reader's SSRF
gate) and renders the stripped title/text/links it gets back. A disabled
flag, a signed-out visitor, or a refused/failed fetch all come back as a
normal JSON error body, which renders as the same placeholder card as any
other broken block -- no separate error path needed here.

`detail` (`59-detail-related-spec.md`) renders ONE record, read-only,
composed -- it does not re-derive field layout itself, it mounts
window.dbbasicDetail (served at /detail), which in turn reuses /form's
own field-rendering pipeline in forced-read-only mode
(window.dbbasicForm.readOnly). `related` renders a CHILD collection
filtered by a foreign key back to the record the page's `detail` block is
showing -- it is exactly a 58 filtered read (`{"kind":"list","filters":
{fk_field: match}}`), compiled here to a plain window.dbbasicList mount
with a `where` option (see list.py); no bespoke fetch. `thread` is sugar
over 22's comment widget (window.dbbasicThread) for the one shape
`related`'s single `fk_field` cannot express: `thread_comments`'
polymorphic `parent_collection`/`parent_id` pair. All three lean on
`$record_id`, a template token (mirroring `22`'s `$user_id` permission
row-filter convention) that resolves at render time to the id captured by
this view's own parameterized route -- see resolveRecordId and the
Python GET() below for how that capture is found.

**Scope boundary (per `plan/parity-completion-plan.md`'s document-
modeling rubric):** `related` is for TRUE CHILDREN with independent
identity and lifecycle -- interactions, comments, followers/following,
task<->files. It is NOT for document-composition items (order/invoice
lines): those embed as a JSON array field on the parent (a `line-items`
block, `66-line-items-spec.md`, not this one) and have no `collection` +
`fk_field` of their own for a `related` block to point at.

Two of the block options ask more of the generators than they currently
offer:
  - `list`'s `filters` and `limit` have no equivalent in window.dbbasicList
    (it always fetches its own /collections/{c}/records?limit=500 with no
    filter hook). Blocks that set either fall back to a small client-side
    fetch + filter + sort + slice here instead of pretending the option
    works -- see renderFilteredList. Filter-less, limit-less list blocks
    use the real window.dbbasicList and get its full feature set (search
    box, live edit/delete, row styling) for free. `related` never takes
    this path -- it always compiles to window.dbbasicList's `where` option
    (58's real, server-side filter), never the client-side fallback.
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
/* .detailcard/.detailrow/.detaillabel/.detailvalue: global now, in /style
   (app-theme/objects/site/style.py) -- window.dbbasicForm.readOnly (via
   window.dbbasicDetail) can be mounted outside this page too, so those
   rules moved out of this page-local stylesheet. */
.markdownblock { line-height: 1.6; }
.readertitle { margin: 0 0 0.5rem; font-size: 1.3rem; }
.readertext p { line-height: 1.6; margin: 0 0 0.85rem; }
.readerlinks { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius-md);
               padding: 0.75rem 1rem 0.75rem 2rem; margin-top: 0.75rem; font-size: 0.9rem; }
.readerlinks li { margin: 0.25rem 0; }
"""

_SCRIPT = r"""
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const human = (n) => String(n || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
const el = (id) => document.getElementById(id);

const KNOWN_KINDS = ["list", "form", "detail", "related", "thread", "count", "markdown", "reader"];

function unsupportedCard(msg) {
  return '<div class="viewblock-error">' + esc(msg || "unsupported block") + "</div>";
}

// 59's $record_id template token: a block field may hold the literal
// string "$record_id", resolved here to RECORD_ID -- the id GET() below
// captured from this view's own parameterized route (mirroring 22's
// $user_id permission row-filter convention, at render time instead of
// query time). Anything else (a literal id, e.g. an admin dashboard block
// pinned to one record) passes through unchanged. Returns null when the
// token can't resolve (no RECORD_ID for this render -- zero or multiple
// route captures, or the view was reached without going through its
// registered route) so callers can show the same visible error state
// "target collection missing" already uses, never a runtime crash on an
// empty filter/record id.
function resolveRecordId(value) {
  if (value !== "$record_id") return value || null;
  return RECORD_ID || null;
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
  const viewerId = (typeof VIEWER_ID !== "undefined" ? VIEWER_ID : "") || undefined;
  // `fixed`: lock fields to a context value for parent->child compose --
  // {"topic_id": "$record_id"} prefills+hides topic_id to the page's record.
  // Values run through the same $record_id resolution the detail/related
  // blocks use; the generic form renderer (opts.fixed) does the rest.
  const fixed = {};
  if (block.fixed && typeof block.fixed === "object") {
    for (const k in block.fixed) {
      const rv = resolveRecordId(block.fixed[k]);
      if (rv) fixed[k] = rv;
    }
  }
  const base = {mount: mount, owner: viewerId};
  if (Object.keys(fixed).length) base.fixed = fixed;
  // A compose form (no record_id) resets to empty after each save so another
  // child can be posted; a sibling `related` block re-renders on the record
  // event. An edit form (record_id) keeps the default post-save behavior.
  const go = (record) => window.dbbasicForm(block.collection, Object.assign({}, base, {
    record: record,
    onSaved: block.record_id ? undefined : function () { go(null); },
  }));
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

// 59's detail mode: renders ONE record, read-only, composed -- not a
// second field-renderer. This block is a thin mount wrapper; every
// field-layout/formatting decision lives in window.dbbasicDetail (served
// at /detail), which itself reuses /form's own pipeline
// (window.dbbasicForm.readOnly) in forced-read-only mode.
//
// Owner-aware edit/delete (Stage 6): a block may declare `editable` and/or
// `deletable` (and optionally `delete_redirect`, `owner_field`); the detail
// generator shows Edit/Delete only when the viewer owns the record. VIEWER_ID
// (embedded server-side above) is passed through so the generator can make
// that owner check -- it never widens the underlying permission gate, it only
// decides whether to render the affordances.
function renderDetail(block, mount) {
  if (!block.collection) { mount.innerHTML = unsupportedCard("detail block needs a collection"); return; }
  const recordId = resolveRecordId(block.record_id);
  if (!recordId) { mount.innerHTML = unsupportedCard("detail block needs a record_id"); return; }
  if (!window.dbbasicDetail) { mount.innerHTML = unsupportedCard("detail generator unavailable"); return; }
  const viewerId = (typeof VIEWER_ID !== "undefined" ? VIEWER_ID : "") || null;
  const load = () => window.dbbasicDetail.mount(mount, {
    collection: block.collection,
    record_id: recordId,
    editable: !!block.editable,
    deletable: !!block.deletable,
    delete_redirect: block.delete_redirect || null,
    owner_field: block.owner_field || null,
    viewer_id: viewerId,
  });
  (function sub() {
    if (window.dbbasicSubscribe) window.dbbasicSubscribe(block.collection, load);
    else setTimeout(sub, 400);
  })();
  load();
}

// 59's related block: a CHILD collection filtered by a foreign key back
// to the record the page's `detail` block is showing. This compiles
// directly to 58's filtered read -- window.dbbasicList's `where` option
// (list.py), never the client-side renderFilteredList fallback `list`
// itself sometimes needs -- so a related block is always the real,
// server-side, permission-narrowed filter, one query param:
// {fk_field: resolved match}.
function renderRelated(block, mount) {
  if (!block.collection || !block.fk_field) {
    mount.innerHTML = unsupportedCard("related block needs a collection and fk_field");
    return;
  }
  const match = resolveRecordId(block.match);
  if (!match) { mount.innerHTML = unsupportedCard("related block's match did not resolve"); return; }
  if (!window.dbbasicList) { mount.innerHTML = unsupportedCard("list generator unavailable"); return; }
  const heading = block.title ? '<h3 class="blocktitle">' + esc(block.title) + "</h3>" : "";
  mount.innerHTML = heading + '<div class="listmount"></div>';
  const listMount = mount.querySelector(".listmount");
  window.dbbasicList(block.collection, {mount: listMount, where: {[block.fk_field]: match}});
}

// 59's thread block: pure sugar over 22's comment widget for the one
// composition shape `related`'s single fk_field cannot express
// (thread_comments' polymorphic parent_collection/parent_id pair). 22
// still owns every behavior behind window.dbbasicThread (moderation,
// anon mode, realtime, markdown escaping) -- this only resolves
// $record_id and mounts it, the same "widget any page includes and
// mounts explicitly" shape 22's own Surfaces section documents.
function renderThread(block, mount) {
  if (!block.collection) { mount.innerHTML = unsupportedCard("thread block needs a collection"); return; }
  const recordId = resolveRecordId(block.record_id);
  if (!recordId) { mount.innerHTML = unsupportedCard("thread block needs a record_id"); return; }
  if (!window.dbbasicThread) { mount.innerHTML = unsupportedCard("thread widget unavailable"); return; }
  window.dbbasicThread.mount(mount, {parent_collection: block.collection, parent_id: recordId});
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

// Fetch one url through the flag-gated, signed-in-only /api/read and render
// title + paragraphs + a numbered link list. Every failure shape (flag off,
// signed out, SSRF-refused, non-HTML, timeout) comes back as {status:
// "error", error} from the endpoint and renders as the same placeholder
// card -- no separate error UI needed here.
function renderReader(block, mount) {
  if (!block.url) { mount.innerHTML = unsupportedCard("reader block needs a url"); return; }
  mount.innerHTML = '<div class="state">Fetching&hellip;</div>';
  fetch("/api/read", {
    method: "POST",
    credentials: "same-origin",
    headers: {"content-type": "application/json", accept: "application/json"},
    body: JSON.stringify({url: block.url}),
  })
    .then((res) => res.json().then((data) => ({ok: res.ok, data})))
    .then(({ok, data}) => {
      if (!ok || data.status === "error") {
        mount.innerHTML = unsupportedCard(data.error || "could not read page");
        return;
      }
      const paragraphs = String(data.text || "").split("\n\n")
        .filter((p) => p.trim())
        .map((p) => "<p>" + esc(p) + "</p>")
        .join("") || '<p class="state">No readable text.</p>';
      const links = (data.links || [])
        .map((l) => "<li>" + esc(l.n) + ". <a href=\"" + esc(l.href)
          + "\" target=\"_blank\" rel=\"noopener\">" + esc(l.label) + "</a></li>")
        .join("");
      mount.innerHTML = '<h1 class="readertitle">' + esc(data.title || block.url) + "</h1>"
        + '<div class="readertext">' + paragraphs + "</div>"
        + (links ? '<ol class="readerlinks">' + links + "</ol>" : "");
    })
    .catch(() => { mount.innerHTML = unsupportedCard("could not read page"); });
}

const RENDERERS = {list: renderList, form: renderForm, detail: renderDetail, related: renderRelated,
  thread: renderThread, count: renderCount, markdown: renderMarkdown, reader: renderReader};

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


import os
import re

import object_records
import object_site_routes

_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_DATA_DIR_ENV = "DBBASIC_DATA_DIR"

# Request keys that are never a route capture, whatever a package's route
# pattern happens to name its one param -- see _resolve_view_and_record.
_RESERVED_REQUEST_KEYS = {"_identity", "embed", "view_id"}


def _data_dir():
    # Mirrors app-catalog's stock.py: standalone, reads the env var
    # object_server.py sets for every execution rather than depending on
    # it directly.
    return os.environ.get(_DATA_DIR_ENV, object_records.DEFAULT_DATA_DIR)


def _views_records(base_dir):
    try:
        return object_records.read_collection_records("views", base_dir=base_dir)
    except (LookupError, OSError, ValueError):
        return []


def _route_capture_name(route):
    """Return the one {name} capture in a views.route pattern, or None.

    Reuses object_site_routes' own pattern parser -- docs/site-routing.md's
    {name}/{name:uuid} syntax, no second pattern language (59's Route-
    Seeding). None covers "no route", "no capture" (a plain path like
    /stuck), and "more than one capture" alike: 59 pins $record_id
    resolution to routes with EXACTLY one capture, so none of those three
    cases can ever supply a record id -- same degrade, no need to tell
    them apart here.
    """
    if not route:
        return None
    parsed = object_site_routes._parse_pattern(route)
    if not parsed:
        return None
    params = [name for kind, name, _constraint in parsed if kind == "param"]
    if len(params) != 1:
        return None
    return params[0]


def _resolve_view_and_record(request, base_dir):
    """Return (view_id, record_id) for this request.

    Direct case: request["view_id"] is set -- the plain
    /views/{view_id:uuid} convention (55) a site_routes row maps straight
    to this object. record_id still resolves the same way below (a view
    reached this way carries no captures of its own unless the caller
    also supplied one, e.g. hit directly with an extra query param -- rare
    but harmless, since every read past this point is permission-gated
    the same as any other route).

    Routed-detail case (59): no "view_id" key, but package-seeded routes
    like /contacts/{contact_id:uuid} land a DIFFERENT captured key
    (docs/site-routing.md's request[name] convention). Resolved by
    scanning the (small, one-row-per-collection) `views` collection for
    the record whose own `route` field declares a single capture matching
    one of the keys actually present -- the views record stays the single
    source of truth for which route maps to which view; no second route
    table, no new site_routes column.
    """
    view_id = str(request.get("view_id") or "").strip()
    candidates = {
        key: str(value).strip()
        for key, value in request.items()
        if key not in _RESERVED_REQUEST_KEYS
        and isinstance(value, str)
        and value.strip()
    }

    records = _views_records(base_dir)
    if view_id:
        record_id = ""
        for record in records:
            if record.get("id") == view_id:
                capture = _route_capture_name(record.get("route"))
                if capture and capture in candidates:
                    record_id = candidates[capture]
                break
        return view_id, record_id

    for record in records:
        capture = _route_capture_name(record.get("route"))
        if capture and capture in candidates:
            return str(record.get("id") or ""), candidates[capture]
    return "", ""


def GET(request):
    base_dir = _data_dir()
    view_id, record_id = _resolve_view_and_record(request, base_dir)
    if view_id and not _RECORD_ID_RE.fullmatch(view_id):
        view_id = ""
        record_id = ""
    if record_id and not _RECORD_ID_RE.fullmatch(record_id):
        record_id = ""
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_view_render served", view_id=view_id or "missing",
                 record_id=record_id or "none", user_id=user_id or "anonymous")

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
<script src="/detail"></script>
<script>const VIEW_ID = {view_id!r}; const VIEWER_ID = {(user_id or "")!r}; const RECORD_ID = {record_id!r};{_SCRIPT}</script>
{nav_html}
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
