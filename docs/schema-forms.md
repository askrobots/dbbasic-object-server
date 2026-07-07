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
- `views.list_mode` — the collection's reading mode: `table` (rows compared
  to each other: tasks, orders, files), `cards` (rows read individually:
  notes, contacts, messages), or `feed` (sequential with social context).
  Generated list views pick their shape from this.
- `views.list_fields` — columns/summary fields for list rendering.
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
