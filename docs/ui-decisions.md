# UI Interaction Decisions

A living log of the small, repeated interaction decisions the generative UI
makes — detail vs edit, page vs in-place, when a list caps, when a row links.
The companion to `design-system.md`: that doc is the *architecture* (semantics →
tokens → renderers, "define once, project everywhere"); this doc is the
*behavior* those renderers agree on.

**Why write these down.** Because the UI is one generative renderer, a decision
here is not a per-screen choice — it applies to *every* app at once. The value
is not picking the clever option on each screen; it's that the same rule holds
everywhere, so nothing surprises you. When a decision is inconsistent across
screens, that is a bug (a copy leaked), not a style preference. Most of these
were found by screenshotting the running product and noticing the inconsistency.

Format per decision: **Decision · Rationale · Applies to · Status.** Add new
ones as they're settled; move Open Questions up as they're decided.

---

## Decided

### 1. Detail is a page (its own URL); edit and create are in-place.
- **Decision:** Opening a record's *detail* navigates to `/{collection}/{id}`.
  *Editing* or *creating* a record happens in-place (an inline form on the
  current page), never a separate page.
- **Rationale:** Detail is a *destination* — it must be linkable, bookmarkable,
  back-button-able ("everything has a url"), and it can hold a **composed view**
  (detail block + related records + aggregate) a modal can't. Edit/create is a
  transient *action* — you must not lose your list position or scroll to do it.
- **Applies to:** every list/board row and card (→ detail page); every `+ New X`
  button and every row `✎` (→ in-place form).
- **Status:** shipped. Rows/cards link to `/{collection}/{id}`; `+ New`/`✎`
  reveal the generated form in place.

### 2. Reports and logs have no detail link and no row actions.
- **Decision:** A list over a generated/read-only collection (a rollup target,
  a log) shows neither edit/delete buttons nor a click-through to detail.
- **Rationale:** A rollup row (top IP → hits) has no detail page and can't be
  edited — the next recompute overwrites it. Offering either is a dead end.
- **Applies to:** any list block with `row_actions: false` (which also implies
  `link: false`).
- **Status:** shipped (analytics reports).

### 3. Lists render-cap at 50 rows, with Show-all.
- **Decision:** A list renders at most 50 rows (overridable per block via
  `row_limit`) with a **Show all / Show fewer** toggle; the data is already
  fetched, so expanding is free.
- **Rationale:** Without a cap, a busy collection (a real-traffic rollup, a big
  browse) renders a 50,000-px page. Found by screenshotting live `/analytics`.
- **Applies to:** every list surface in every app, automatically.
- **Status:** shipped.

### 4. Every record has a reachable URL; detail routes accept any id.
- **Decision:** A detail route is `/{collection}/{id}` with **no `:uuid`
  constraint** — any id segment routes to the detail view. Records still get
  UUIDs on create; the route simply doesn't *reject* other ids.
- **Rationale:** "Everything has a url" must hold for *every* record, including
  ones with friendly/seed ids. Once rows and cards link (decision 1), a
  `:uuid`-only route 404s exactly the records a demo shows first. A missing
  record is the view's own not-found state, not a routing 404.
- **Applies to:** every detail route in every package. (The `:uuid` matcher
  still exists for a route that deliberately wants to enforce shape.)
- **Status:** shipped.

### 5. Board columns flex to fit; scroll only when genuinely too many.
- **Decision:** Board columns are `flex: 1 1 200px` (min 200, max 340), not a
  fixed width. A few columns fill the container; the board scrolls **internally**
  (never the page) only when columns would go below their min width.
- **Rationale:** Fixed-width columns overflowed the container and forced a
  *page-level* horizontal scrollbar for a mostly-empty board. The page must
  never scroll sideways.
- **Applies to:** every board (kanban / lead-pipeline).
- **Status:** shipped.

### 6. A report renders as a sorted top-N.
- **Decision:** A list block can declare `sort_by` (+ `sort_dir`) and
  `row_limit` so it renders a true top-N by a real field (top IPs by hits desc),
  not insertion order.
- **Rationale:** "Top" must mean *ranked*, or the report is noise.
- **Applies to:** analytics and any report-shaped list.
- **Status:** shipped.

### 7. A data-dense collection renders as a live table (`list_mode: "table"`).
- **Decision:** A schema (or a view's list block) can declare `list_mode:
  "table"` to render its `list_fields` as a dense HTML table — sortable column
  headers, one row per record, cells formatted by field type (money `_cents` →
  `$`, boolean → Yes/No, datetime → relative). It reuses the same
  `startRowList` engine as the rich-row list, so it **inherits for free**: the
  search box, the 50-row cap + Show-all (#3), client-side sort (#6),
  row→detail navigation (#1), and — the differentiator — **realtime
  re-render** on the change log. A table here is not a static snapshot the way
  django-tables2 is; it updates live as records change.
- **Rationale:** Rich rows (title + snippet) suit content/feeds; they waste
  space and hide structure for inherently tabular data (invoices, contacts,
  links, a rollup). One renderer, chosen per collection by a single schema key,
  keeps every table consistent instead of each app hand-rolling one.
- **Applies to:** any collection whose index goes through `dbbasicList`; opt in
  with `views.list_mode: "table"` + `list_fields`. Header click toggles
  sort field/dir; the whole row is the detail link.
- **Status:** shipped. Live on `/articles` (Title · Published On · Published).

---

## Open questions (decide, then move up)

### A. Create UX on data-heavy pages.
`+ New X` currently reveals a full-width inline form that pushes the list/board
down and out of view. Fine when empty; disruptive when the page has data. Options:
a collapsible/side form, a modal, or navigating to a dedicated create route.
Leaning: keep in-place (decision #1) but render the form as a **right-side drawer
or a modal** on data-heavy index pages, so the list stays visible. Undecided.

### B. Triage lists → master-detail split-pane.
An inbox / review-queue / messaging thread list processes many items fast;
navigating away per item (decision #1) is wrong there. Proposed: a
`list_mode: "split"` — click a row, preview in a right pane, never navigate.
A per-app opt-in exception to decision #1, not the default. Not built.

### C. Detail view de-emphasizes the raw id.
The generated detail renders the record's `id` (a UUID) as the first field row —
noise. Proposal: hide `id`/computed fields from the default detail field table
(they're still reachable), the way `list_fields` already curates the list.

### D. ~~Seed data should use UUIDs.~~ RESOLVED (decision 4).
Was: friendly seed ids (`c-grace`, `t-demo-1`) 404'd against `{id:uuid}` routes
once rows linked. Resolved by relaxing detail routes to accept any id (decision
4) rather than rewriting seed ids and their FK references — every record is
reachable whatever its id shape.

---

## How to use this

- Before adding a per-screen interaction, check here — the decision may already
  exist and just needs wiring.
- If you find two screens behaving differently for the same interaction, that's
  a bug: pick the rule (add it here) and fix it in the shared renderer, not the
  screen.
- These decisions live in `packages/app-theme/objects/site/` (list/form/detail
  generators) and `packages/app-views/objects/site/view_render.py` (block →
  generator config), the single places that project them everywhere.
