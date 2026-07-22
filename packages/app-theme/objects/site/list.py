"""Shared list generator, served at /list as window.dbbasicList.

Renders a collection as rich rows (avatar, title/link, subtitle, relative
date, tag pills, per-row edit + delete), with a search box (over
/api/search) and a newest/oldest sort. It subscribes to the collection
over the /ws websocket, so the list auto-refreshes when a record changes
in another tab, by another user, or by an agent — a thing the old stack's
lists did not do. Display accessors come from a small config; search and
live updates are automatic.

`cfg.where` (optional, `{field: value}`) narrows the fetch to
`58-query-filter-spec.md`'s `field=value` (implicit `eq`) query encoding —
the same server-side filter `/collections/{c}/records` already applies
after its own permission row filter, so a `where`'d list can only narrow
what the caller could already see, never widen it. This is what
`59-detail-related-spec.md`'s `related` block compiles to: one `where`
entry, `{fk_field: parent_record_id}`, no bespoke fetch. Plain lists
(no `where`) are unaffected.

`60-list-modes-spec.md`: board / tree / calendar.

`window.dbbasicList(collection, cfg)` is schema-driven for THREE more
`views.list_mode` values beyond the plain row list above (`table`/`cards`/
`feed`, which all render as the row list -- this generator has never
distinguished them). On mount it fetches `/api/schema/{collection}` (the
same public, structure-only endpoint window.dbbasicForm already reads) and,
when `schema.views.list_mode` is `board`/`tree`/`calendar`, delegates to
one of the render functions below instead of the row list -- no new route,
no per-page code, the same "1 -> 12" property every generator improvement
already has. A collection with `list_mode` left at `table`/`cards`/`feed`/
unset never pays for this beyond the one extra schema fetch.

- **board** (kanban AND the CRM lead pipeline -- ONE mode, not two: a
  collection gets `views.list_mode: "board"` with a grouping field, never
  a separate "kanban block"). Buckets one flat 58 fetch by an enum field
  into columns (`groupByColumn`), in the enum's own declared order, plus
  an "(unset)" column first for an empty/stray value -- never a dropped
  card. `group_field` defaults per `defaultGroupField`'s chain (schema
  `flow.field`, else the first `transitions`-guarded enum, else the first
  plain enum) so `tasks` and a bare `contacts.lead_status` both get a
  working board from the same fallback with no divergent config. A drag
  issues `PUT /collections/{c}/records/{id}` with the target column as the
  new field value -- THE SAME write any row-action or generated-form edit
  already issues. `object_records.update_collection_record` runs
  `_validate_field_transitions` on every such write, guard map or not; this
  file adds no client-side legality check and no second write path. If the
  group field carries no `transitions` map (`contacts.lead_status` today --
  no map declared, see `packages/app-contacts/schemas/contacts.json`), the
  drag is an ORDINARY unguarded write, exactly as permissive as editing
  that field through the generated form already is -- not a regression, not
  a silently-invented guard. Adding a `transitions` (and optionally `flow`)
  map to `contacts.json` to make the pipeline board a *guarded* workflow is
  Stage-6 schema wiring, not done here (open question, carried forward
  below). A rejected drop (`res.ok` false -- illegal `to`, or a failed
  `when` guard) reverts the card to its origin column by simply re-drawing
  from the last successfully loaded set; the record was never mutated
  client-side before the server confirmed it.
  **Open question carried forward verbatim from `31-wizard-kanban-stub.md`
  via `60-list-modes-spec.md` (unresolved here too):** when one user drags
  a card to another column while a second user's client is mid-scroll or
  has independently reordered the same column's presentational order, does
  the card resolve to its DOM position or its semantic (column) position
  once both realtime updates land? v1 has no persisted within-column rank
  (a reload always re-sorts by the collection's default sort), so there is
  no "position" to preserve beyond column membership -- semantic/column
  position wins by construction -- but this is stated, not proven under two
  concurrent clients; needs a concrete look once this ships.
- **tree** (account hierarchy, location hierarchy -- any self-relation).
  Nests one flat 58 fetch by a `parent_field` self-relation
  (`buildTree`), read-only in v1 (re-parenting stays a plain form edit).
  **Cycle guard**: neither `fin_accounts.parent_id` nor `locations.
  parent_id` has platform-level acyclicity enforcement (both schemas say so
  in their own text) -- `buildTree` tracks a `visited` id set across the
  whole render pass; a node already rendered is never re-descended into, so
  a malformed loop renders once and stops instead of hanging the page. Logs
  once (`console.warn`) when a render pass hits this, so an operator can
  find the bad `parent_id`. **Depth cap** (`max_depth`, default 10) is a
  second, independent guard alongside the cycle guard, not a restatement of
  it: it bounds a legitimately deep (but acyclic) tree's render cost. A
  node at the cap gets a `truncated: true` flag on its data (spec's own
  open question -- "silent stop" vs. an affordance -- is left unresolved;
  the flag exists so a future UI decision doesn't need a second data pass).
  One implementation note NOT in the spec's own cost model: this renderer
  does a single flat fetch (`/collections/{c}/records?limit=500`, exactly
  the row list's own shape, plus `cfg.where`) and nests client-side, rather
  than one 58 filter call per expanded level. Reason: this platform's query
  parser drops a blank query value (`?parent_id=` for "root" -- Python's
  `urllib.parse.parse_qsl` without `keep_blank_values=True`,
  `object_server._parse_query`), so the spec's literal `{parent_field: ""}`
  root filter can't be expressed as a query string today. One bounded fetch
  (same `limit=500` ceiling every other list already accepts) sidesteps
  that gap entirely and is still exactly a 58 filtered fetch, bucketed
  client-side -- the same pattern board's own Storage section already
  uses, just bucketing by `parent_field` instead of one enum.
- **calendar** (month grid over a date/datetime field). One 58 range fetch
  per visible month (`date_field.gte`/`date_field.lte`, 58's own operators,
  nothing new), bucketed client-side by day (`bucketByDate`). A record with
  no `date_field` value is never dropped: it renders once in an "Undated"
  row above the grid. Read-only in v1 (dragging a card to reschedule is
  future work); prev/next re-issues the range fetch, nothing prefetched.

All three inherit 58's Permissions Posture with zero new access machinery:
every card/node/cell comes from the exact `/collections/{c}/records`
fetch the row list already issues (permission row-filter first, field
filter second, same as any other read through this generator) -- a viewer
who can't read a record never sees its card/node/cell, and a `group_field`/
`parent_field`/`date_field` this file picks is always a real schema field
(the same structure-only `/api/schema/{c}` endpoint window.dbbasicForm
already reads), never a hidden one singled out.

`list_modes_enabled` (default ON -- an absent or non-"off"/"false" value
counts as on, since nothing in this codebase pre-seeds a `feature_flags`
row) gates all three; off, or a mode whose field can't be derived (no enum
for board, no self-relation for tree, no writable date for calendar, and no
explicit `views.board`/`views.tree`/`views.calendar` override), falls back
to the plain row list with a visible, correctable notice -- never a silent
empty page, matching every other block's degrade-to-safe posture.

Defined once here — every list page reuses it instead of hand-writing rows.
"""

