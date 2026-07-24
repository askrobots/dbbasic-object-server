# Schema Field Contract — Generated Forms and Views

This is the contract that lets user interfaces generate themselves from
schemas. One schema drives every surface at once: Scroll's desktop forms, a
server-rendered web form, an agent reading constraints over MCP, and the
record-write APIs enforcing the same rules for all of them.

The governing principle: **schemas declare semantics, never widgets.** A
field says "one of these values" or "points at a projects record" — each
interface maps that meaning onto its own idiom (a dropdown, a picker, a
JSON constraint, a spoken prompt). New kinds of interface plug in later
without touching the data.

Everything here is enforced by the server on record writes today. None of
it is a DSL — it is keys in the schema JSON document, checked by explicit
code, versioned and rolled back through the normal schema history.

## Field Keys

```json
{
  "name": "urgency",
  "type": "enum",
  "required": true,
  "label": "Urgency",
  "help": "How soon this needs attention",
  "placeholder": "Choose…",
  "default": "normal",
  "enum": ["low", "normal", "high", "critical"]
}
```

- `name` — required; letters/digits/underscores.
- `type` — `text` (default), `textarea`, `integer`, `number`, `boolean`,
  `date`, `datetime`, `enum`, `computed`. Types are validated on write
  (integers must parse, dates must be ISO, enums must match, and so on).
  Boolean fields accept several spellings on input (`True`, `1`, `yes`)
  but are stored canonically as `true`/`false`, so permission row
  filters like `{"is_public": "true"}` match reliably.
- `required` — empty values rejected on write.
- `label`, `help`, `placeholder` — presentation text for generated forms.
- `default` — filled in when the field is not submitted.
- `enum` — allowed values; writes outside the list are rejected. Renders as
  a dropdown/segment control in form UIs.
- `computed` / `read_only` — the server rejects client writes to these
  fields; forms render them display-only.
