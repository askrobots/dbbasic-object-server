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

### 8. The generated form matches a hand-built one (spacing + themed selects).
- **Decision:** The generative form uses **one** spacing mechanism (grid
  `gap`), collapses a field's error line until there's an actual message
  (`.err:empty { display: none }`), and themes every `<select>` itself
  (`appearance: none` + our own chevron).
- **Rationale:** The generated form was looser than a bespoke one for two
  invisible reasons — it inherited *both* the grid gap and the global
  `.stack > * + *` margin (doubled space), and reserved a blank 1rem error
  line under every field (~44px between fields vs ~14px). And an unstyled
  `<select>` renders as Safari's beveled native control but Chrome's flat one,
  so the same page looked different per browser. A generic renderer has to be
  *at least as good* as the hand-built page it replaces, or migrating onto it
  (decision below) is a regression. Found by comparing the bespoke Projects
  form to the generated Notes form side by side.
- **Applies to:** every generated form (`window.dbbasicForm`) and every
  `<select>` in the product.
- **Status:** shipped. Measured 14px inter-field gap, no reserved error space.

### 9. A board collection can toggle to a table (persisted per-collection).
- **Decision:** A collection declared `list_mode: "board"` that also has
  `list_fields` shows a small **Board / Table** segmented control; the table is
  the same generative table from decision #7. The choice is stored per
  collection in `localStorage` and survives navigation.
- **Rationale:** A kanban is the right default for a workflow surface, but the
  same records are often better *scanned* as a dense sortable table — the
  classic kanban⇄list toggle. Both renderers already exist behind one
  `resolveListMode`, so it's a switch, not new rendering. Switching **reloads**
  rather than swapping in place: `dbbasicSubscribe` has no unsubscribe, so an
  in-place swap would leave the previous mode's change-log handler live and
  double-render. One page load = one subscription.
- **Applies to:** any board-declared collection with `list_fields` (tasks
  today). Tree/calendar toggles are a possible follow-on, same mechanism.
- **Status:** shipped (tasks Board/Table).

### 10. Make the generic layer ≥ the bespoke page before deleting the bespoke.
- **Decision:** When a hand-built page does something the generic renderer
  doesn't (or does *better*), fix the generic renderer to that bar **first**,
  then delete the bespoke page onto it — never the reverse order.
- **Rationale:** A bespoke page that looks better than the generic one isn't
  just debt to delete; it's a spec for a quality gap in the shared layer. The
  Projects form (tight) was showing what the generated form (loose, #8) should
  be. Delete-first would have made Projects *worse*; fix-first made every
  form better and Projects free.
- **Applies to:** every "migrate a bespoke page onto the generator" task
  (Projects done; analytics/commerce pages next).
- **Status:** doctrine. First applied migrating `site/projects.py` (~60 lines
  of hand-rolled table/form/fetch deleted) onto `dbbasicList` + `dbbasicForm`.

### 11. A collection filters by declaring `filter_fields` (works on every mode).
- **Decision:** A schema's `views.filter_fields` renders a **filter bar** above
  the list — an enum field → a select of its options, a boolean → Yes/No. A
  pick ANDs into the fetch `where` and reloads the active mode. Filters compose
  with the caller's own `where` (entity scope, a parent FK) and with the
  client-side search box; a **Clear** button appears once anything is set.
- **Rationale:** The list renderer had sort + live but not filters — the third
  leg of "a table, but generative and alive." Filtering is server-side (the
  same `field=value` path the board already uses, applied after the permission
  row filter), so a filtered list can only ever *narrow* what the viewer may
  already see — never widen it. One schema key, filters on list, table, and
  board at once.
- **Applies to:** any collection that declares `filter_fields` (tasks:
  status + urgency today). Text is left to the search box; a date-range control
  is the next increment (needs the dotted-operator `gte/lte` query path).
- **Status:** shipped. Verified server-side narrowing (6 tasks → 2 on
  status=open) and that the bar renders in board and table.

### 12. A collection opts into behaviors via `capabilities` (not just display).
- **Decision:** Beyond the display keys (`list_mode`/`filter_fields`/…), a
  schema declares generic *behaviors* under a root-level `capabilities` key,
  wired by the platform rather than hand-built per app. First one:
  `capabilities.comments: true` → the detail page grows a comment thread
  (`window.dbbasicThread`, backed by the polymorphic `thread_comments`
  collection), no per-view block, no per-app comment table.
- **Rationale:** Comments existed **four times** (task_comments, thread_comments,
  profile_comments, interactions) — the same "a record has a thread hung off
  it" copied per app. That's a workaround for a layer that should be uniform.
  One capability flag collapses them: the widget is polymorphic
  (parent_collection + parent_id), so any collection gets comments by declaring
  one key. This is the *behavior/connection* layer sitting on top of the
  display layer — the same "define once, project everywhere," for what a
  collection *does* and *connects to*, not just how it looks.
- **Applies to:** any collection (tasks today). `capabilities` is whitelisted in
  schema normalization and surfaced in `/api/schema` so the client can wire it.
- **Status:** shipped. task_comments migrated onto thread_comments; the old
  related block removed; verified a live widget-posted comment attributes
  correctly. Next capabilities: `attachments`, `shareable`/permissions.
- **Gotchas found building it:** a field that is `required` **and** `read_only`
  can never be created through the HTTP write path (only a server-side
  `preserve_read_only` bypass) — `thread_comments.parent_*` had to drop
  `read_only` to be settable on create. And `owner_id` (a `public:hidden` field)
  is server-set from the session; a client value is rejected, and it's redacted
  from other readers — so comments stamp a separate `author_name` for
  attribution.

### 13. Every data collection carries created_at; detail always shows it.
- **Decision:** `created_at` (datetime, read-only, server-owned) is a baseline
  field on every user-facing data collection, and the detail generator always
  surfaces `created_at`/`updated_at` as record metadata — even when
  `detail_fields` curates the main fields — the way the list row already shows
  a timestamp.
- **Rationale:** "When was this created" is baseline info every record should
  carry and every detail should show; `detail_fields` is for curating the
  *content* fields, not for suppressing metadata. Found by spotting records
  with no timestamp (a collection missing the field entirely — e.g. projects)
  and detail pages hiding it (tasks/contacts/forum_topics curated it out).
- **Applies to:** all user-facing collections (config/system ones —
  feature_flags, user_prefs, shell_*, *_definitions, project_access,
  ai_prices, dbbasic_probe — intentionally omit it). Detail metadata append is
  automatic in the shared renderer.
- **Status:** shipped. Added the field to projects + organizations/files/
  events/interactions/time_logs/tags/templates; detail appends the timestamps.
  (Old records predate the field and read it back empty — new ones get a
  server-set value.)

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