_JS = r"""
(function () {
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const qs = (m) => typeof m === "string" ? document.querySelector(m) : m;
  const humanize = (n) => String(n || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

  function relDate(iso) {
    if (!iso) return "";
    const d = new Date(iso); if (isNaN(d)) return "";
    const ms = Date.now() - d.getTime();
    if (ms < 3600000) { const m = Math.floor(ms / 60000); return m < 1 ? "just now" : m + "m"; }
    if (ms < 86400000) return Math.floor(ms / 3600000) + "h";
    if (ms < 7 * 86400000) return Math.floor(ms / 86400000) + "d";
    return d.toLocaleDateString(undefined, {month: "short", day: "numeric"});
  }
  function pills(v) {
    return String(v || "").split(",").map((t) => t.trim()).filter(Boolean)
      .map((t) => '<span class="pill">' + esc(t) + '</span>').join("");
  }
  // A single enum/status value renders as a colored badge -- the same
  // treatment the bespoke Projects table gives "active". Tone is inferred
  // from the value's meaning so the whole product agrees (a "done"/"paid"
  // is green everywhere, a "blocked"/"overdue" red) without each schema
  // having to declare colors. Unknown values get a neutral badge.
  function badgeTone(val) {
    const s = String(val || "").toLowerCase().replace(/[\s-]+/g, "_");
    if (/^(active|done|complete|completed|paid|approved|published|resolved|won|live|success|ready|enabled|open)$/.test(s)) return "positive";
    if (/^(blocked|overdue|failed|cancelled|canceled|rejected|lost|error|archived|expired|disabled|closed)$/.test(s)) return "danger";
    if (/^(pending|in_progress|review|in_review|on_hold|draft|todo|new|waiting|queued|processing|backlog)$/.test(s)) return "warning";
    return "";
  }
  function enumBadge(val) {
    const tone = badgeTone(val);
    return '<span class="badge' + (tone ? " " + tone : "") + '">' + esc(String(val)) + '</span>';
  }

  // 58's field=value encoding (implicit eq) -- one query param per `where`
  // entry, ANDed by the server after its own permission row filter. `extra`
  // layers on any additional dotted-operator params (calendar's own
  // `date_field.gte`/`date_field.lte` range) with the same encoding. Shared
  // by the plain row list and all three 60 modes below -- every mode's
  // fetch is this SAME query builder, just with the mode's own grouping
  // dimension bucketed client-side on top (see each render function).
  function whereQueryString(where, extra) {
    const parts = [];
    if (where) for (const [k, v] of Object.entries(where)) parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
    if (extra) for (const [k, v] of Object.entries(extra)) parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
    return parts.length ? "&" + parts.join("&") : "";
  }

  function isEnumField(field) {
    return !!field && (field.type === "enum" || Array.isArray(field.enum));
  }

  function isDateField(field) {
    return !!field && (field.type === "date" || field.type === "datetime");
  }

  function fieldsByName(schema) {
    const byName = {};
    (schema.fields || []).forEach((f) => { byName[f.name] = f; });
    return byName;
  }

  // ---- board: group by an enum field, columns in enum order --------------

  // 60's default chain: the schema's `flow.field` (10-flow-spec.md's
  // annotation of an existing transitions map -- inert today, no shipped
  // schema declares `flow` yet, but honored the moment one does) else the
  // first transitions-guarded enum (tasks.status) else the first plain enum
  // (a bare lead_status-shaped field) else none (Degradation: falls back
  // to table).
  function defaultGroupField(schema) {
    const fields = schema.fields || [];
    const byName = fieldsByName(schema);
    if (schema.flow && schema.flow.field && byName[schema.flow.field]) return schema.flow.field;
    const guarded = fields.find((f) => isEnumField(f) && f.transitions && Object.keys(f.transitions).length > 0);
    if (guarded) return guarded.name;
    const anyEnum = fields.find(isEnumField);
    return anyEnum ? anyEnum.name : null;
  }

  // "(unset)" column first (mirrors flow's own "an empty existing value may
  // move anywhere" rule), then the enum's own declared order -- nothing
  // re-sorted, re-derived, or alphabetized.
  function boardColumns(field) {
    return [""].concat((field && field.enum) || []);
  }

  // Buckets a flat record list by `groupField`'s value into `columns`. A
  // value that isn't one of the field's declared enum options (stray data,
  // not a spec'd case) folds into the "(unset)" bucket rather than either
  // vanishing or inventing a new column on the fly -- never a dropped card.
  function groupByColumn(records, groupField, columns) {
    const known = new Set(columns);
    const buckets = {};
    columns.forEach((c) => { buckets[c] = []; });
    for (const r of records) {
      const v = known.has(String(r[groupField] || "")) ? String(r[groupField] || "") : "";
      buckets[v].push(r);
    }
    return buckets;
  }

  function resolveBoardConfig(schema) {
    const block = (schema.views && schema.views.board) || {};
    const groupField = block.group_field || defaultGroupField(schema);
    const field = groupField ? fieldsByName(schema)[groupField] : null;
    if (!isEnumField(field)) {
      return {error: "board mode needs an enum field -- none found; showing table"};
    }
    const cardFields = block.card_fields || (schema.views && schema.views.list_fields) || [];
    return {config: {groupField: groupField, columns: boardColumns(field),
                     cardFields: cardFields, byName: fieldsByName(schema)}};
  }

  // ---- tree: nest by a self-relation, cycle-guarded + depth-capped -------

  function isSelfRelation(schema, field) {
    if (!field || !field.relation) return false;
    const target = typeof field.relation === "string" ? field.relation : field.relation.collection;
    return target === schema.name;
  }

  function resolveTreeConfig(schema) {
    const block = (schema.views && schema.views.tree) || {};
    const parentField = block.parent_field || "parent_id";
    const field = fieldsByName(schema)[parentField];
    if (!isSelfRelation(schema, field)) {
      return {error: "tree mode needs a self-relation field ('" + parentField + "' is not one); showing table"};
    }
    const maxDepth = block.max_depth == null ? 10 : Number(block.max_depth);
    return {config: {parentField: parentField, maxDepth: maxDepth}};
  }

  // Pure nest-and-guard over an already-fetched flat record list -- no DOM,
  // no fetch, so this is what the test suite runs directly under node.
  // `visited` is one Set for the whole render pass: a record already
  // rendered once (reached from a first parent) is skipped, never
  // re-descended, when some OTHER row's `parentField` also points at it --
  // the guard 60's Storage section calls for against a malformed/cyclic
  // `parent_id` chain. `maxDepth` is the independent second guard: a node
  // at the cap still renders (never silently missing) but does not descend
  // further, and carries `truncated: true` when it actually has children
  // being cut off, so a future UI affordance has something to key off.
  function buildTree(records, opts) {
    opts = opts || {};
    const parentField = opts.parentField || "parent_id";
    const maxDepth = opts.maxDepth == null ? 10 : opts.maxDepth;
    const startParentId = opts.startParentId || "";

    const byParent = new Map();
    for (const r of records) {
      const p = String(r[parentField] || "");
      if (!byParent.has(p)) byParent.set(p, []);
      byParent.get(p).push(r);
    }

    const visited = new Set();
    let cycleDetected = false;

    function descend(parentId, depth) {
      const kids = byParent.get(parentId) || [];
      const out = [];
      for (const child of kids) {
        if (visited.has(child.id)) { cycleDetected = true; continue; }
        visited.add(child.id);
        const grandkids = byParent.get(child.id) || [];
        const canDescend = depth + 1 < maxDepth;
        out.push({
          record: child,
          depth: depth,
          truncated: !canDescend && grandkids.length > 0,
          children: canDescend ? descend(child.id, depth + 1) : [],
        });
      }
      return out;
    }

    return {nodes: descend(startParentId, 0), cycleDetected: cycleDetected};
  }

  // ---- calendar: month grid over a date/datetime field --------------------

  function defaultDateField(schema) {
    const f = (schema.fields || []).find((f) => isDateField(f) && !f.read_only);
    return f ? f.name : null;
  }

  function resolveCalendarConfig(schema) {
    const block = (schema.views && schema.views.calendar) || {};
    const dateField = block.date_field || defaultDateField(schema);
    const field = dateField ? fieldsByName(schema)[dateField] : null;
    if (!isDateField(field)) {
      return {error: "calendar mode needs a date field -- none found; showing table"};
    }
    return {config: {dateField: dateField, defaultView: block.default_view || "month"}};
  }

  function monthRange(year, month) {
    const start = new Date(Date.UTC(year, month, 1));
    const end = new Date(Date.UTC(year, month + 1, 0));
    return {start: start.toISOString().slice(0, 10), end: end.toISOString().slice(0, 10)};
  }

  // 42 cells (6 weeks); leading/trailing days from adjacent months are
  // dimmed (inMonth: false) and never fetched -- purely a grid-shape filler.
  function monthGridDays(year, month) {
    const first = new Date(Date.UTC(year, month, 1));
    const startOffset = first.getUTCDay();
    const cells = [];
    for (let i = 0; i < 42; i++) {
      const d = new Date(Date.UTC(year, month, 1 - startOffset + i));
      cells.push({date: d.toISOString().slice(0, 10), inMonth: d.getUTCMonth() === month});
    }
    return cells;
  }

  // A record with no `dateField` value lands in `undated`, never dropped --
  // "visible gap, not silent loss", the same posture 58 commits to for an
  // unfiltered fallback.
  function bucketByDate(records, dateField) {
    const byDay = {};
    const undated = [];
    for (const r of records) {
      const v = r[dateField];
      if (!v) { undated.push(r); continue; }
      const day = String(v).slice(0, 10);
      if (!byDay[day]) byDay[day] = [];
      byDay[day].push(r);
    }
    return {byDay: byDay, undated: undated};
  }

  // ---- feature flag + schema-driven mode resolution ------------------------

  // Default ON: nothing pre-seeds a `feature_flags` row for this flag, so
  // "unknown" must read as "on" (an absent value would otherwise read as
  // off through window.dbbasicFlags.on, which is default-false) -- only an
  // explicit "off"/"false" value turns the three modes off.
  async function listModesEnabled() {
    if (!window.dbbasicFlags) return true;
    try {
      await window.dbbasicFlags.load();
      const v = window.dbbasicFlags("list_modes_enabled");
      return v !== "off" && v !== "false";
    } catch (e) {
      return true;
    }
  }

  // Table config from a schema: columns from list_fields (curated) else every
  // field, never the raw id. Returns null when there's nothing to show as
  // columns. Hoisted out of resolveListMode so the board<->table switcher can
  // build a table for a board-declared collection too (it has list_fields).
  function buildTableConfig(schema) {
    if (!schema) return null;
    const byName = {}; (schema.fields || []).forEach((f) => byName[f.name] = f);
    const fields = ((schema.views && schema.views.list_fields) || (schema.fields || []).map((f) => f.name))
      .filter((n) => byName[n] && n !== "id");
    return fields.length ? {fields: fields, byName: byName} : null;
  }

  // Filter controls from schema.views.filter_fields. Each named field becomes
  // a control: an enum -> a select of its options, a boolean -> Yes/No. The
  // chosen values are ANDed into the same server-side `where` fetch the board
  // already uses (field=value, applied after the permission row filter), so a
  // filtered list can only ever narrow what the viewer may already see. Types
  // other than enum/boolean are skipped for now (text is covered by the search
  // box; a date-range control is a follow-on that needs the dotted-operator
  // `extra` query path). One declaration, filters on every mode.
  function buildFilters(schema) {
    const ff = schema && schema.views && schema.views.filter_fields;
    if (!Array.isArray(ff) || !ff.length) return [];
    const byName = {}; (schema.fields || []).forEach((f) => byName[f.name] = f);
    const out = [];
    for (const name of ff) {
      const f = byName[name]; if (!f) continue;
      const t = String(f.type || "").toLowerCase();
      if (t === "enum" || Array.isArray(f.enum)) {
        out.push({name: name, label: f.label || humanize(name), type: "enum", options: f.enum || []});
      } else if (t === "boolean") {
        out.push({name: name, label: f.label || humanize(name), type: "boolean"});
      }
    }
    return out;
  }

  // Resolve relation fields to a human label instead of a raw FK id. A field
  // with `relation: {collection, display_field}` stores an id (o-acme); the
  // table/board should show the target's name ("Acme"), the way the detail
  // page already does. Fetch each referenced collection once, build an
  // id->display map. Done once per render surface (not per realtime reload) --
  // relation targets are comparatively stable, and a stale label just corrects
  // on the next full load. Fails soft: a field with no map falls back to the id.
  async function loadRelationMaps(byName, fields) {
    const maps = {};
    await Promise.all((fields || []).map(async (n) => {
      const f = byName && byName[n];
      if (!f || !f.relation) return;
      const col = typeof f.relation === "string" ? f.relation : f.relation.collection;
      const disp = (typeof f.relation === "object" && f.relation.display_field) || "name";
      try {
        const res = await fetch("/collections/" + encodeURIComponent(col) + "/records?limit=500",
          {credentials: "same-origin", headers: {accept: "application/json"}});
        if (!res.ok) return;
        const body = await res.json();
        const m = {};
        for (const r of (body.records || [])) m[r.id] = r[disp] || r.id;
        maps[n] = m;
      } catch (e) { /* leave unmapped -> falls back to the id */ }
    }));
    return maps;
  }
  function relLabel(fname, value, byName, relMaps) {
    const f = byName && byName[fname];
    if (f && f.relation && relMaps && relMaps[fname] && (value in relMaps[fname])) return relMaps[fname][value];
    return value;
  }

  async function resolveListMode(collection) {
    if (!(await listModesEnabled())) return {kind: null, notice: null, schema: null};

    let schema = null;
    try {
      const res = await fetch("/api/schema/" + encodeURIComponent(collection),
        {credentials: "same-origin", headers: {accept: "application/json"}});
      if (res.ok) { const body = await res.json(); schema = body.schema || null; }
    } catch (e) { schema = null; }
    if (!schema) return {kind: null, notice: null, schema: null};

    const wanted = schema.views && schema.views.list_mode;
    if (wanted === "board") {
      const r = resolveBoardConfig(schema);
      return r.config ? {kind: "board", config: r.config, schema: schema} : {kind: null, notice: r.error, schema: schema};
    }
    if (wanted === "tree") {
      const r = resolveTreeConfig(schema);
      return r.config ? {kind: "tree", config: r.config, schema: schema} : {kind: null, notice: r.error, schema: schema};
    }
    if (wanted === "calendar") {
      const r = resolveCalendarConfig(schema);
      return r.config ? {kind: "calendar", config: r.config, schema: schema} : {kind: null, notice: r.error, schema: schema};
    }
    if (wanted === "table") {
      const cfg = buildTableConfig(schema);
      return cfg ? {kind: "table", config: cfg, schema: schema} : {kind: null, notice: null, schema: schema};
    }
    return {kind: null, notice: null, schema: schema};
  }

  // ---- board/tree/calendar renderers (impure: fetch + DOM + realtime) ----

  function cardTitle(cfg, r) {
    // Label a record by a human field, trying common text fields before the raw
    // id -- so a comment / note / message / interaction (no title/name field)
    // shows its actual text instead of a UUID (and the avatar gets a real
    // letter). cfg.title overrides everything.
    const auto = r.title || r.name || r.label || r.subject || r.body || r.text
      || r.content || r.message || r.description || r.email || r.id;
    return (cfg.title ? cfg.title(r) : auto) || "(untitled)";
  }

  function renderBoard(collection, cfg, mount, boardCfg) {
    let all = [];
    let relMaps = {};

    function cardHtml(r) {
      const title = cardTitle(cfg, r);
      // Skip the group field, empties, and any field whose value is already
      // part of the title (first_name/last_name when the title is the full
      // name) -- otherwise the card repeats "Grace" / "Hopper" under "Grace
      // Hopper". Relation fields show the resolved label, not the raw FK id.
      const extra = boardCfg.cardFields.filter((f) => f !== boardCfg.groupField && r[f])
        .map((f) => relLabel(f, String(r[f]), boardCfg.byName, relMaps))
        .filter((label) => label && String(title).indexOf(label) === -1)
        .map((label) => '<div class="boardcardfield">' + esc(label) + '</div>').join("");
      return '<div class="boardcard" draggable="true" data-id="' + esc(r.id) + '">'
        + '<div class="boardcardtitle">' + esc(title) + '</div>' + extra + '</div>';
    }

    function draw() {
      const buckets = groupByColumn(all, boardCfg.groupField, boardCfg.columns);
      mount.innerHTML = '<div class="board">' + boardCfg.columns.map((col) =>
        '<div class="boardcol">'
        + '<div class="boardcolhead">' + esc(col || "(unset)")
        + '<span class="boardcolcount">' + buckets[col].length + '</span></div>'
        + '<div class="boardcolbody" data-drop="' + esc(col) + '">' + buckets[col].map(cardHtml).join("") + '</div>'
        + '</div>'
      ).join("") + '</div>';
      wireDrag();
    }

    function wireDrag() {
      mount.querySelectorAll(".boardcard").forEach((card) => {
        card.addEventListener("dragstart", (e) => {
          e.dataTransfer.setData("text/plain", card.dataset.id);
          e.dataTransfer.effectAllowed = "move";
        });
        // A board card opens its detail on click. HTML5 drag-and-drop does not
        // emit a click, so a plain click is unambiguous -- no drag-guard flag
        // (an earlier guard could get stuck and swallow a card's click).
        if (cfg.link !== false) {
          card.style.cursor = "pointer";
          card.addEventListener("click", () => {
            const id = card.dataset.id;
            const record = all.find((x) => x.id === id) || {id: id};
            window.location.href = cfg.href ? cfg.href(record) : "/" + collection + "/" + encodeURIComponent(id);
          });
        }
      });
      mount.querySelectorAll(".boardcolbody").forEach((body) => {
        body.addEventListener("dragover", (e) => { e.preventDefault(); e.dataTransfer.dropEffect = "move"; });
        body.addEventListener("drop", async (e) => {
          e.preventDefault();
          const id = e.dataTransfer.getData("text/plain");
          const toCol = body.dataset.drop;
          const record = all.find((r) => r.id === id);
          if (!record || String(record[boardCfg.groupField] || "") === toCol) return;
          // 10-flow: this is the ordinary record-update write path -- the
          // exact PUT any row action or generated-form edit already issues.
          // object_records.update_collection_record runs
          // _validate_field_transitions on it unconditionally; a `to` not
          // present in the source value's transitions list, or a failing
          // `when` guard, comes back non-2xx here. Nothing is applied
          // client-side before that response, so a rejected move needs no
          // undo -- re-drawing from `all` (unchanged) is the revert.
          const res = await fetch("/collections/" + collection + "/records/" + encodeURIComponent(id), {
            method: "PUT",
            credentials: "same-origin",
            headers: {"content-type": "application/json", accept: "application/json"},
            body: JSON.stringify({[boardCfg.groupField]: toCol}),
          });
          if (!res.ok) {
            let message = "Move rejected";
            try { const body = await res.json(); message = body.error || message; } catch (e2) {}
            draw();
            if (cfg.onError) cfg.onError(message); else alert(message);
            return;
          }
          load();
        });
      });
    }

    async function load() {
      const res = await fetch("/collections/" + collection + "/records?limit=500" + whereQueryString(cfg.where),
        {credentials: "same-origin", headers: {accept: "application/json"}});
      if (!res.ok) { mount.innerHTML = '<div class="state denied">Not available.</div>'; return; }
      const body = await res.json();
      all = body.records || [];
      draw();
    }

    (function sub() {
      if (window.dbbasicSubscribe) window.dbbasicSubscribe(collection, load);
      else setTimeout(sub, 400);
    })();
    // Resolve card relation labels once (org id -> name) before first draw.
    loadRelationMaps(boardCfg.byName, boardCfg.cardFields).then((m) => { relMaps = m; load(); });
    return load;
  }

  function renderTree(collection, cfg, mount, treeCfg) {
    function nodeHtml(node) {
      const kidsHtml = node.children.length
        ? '<div class="treekids">' + node.children.map(nodeHtml).join("") + '</div>'
        : "";
      const toggle = node.children.length
        ? '<button type="button" class="treetoggle" data-act="toggle">&#9662;</button>'
        : '<span class="treeleaf"></span>';
      const cap = node.truncated ? '<span class="pill" title="More levels not shown">&hellip;</span>' : "";
      return '<div class="treenode" style="--depth:' + node.depth + '">'
        + '<div class="treerow">' + toggle + '<span class="treetitle">' + esc(cardTitle(cfg, node.record)) + '</span>' + cap + '</div>'
        + kidsHtml + '</div>';
    }

    function draw(built) {
      if (built.cycleDetected) {
        console.warn("[dbbasicList tree] a parent_id cycle was detected in '" + collection
          + "' -- each affected node renders once and is not re-descended.");
      }
      mount.innerHTML = built.nodes.length
        ? '<div class="tree">' + built.nodes.map(nodeHtml).join("") + '</div>'
        : '<div class="state">Nothing yet.</div>';
      mount.querySelectorAll("button.treetoggle").forEach((btn) => {
        btn.addEventListener("click", () => {
          const kids = btn.closest(".treenode").querySelector(".treekids");
          if (kids) kids.classList.toggle("collapsed");
        });
      });
    }

    async function load() {
      const res = await fetch("/collections/" + collection + "/records?limit=500" + whereQueryString(cfg.where),
        {credentials: "same-origin", headers: {accept: "application/json"}});
      if (!res.ok) { mount.innerHTML = '<div class="state denied">Not available.</div>'; return; }
      const body = await res.json();
      draw(buildTree(body.records || [], {parentField: treeCfg.parentField, maxDepth: treeCfg.maxDepth}));
    }

    (function sub() {
      if (window.dbbasicSubscribe) window.dbbasicSubscribe(collection, load);
      else setTimeout(sub, 400);
    })();
    load();
    return load;
  }

  function renderCalendar(collection, cfg, mount, calCfg) {
    const today = new Date();
    let year = today.getUTCFullYear();
    let month = today.getUTCMonth();

    function eventHtml(r) {
      const title = cardTitle(cfg, r);
      return '<div class="calevent" title="' + esc(title) + '">' + esc(title) + '</div>';
    }

    function draw(bucketed) {
      const cells = monthGridDays(year, month);
      const undatedHtml = bucketed.undated.length
        ? '<div class="calundated"><span class="calundatedlabel">Undated (' + bucketed.undated.length + ')</span>'
          + bucketed.undated.map(eventHtml).join("") + '</div>'
        : "";
      const gridHtml = cells.map((c) => {
        const evs = bucketed.byDay[c.date] || [];
        return '<div class="calcell' + (c.inMonth ? "" : " dim") + '"><div class="caldate">'
          + Number(c.date.slice(8, 10)) + '</div>' + evs.map(eventHtml).join("") + '</div>';
      }).join("");
      const label = new Date(Date.UTC(year, month, 1)).toLocaleDateString(undefined, {month: "long", year: "numeric"});
      mount.innerHTML = '<div class="calheader"><button type="button" class="btn sm" data-act="prev">&lsaquo;</button>'
        + '<span class="calmonth">' + esc(label) + '</span>'
        + '<button type="button" class="btn sm" data-act="next">&rsaquo;</button></div>'
        + undatedHtml + '<div class="calgrid">' + gridHtml + '</div>';
      mount.querySelector('[data-act="prev"]').addEventListener("click", () => {
        month--; if (month < 0) { month = 11; year--; } load();
      });
      mount.querySelector('[data-act="next"]').addEventListener("click", () => {
        month++; if (month > 11) { month = 0; year++; } load();
      });
    }

    async function load() {
      const range = monthRange(year, month);
      const extra = {};
      extra[calCfg.dateField + ".gte"] = range.start;
      extra[calCfg.dateField + ".lte"] = range.end;
      const res = await fetch("/collections/" + collection + "/records?limit=500" + whereQueryString(cfg.where, extra),
        {credentials: "same-origin", headers: {accept: "application/json"}});
      if (!res.ok) { mount.innerHTML = '<div class="state denied">Not available.</div>'; return; }
      const body = await res.json();
      draw(bucketByDate(body.records || [], calCfg.dateField));
    }

    (function sub() {
      if (window.dbbasicSubscribe) window.dbbasicSubscribe(collection, load);
      else setTimeout(sub, 400);
    })();
    load();
    return load;
  }

  // ---- the plain row list (table/cards/feed -- unchanged behavior) -------

  window.dbbasicList = function (collection, cfg) {
    cfg = Object.assign({}, cfg || {});  // own a copy -- 65 may add cfg.where below
    const mount = qs(cfg.mount);
    const searchEl = cfg.search ? qs(cfg.search) : null;
    const sortEl = cfg.sort ? qs(cfg.sort) : null;
    let all = [];
    let activeReload = null;

    // Page hooks: cfg.slots[name] (per page) + window.dbbasicSlots (cross-page,
    // operator-registered). Both return HTML; the generator injects it at the
    // named hook. No-op when neither is present.
    function slotHtml(name, ctx) {
      let out = "";
      const local = cfg.slots && cfg.slots[name];
      if (local) { try { const h = local(ctx); if (h) out += h; } catch (e) {} }
      if (window.dbbasicSlots) out += window.dbbasicSlots.render(collection, name, ctx);
      return out;
    }

    function startRowList(notice, table) {
      // Relation columns (organization_id -> "Acme") resolved once for the table.
      let relMaps = {};
      // Edit/delete only for a row the viewer actually owns. BOTH ids must be
      // present and equal -- a log/report/rollup row has no owner_id, and a
      // report view has no cfg.owner, so `undefined === undefined` must not
      // wrongly offer actions. `cfg.rowActions:false` is the explicit off
      // switch. Shared by the row list and the table so both modes offer the
      // SAME actions -- flipping between them never silently drops edit/delete.
      function isMine(r) { return !!(cfg.owner && r.owner_id && r.owner_id === cfg.owner); }
      function rowActionsHtml(r) {
        const acts = (cfg.rowActions !== false && isMine(r))
          ? '<button class="rowbtn" data-act="edit" data-id="' + esc(r.id) + '" title="Edit">✎</button>'
            + '<button class="rowbtn danger" data-act="delete" data-id="' + esc(r.id) + '" title="Delete">✕</button>'
          : "";
        return acts + slotHtml("row_actions", r);
      }
      function row(r) {
        const title = cardTitle(cfg, r);
        const av = String(title).trim().charAt(0) || "?";
        // Every row links to its detail view -- reachability, "everything has a
        // url". The detail route is `/{collection}/{id}` by convention; cfg.href
        // overrides it (and is treated as an external/new-tab link), and
        // cfg.link === false opts a report/log (no detail page) out entirely.
        const href = (cfg.link === false) ? "" : (cfg.href ? cfg.href(r) : "/" + collection + "/" + encodeURIComponent(r.id));
        const linkAttrs = cfg.href ? ' target="_blank" rel="noopener"' : "";
        const titleHtml = href
          ? '<a href="' + esc(href) + '"' + linkAttrs + '>' + esc(title) + '</a>'
          : esc(title);
        const sub = cfg.subtitle ? cfg.subtitle(r) : "";
        const tags = cfg.tags ? cfg.tags(r) : "";
        const created = cfg.created ? cfg.created(r) : r.created_at;
        // Edit/delete only for a row the viewer actually owns. BOTH ids must be
        // present and equal -- a log/report/rollup row has no owner_id, and a
        // report view has no cfg.owner, so the old `undefined === undefined`
        // wrongly offered edit/delete on generated data. `cfg.rowActions:false`
        // is the explicit off switch (a list block over a log/report sets it).
        return '<div class="listrow"><div class="av">' + esc(av) + '</div><div class="body">'
          + '<div class="rowtitle">' + titleHtml + '</div>'
          + (sub ? '<div class="rowsub">' + esc(sub) + '</div>' : "")
          + '<div class="rowmeta">' + (created ? '<span class="when">' + esc(relDate(created)) + '</span>' : "")
          + pills(tags) + '</div></div><div class="rowactions">' + rowActionsHtml(r) + '</div></div>';
      }
      // A list over a collection with real volume (a rollup report, a busy log)
      // must never render every row -- that produces a 50,000px page. Cap the
      // rendered rows (cfg.limit, default 50) with a Show-all toggle; the data
      // is already fetched, so expanding is free. Universal: every list surface
      // in every app inherits the cap, no per-view opt-in.
      const DEFAULT_ROW_CAP = 50;
      let expanded = false;
      let lastList = [];
      function sortList(list) {
        // A report can sort by a real field+direction (cfg.sortBy) -- e.g. top
        // IPs by hits, descending -- otherwise fall back to the created_at
        // newest/oldest default the sort <select> drives.
        if (cfg.sortBy && cfg.sortBy.field) {
          const f = cfg.sortBy.field, dir = (cfg.sortBy.dir === "asc") ? 1 : -1;
          return list.slice().sort((a, b) => {
            const an = parseFloat(a[f]), bn = parseFloat(b[f]);
            const cmp = (!isNaN(an) && !isNaN(bn))
              ? (an - bn)
              : String(a[f] == null ? "" : a[f]).localeCompare(String(b[f] == null ? "" : b[f]));
            return cmp * dir;
          });
        }
        const s = list.slice().sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")));
        return (sortEl && sortEl.value === "oldest") ? s : s.reverse();
      }
      // ---- table list_mode: a real <table> over the SAME fetch/sort/cap/
      // subscribe pipeline as the row list, so it inherits filtering, the
      // render cap, detail links, and LIVE UPDATES for free. `table` is
      // {fields, byName} from the schema (list_fields + field metadata); cells
      // are formatted by field semantics; headers sort; rows link to detail.
      function humanName(n) { return String(n || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()); }
      function fmtCell(fname, r) {
        const f = (table && table.byName[fname]) || {};
        const t = String(f.type || "").toLowerCase();
        const v = r[fname];
        if (v == null || v === "") return "—";
        if (/_cents$/.test(fname) && (t === "integer" || t === "number")) {
          const n = parseFloat(v); if (!isNaN(n)) return "$" + (n / 100).toFixed(2);
        }
        if (t === "boolean") return v === "true" ? "Yes" : "No";
        if (t === "datetime" || t === "date") return esc(relDate(v));
        if (f.relation) return esc(relLabel(fname, String(v), table.byName, relMaps));
        if (isEnumField(f)) return enumBadge(v);
        if (/(^|_)tags?$/.test(fname) || t === "array" || t === "list") return pills(v) || "—";
        return esc(String(v));
      }
      function tableBody(shown) {
        const sf = cfg.sortBy && cfg.sortBy.field;
        // Peer with the row list: an owned row gets the same edit/delete (+ any
        // row_actions slot) in a trailing column. Only add the column when a
        // shown row actually has actions, so a read-only/report table stays a
        // clean grid with no empty trailing column.
        const showActs = shown.some((r) => rowActionsHtml(r) !== "");
        const th = table.fields.map((n) => {
          const f = table.byName[n] || {};
          const arrow = (sf === n) ? (cfg.sortBy.dir === "asc" ? " ▲" : " ▼") : "";
          return '<th data-sort="' + esc(n) + '">' + esc(f.label || humanName(n)) + arrow + '</th>';
        }).join("") + (showActs ? '<th class="dtactions" aria-label="Actions"></th>' : "");
        const trs = shown.map((r) => {
          const tds = table.fields.map((n) => '<td>' + fmtCell(n, r) + '</td>').join("")
            + (showActs ? '<td class="dtactions">' + rowActionsHtml(r) + '</td>' : "");
          return '<tr data-id="' + esc(r.id) + '"' + (cfg.link !== false ? ' class="clickrow"' : "") + '>' + tds + '</tr>';
        }).join("");
        return '<div class="dtablewrap"><table class="dtable"><thead><tr>' + th
          + '</tr></thead><tbody>' + trs + '</tbody></table></div>';
      }
      function render(list) {
        lastList = list;
        const sorted = sortList(list);
        const cap = (cfg.limit != null) ? cfg.limit : DEFAULT_ROW_CAP;
        const shown = expanded ? sorted : sorted.slice(0, cap);
        const ctx = {collection: collection, count: list.length};
        const body = shown.length
          ? (table ? tableBody(shown) : shown.map(row).join(""))
          : (slotHtml("empty", ctx) || '<div class="state">Nothing yet.</div>');
        const noticeHtml = notice ? '<div class="state notice">' + esc(notice) + '</div>' : "";
        let more = "";
        if (sorted.length > cap) {
          more = expanded
            ? '<button class="listmore" data-act="collapse">Show fewer</button>'
            : '<button class="listmore" data-act="showall">Show all ' + sorted.length + '</button>';
        }
        mount.innerHTML = noticeHtml + slotHtml("before_list", ctx) + body + more + slotHtml("after_list", ctx);
      }
      async function load() {
        const res = await fetch("/collections/" + collection + "/records?limit=500" + whereQueryString(cfg.where),
          {credentials: "same-origin", headers: {accept: "application/json"}});
        if (!res.ok) { render([]); return; }
        const body = await res.json(); all = body.records || []; render(all);
      }
      // Last-request-wins: each keystroke bumps a sequence token so a slow
      // in-flight /api/search response can't land AFTER a newer input (or a
      // backspace-to-empty) and overwrite the current view with stale
      // filtered results -- the "clear the box but it still shows filtered"
      // bug. Both the empty path and every await checkpoint honor the token.
      let searchSeq = 0;
      async function search(q) {
        const seq = ++searchSeq;
        if (!q) { render(all); return; }
        const res = await fetch("/api/search?q=" + encodeURIComponent(q) + "&collections=" + collection + "&limit=50",
          {credentials: "same-origin", headers: {accept: "application/json"}});
        if (seq !== searchSeq || !res.ok) return;
        const body = await res.json();
        if (seq !== searchSeq) return;
        render((body.results || {})[collection] || []);
      }

      if (searchEl) searchEl.addEventListener("input", (e) => search(e.target.value.trim()));
      if (sortEl) sortEl.addEventListener("change", () => render(all));
      mount.addEventListener("click", async (e) => {
        const more = e.target.closest("button.listmore");
        if (more) { expanded = (more.dataset.act === "showall"); render(lastList); return; }
        // table: click a header to sort by that column (toggle desc/asc); click
        // a row to open its detail (same reachability as the row list / cards).
        const thSort = e.target.closest("th[data-sort]");
        if (thSort) {
          const f = thSort.dataset.sort, cur = cfg.sortBy || {};
          cfg.sortBy = {field: f, dir: (cur.field === f && cur.dir !== "asc") ? "asc" : "desc"};
          render(lastList); return;
        }
        // A row click opens detail -- UNLESS the click landed on an action
        // button or a link inside the row (the actions column, or a linked
        // cell), which have their own behavior.
        const clickrow = e.target.closest("tr.clickrow");
        if (clickrow && !e.target.closest("button, a")) {
          const id = clickrow.dataset.id, rec = all.find((x) => x.id === id) || {id: id};
          window.location.href = cfg.href ? cfg.href(rec) : "/" + collection + "/" + encodeURIComponent(id);
          return;
        }
        const btn = e.target.closest("button.rowbtn"); if (!btn) return;
        const id = btn.dataset.id;
        if (btn.dataset.act === "delete") {
          if (!confirm("Delete this?")) return;
          await fetch("/collections/" + collection + "/records/" + encodeURIComponent(id),
            {method: "DELETE", credentials: "same-origin", headers: {accept: "application/json"}});
          load();
        } else if (btn.dataset.act === "edit" && cfg.onEdit) {
          const r = all.find((x) => x.id === id); if (r) cfg.onEdit(r);
        }
      });
      (function sub() {
        if (window.dbbasicSubscribe) window.dbbasicSubscribe(collection, load);
        else setTimeout(sub, 400);
      })();
      // In table mode, resolve relation labels once before the first render so
      // columns show names not FK ids; the plain row list has no such columns.
      if (table) {
        loadRelationMaps(table.byName, table.fields).then((m) => { relMaps = m; load(); });
      } else {
        load();
      }
      return load;
    }

    // 60: resolve list_mode from the schema once, then delegate to
    // board/tree/calendar -- or fall through to the plain row list above,
    // completely unchanged, when the mode is table/cards/feed/absent, the
    // flag is off, or the mode's field can't be derived (Degradation: falls
    // back to table with a visible notice, never a silent empty page).
    // 65 multi-entity: auto-scope a TOP-LEVEL browse list to the nav
    // switcher's current entity. Only when there is no existing cfg.where --
    // a `related` child list (a journal's postings, matched by journal_id) or
    // an explicitly filtered list already carries the scope it needs, and
    // adding the current entity there would wrongly hide a parent's children
    // when a different entity is selected. Gated on an entity actually being
    // selected AND the collection having an entity_id field, so non-entity
    // lists and the "All entities" state are untouched (the same never-hide-
    // by-default posture 58 takes for its own filters).
    async function scopeToCurrentEntity() {
      const cur = (window.dbbasicEntity && window.dbbasicEntity()) || "";
      if (!cur || cfg.where) return;
      try {
        const res = await fetch("/api/schema/" + encodeURIComponent(collection),
          {credentials: "same-origin", headers: {accept: "application/json"}});
        if (!res.ok) return;
        const schema = (await res.json()).schema;
        if (schema && (schema.fields || []).some((f) => f.name === "entity_id")) {
          cfg.where = {entity_id: cur};
        }
      } catch (e) { /* schema fetch failed -- leave the list unscoped, never blank */ }
    }

    // A grouped mode (board/tree/calendar) hides the row list's search + sort
    // box -- those controls only drive the row list. Table and plain-list keep
    // them.
    function setGroupedControls(hidden) {
      if (sortEl) sortEl.style.display = hidden ? "none" : "";
      if (searchEl) searchEl.style.display = hidden ? "none" : "";
    }

    // board <-> table switcher. Persist the choice and reload rather than
    // swap in place: window.dbbasicSubscribe has no unsubscribe, so an
    // in-place swap would leave the previous mode's change-log handler live
    // and double-render. One page load = one subscription. The choice is
    // per-collection and survives navigation.
    const modeStoreKey = "dbb_listmode_" + collection;
    function storedMode() {
      try { return window.localStorage.getItem(modeStoreKey); } catch (e) { return null; }
    }
    function storeMode(m) {
      try { window.localStorage.setItem(modeStoreKey, m); } catch (e) {}
    }
    function renderSwitcher(current, modes) {
      if (!mount || !mount.parentNode) return;
      const bar = document.createElement("div");
      bar.className = "listmodes";
      bar.setAttribute("role", "tablist");
      bar.innerHTML = modes.map((m) =>
        '<button type="button" class="modebtn' + (m.key === current ? " active" : "")
        + '" data-mode="' + m.key + '" aria-selected="' + (m.key === current) + '">'
        + esc(m.label) + "</button>").join("");
      mount.parentNode.insertBefore(bar, mount);
      bar.addEventListener("click", (e) => {
        const btn = e.target.closest("[data-mode]");
        if (!btn) return;
        const mk = btn.getAttribute("data-mode");
        if (mk === current) return;
        storeMode(mk);
        window.location.reload();
      });
    }

    // Filter bar: a row of selects above the list, one per filter field. A
    // change ANDs the picked value into cfg.where and reloads the active mode
    // -- server-side narrowing that composes with the caller's own where
    // (entity scope, a parent FK) and with the client-side search box. Shown
    // in every mode. Insert before mount so a mode re-render (which replaces
    // mount's contents) never disturbs it.
    function renderFilters(filters, onChange) {
      if (!filters.length || !mount || !mount.parentNode) return;
      const bar = document.createElement("div");
      bar.className = "listfilters";
      bar.innerHTML = filters.map((f) => {
        let opts = '<option value="">All ' + esc(f.label) + '</option>';
        if (f.type === "enum") for (const o of f.options) opts += '<option value="' + esc(o) + '">' + esc(o) + '</option>';
        else if (f.type === "boolean") opts += '<option value="true">Yes</option><option value="false">No</option>';
        return '<select class="filterctl" data-filter="' + esc(f.name) + '" aria-label="Filter by ' + esc(f.label) + '">' + opts + '</select>';
      }).join("") + '<button type="button" class="filterclear" data-filterclear hidden>Clear</button>';
      mount.parentNode.insertBefore(bar, mount);
      const clearBtn = bar.querySelector("[data-filterclear]");
      function sync() {
        const any = [].some.call(bar.querySelectorAll("select[data-filter]"), (s) => s.value);
        if (clearBtn) clearBtn.hidden = !any;
      }
      bar.addEventListener("change", (e) => {
        const sel = e.target.closest("select[data-filter]");
        if (!sel) return;
        onChange(sel.getAttribute("data-filter"), sel.value);
        sync();
      });
      if (clearBtn) clearBtn.addEventListener("click", () => {
        bar.querySelectorAll("select[data-filter]").forEach((s) => { s.value = ""; });
        onChange(null, null);
        sync();
      });
    }

    (async function boot() {
      await scopeToCurrentEntity();
      // Capture the caller/entity scope AFTER scopeToCurrentEntity so user
      // filters layer on top of it rather than replacing it.
      const baseWhere = cfg.where ? Object.assign({}, cfg.where) : null;
      const resolved = await resolveListMode(collection);

      // Wire filters (enum/boolean) once, before dispatching a mode -- they
      // apply to whichever renderer becomes active.
      const filterState = {};
      function applyFilterWhere() {
        const merged = Object.assign({}, baseWhere || {});
        for (const k in filterState) if (filterState[k] != null && filterState[k] !== "") merged[k] = filterState[k];
        cfg.where = Object.keys(merged).length ? merged : null;
      }
      function onFilterChange(field, value) {
        if (field === null) { for (const k in filterState) delete filterState[k]; }
        else if (value == null || value === "") delete filterState[field];
        else filterState[field] = value;
        applyFilterWhere();
        if (activeReload) activeReload();
      }
      renderFilters(buildFilters(resolved.schema), onFilterChange);

      // A board-declared collection that also has list_fields can render as a
      // table too, so offer a switcher (the classic kanban<->list toggle). The
      // user's pick (localStorage) wins over the schema default.
      const tableConfig = buildTableConfig(resolved.schema);
      const canBoard = resolved.kind === "board";
      if (canBoard && tableConfig) {
        const pick = storedMode() === "table" ? "table" : "board";
        renderSwitcher(pick, [{key: "board", label: "Board"}, {key: "table", label: "Table"}]);
        if (pick === "table") {
          setGroupedControls(false);
          activeReload = startRowList(null, tableConfig);
        } else {
          setGroupedControls(true);
          activeReload = renderBoard(collection, cfg, mount, resolved.config);
        }
        return;
      }
      if (resolved.kind === "board" || resolved.kind === "tree" || resolved.kind === "calendar") {
        setGroupedControls(true);
        if (resolved.kind === "board") { activeReload = renderBoard(collection, cfg, mount, resolved.config); return; }
        if (resolved.kind === "tree") { activeReload = renderTree(collection, cfg, mount, resolved.config); return; }
        activeReload = renderCalendar(collection, cfg, mount, resolved.config); return;
      }
      // table keeps the search box + sort control (unlike the grouped modes) --
      // it's the row list rendered as a <table>, so those controls still apply.
      if (resolved.kind === "table") { activeReload = startRowList(resolved.notice, resolved.config); return; }
      activeReload = startRowList(resolved.notice);
    })();

    return {reload: () => { if (activeReload) activeReload(); }};
  };
})();
"""


def GET(request):
    return {"content_type": "application/javascript; charset=utf-8", "body": _JS}