- `transitions` — for lifecycle fields, which values each current value
  may move to. Enforced on record update; values missing from the map
  are terminal. This is data plus one check, not a state machine
  framework — no hooks, no callbacks, no side effects:

  ```json
  {"name": "status", "type": "enum",
   "enum": ["open", "assigned", "done"],
   "transitions": {"open": ["assigned"], "assigned": ["done", "open"]}}
  ```

  Form UIs can offer only the moves allowed from the record's current
  value; the server rejects everything else either way.

  A list entry can also be a guarded object instead of a plain string,
  adding *who* on top of *whether*:

  ```json
  {"transitions": {
    "assigned": [{"to": "open", "when": {"assigned_to": "$user_id"}}]
  }}
  ```

  The move is allowed only once every `when` clause matches the record's
  current stored values against the resolved subject variable
  (`$user_id`, `$account_id`, `$accessible_projects`, `$owned_projects`,
  `$writable_projects`) or a literal string — the same closed set and
  matching rules row filters use (see docs/permissions-model.md). Guards
  are only evaluated where a request subject is available (the HTTP
  update path); direct library callers still get the plain validity
  check (the move's `to` must be in the list) but not the guard.
- `relation` — a validated pointer to another collection:

  ```json
  {"name": "project_id", "relation": {"collection": "projects", "display_field": "name"}}
  ```

  On write, the value must be an existing record id in that collection
  (`projects/p999` missing → 400). `display_field` tells form UIs which
  field of the target record to show in pickers. A bare string
  (`"relation": "projects"`) is shorthand. This is deliberately a pointer
  plus a display hint, not an association framework — no joins, no lazy
  loading, no cascades.
- `validation` — explicit bounds, all enforced on write:

  ```json
  {"validation": {"min_length": 3, "max_length": 80, "pattern": "^[a-z-]+$", "min": 0, "max": 100}}
  ```

  `pattern` is **full-matched** against the whole value (`re.fullmatch`),
  not searched — so it must describe the entire field, not a prefix. A URL
  check is `^https?://\S+$`, not `^https?://` (that would only match the
  literal scheme and reject every real URL). The `^`/`$` anchors are
  therefore redundant but fine to keep for readability.

## Schema Root Keys

Presentation metadata lives beside the fields and versions with them —
Scroll's "Save Layout" persists here, so form design has history and
rollback like source code:

```json
{
  "fields": [...],
  "forms": {
    "default": {"fields": ["title", "description", "project_id", "urgency", "due_date", "assigned_to"]}
  },
  "views": {
    "list_mode": "table",
    "list_fields": ["title", "urgency", "due_date", "assigned_to"]
  }
}
```

- `forms.default.fields` — field order for the generated record form.
  Additional named forms can define alternate layouts.
- `views.list_mode` — how the generated list renders. The shared list
  renderer (`window.dbbasicList`) resolves it from the schema:
  - `table` — a dense, sortable HTML table over `list_fields`; cells format by
    type (money `_cents`, boolean, relative dates, enum badges), and relation
    columns show the target's label, not the raw id.
  - `board` — a kanban grouped by an enum field, drag-to-transition (the drag
    issues the ordinary status write, so `flow` transitions still gate it). A
    board collection that also has `list_fields` gets a **Board ⇄ Table**
    toggle (the choice persists per collection).
  - `tree` — nests a self-relation (`parent_id`) into a hierarchy.
  - `calendar` — buckets by a date field.
  - absent / `cards` / `feed` — the plain rich-row list.

  Every mode shares one fetch/sort/cap/search/realtime pipeline, so all of
  them inherit filtering, the 50-row cap, and live updates. A mode whose
  required field can't be derived falls back to the row list with a visible
  notice — never a blank page.
- `views.list_fields` — columns (table), card fields (board), or summary
  fields (rows). Relation fields resolve to the referenced record's label.
- `views.filter_fields` — fields to expose as a **filter bar** above the list
  (enum → a select of its options, boolean → Yes/No). A pick narrows the fetch
  server-side (`field=value`, after the permission row filter) and composes
  with the search box; works in every mode.
- `search` — opts the collection into global search (`GET /api/search`
  and the `global_search` MCP tool):

  ```json
  {"search": {"fields": ["title", "content"], "result_fields": ["id", "title"]}}
  ```

  `fields` are the searchable fields; `result_fields` (optional) trims
  what each search hit returns, defaulting to `id` plus
  `views.list_fields`. Collections without a `search` section never
  appear in search results. Search runs inside the permission engine, so
  row filters and field permissions bound what any caller can find.
- `formula` / `rollup` (on a `type: computed` field) — derived values,
  materialized on write: a formula computes from sibling fields
  (`"first_name + \" \" + last_name"`); a rollup aggregates a child
  collection (`{"collection": "fin_journal_lines", "fk_field": "journal_id",
  "op": "sum", "field": "debit_cents"}`). Stored like any field, so every
  view/filter/sort sees them; kept current by the write path. See
  [`validation-and-logic.md`](validation-and-logic.md).
- `capabilities` — generic per-collection *behaviors* on top of this display
  layer: `{"comments": true}`, `{"attachments": true}`, `{"shareable": true}`
  grow a comment thread, attachment list, and owner-checked sharing on the
  detail page from one flag each. See [`capabilities.md`](capabilities.md).

## Worked Example: tasks

The acceptance test for this contract is Scroll's New Task screen,
expressed entirely as data:

```json
{
  "fields": [
    {"name": "id"},
    {"name": "title", "type": "text", "required": true, "label": "Title",
     "validation": {"min_length": 1, "max_length": 200}},
    {"name": "description", "type": "textarea", "label": "Description"},
    {"name": "project_id", "label": "Project",
     "relation": {"collection": "projects", "display_field": "name"}},
    {"name": "urgency", "type": "enum", "label": "Urgency", "default": "normal",
     "enum": ["low", "normal", "high", "critical"]},
    {"name": "due_date", "type": "date", "label": "Due Date"},
    {"name": "assigned_to", "label": "Assigned To",
     "relation": {"collection": "users", "display_field": "display_name"}}
  ],
  "forms": {"default": {"fields": ["title", "description", "project_id",
                                     "urgency", "due_date", "assigned_to"]}},
  "views": {"list_mode": "table",
             "list_fields": ["title", "urgency", "due_date", "assigned_to"]}
}
```

A form renderer walks `forms.default.fields`, looks up each field's
semantics, and emits the right control. The record POST enforces every
rule the form promised — and the same rules bind a record created over
MCP or raw HTTP, so no surface can bypass what another surface displays.
