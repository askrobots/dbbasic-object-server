# Package Authoring

This is the practical guide for building a DBBASIC package: one installable
directory that ships an app piece — objects, schemas, and seed data — without
a deploy pipeline. It is written for humans and AI tools that generate
packages. The runtime contract details live in `runtime-contract.md#packages`;
this page is the authoring workflow.

## What A Package Is

A package is a directory under `packages/` (or `DBBASIC_PACKAGES_DIR`) with a
manifest and package-relative content:

```text
packages/{package_id}/
  dbbasic-package.json
  objects/            # object source files
  schemas/            # collection schema JSON files
  seed/               # initial records TSV files
  permissions/        # declared, not yet installed (merge semantics pending)
  migrations/         # declared, not yet run (run semantics pending)
```

A good package is one app primitive: contacts, notes, tasks, articles,
invoices. Small enough to review, complete enough to install and use.

## The Manifest

`dbbasic-package.json` is plain JSON:

```json
{
  "id": "contacts",
  "name": "Contacts",
  "version": "0.1.0",
  "description": "Contact records with a browse/edit page",
  "compatibility": {"dbbasic_object_server": ">=0.1.0"},
  "dependencies": [],
  "objects": [
    {"id": "contacts_directory", "path": "objects/contacts/directory.py"}
  ],
  "schemas": [
    {"collection": "contacts", "path": "schemas/contacts.json"}
  ],
  "seed": [
    {"collection": "contacts", "path": "seed/contacts.tsv"}
  ],
  "permissions": [],
  "migrations": [],
  "schedules": [
    {"id": "contacts_dedupe_nightly",
     "object_id": "contacts_directory",
     "schedule": "10 6 * * *",
     "description": "Flag likely duplicate contacts overnight."}
  ],
  "nav": [
    {"id": "contacts", "label": "Contacts", "path": "/contacts",
     "blurb": "People, the organizations they belong to, and interactions",
     "surface": "member", "group": "Work", "order": 60}
  ]
}
```

Rules the server enforces:

- `id` must be a safe package identifier; object ids and collection names are
  validated with the same rules as the rest of the server.
- All paths are package-relative. Absolute paths, null bytes, and `..`
  traversal are rejected at parse time.
- Unknown manifest sections are rejected rather than ignored.

## What Install Actually Does Today

Installs are deliberately conservative:

- `objects` entries are written under the configured objects root. Existing
  objects require `allow_replace`.
- `schemas` entries are validated and written under `data/schemas/`. Existing
  schemas require `allow_replace`.
- `seed` TSV is **install-once**: it is written only when the target
  collection has no records file yet. On a reinstall or upgrade of a package
  whose collection already holds data, seeding is skipped (reported as
  `status: "skipped"`, `action: "skip"`) — it never overwrites live data, and
  it never blocks the install. This is what makes an in-place upgrade safe:
  reinstall with `allow_replace` to ship new object code, a migrated schema,
  and re-merged permissions while every existing record is preserved. Live
  records live outside the package, in `data/collections/<name>/records.tsv`
  keyed by collection, so an upgrade has no reason to touch them. (Evolving a
  schema across an upgrade — backfilling a newly added field on old rows — is
  the separate `migrations` story below, not yet implemented; adding a
  read-only/server-set field like `created_at` is safe because old rows simply
  read as empty for it.)
- `permissions` entries MERGE: each fragment file is
  `{"rules": [ ... ]}` using the same rule shape as the policy document
  (`effect`, `principal`, `actions`, optional `collection`/`object_id`,
  row filters, field lists). On install the rules are validated, stamped
  with `"package": "<package_id>"` provenance, and appended to the
  deployment policy — skipping any rule that already exists, so reinstalls
  are idempotent. This is how a site package makes its own pages public:
  ship the grant with the code instead of hand-editing policy after install.
  Invalid fragments block the whole install.
- `migrations` are accepted in the manifest and reported in dry-runs, but
  install rejects them until explicit run semantics land.
- `schedules` become `task_<id>` rows in the scheduler trigger's state,
  which is where `object_daemon.process_scheduler` reads the board from.
  **If your app needs a recurring pass, declare it here.** A schedule that
  exists only in a running server's state is invisible to review, absent
  from a fresh install, and gone the next time the box is rebuilt — that
  is precisely how a demo server ended up with four nightly passes that
  appeared nowhere in this repository. Fields: `id` (becomes the state
  key, `[a-z][a-z0-9_]*`), `object_id`, `schedule`, and optional `type`
  (`cron` — the default — or `onetime`), `method` (default `POST`),
  `payload`, `description`.

  Three properties make declaring a schedule safe to do repeatedly:

  - **Run history survives.** `last_run`, `run_count` and `next_run`
    belong to the daemon and are never reset by an install. An upgrade
    that forgot when a pass last worked would make "is this actually
    running?" unanswerable.
  - **A pause is honoured.** The package declares what *should* run; an
    operator decides what *does* right now. A reinstall never restarts a
    task somebody deliberately paused.
  - **A changed expression reschedules.** Editing the cron clears
    `next_run` so the daemon recomputes it; leaving it alone keeps
    tonight's firing exactly where it was, so shipping a patch does not
    skip a run.

  Two things are refused outright, at install time rather than at 3am:
  an unparseable cron (the daemon reads that as "no next run" and moves
  on in silence — a pass that never runs and never complains), and a
  schedule aimed at an object neither this package nor the server
  provides. Removing a schedule from a manifest does **not** delete the
  task, matching the rest of install: deregistering never destroys.
- `nav` entries become rows in the `nav_entries` collection, which every
  navigation surface folds over. **If your app serves a page, declare its
  door here.** See the section below.
- The HTTP install route creates a restore point first and appends changelog
  rows under `data/package_changes/{package_id}/changes.jsonl`.

## Navigation (`nav`)

A menu maintained by hand rots the moment somebody ships without editing
it, and the rot is invisible — nothing fails, the front door just
advertises a server that no longer exists. This repository had three such
lists of the same apps: the app switcher's JS array in `site_nav` (25
entries), the home page's tile grid in `site_home` (21), and a
collection-to-URL map for search hits. They disagreed with each other and
between them named none of the eight newest apps. Worse, `site_home`
existed in no package at all — it lived only on a running server, so a
rebuilt box came back with no front door and said nothing about it, which
is precisely the failure the `schedules` section was built to end for
cron.

So a door is declared by the app that owns it, and every navigation
surface becomes a fold over one registry:

```json
"nav": [
  {"id": "shop", "label": "Shop", "path": "/shop",
   "blurb": "Browse what is for sale and buy it",
   "surface": "public", "group": "Commerce", "order": 400}
]
```

- `id` becomes the `nav_entries` record id, so `[a-z][a-z0-9_]*`. It must
  be unique across every installed package: two packages writing one row
  is two packages fighting over one door.
- `label` and `path` are required. `path` starts with `/` and holds no
  whitespace.
- `blurb` is one honest line in the present tense saying what the app
  does — it is rendered under the label on the home grid. No marketing.
- `surface` is `public` | `member` | `operator` | `hidden`, default
  `member`. This governs the MENU and is not access control: permissions
  decide who may open a page, and leaving a door off the menu hides
  nothing that was not already protected. `/shop` and the public
  product/pay/portal pages are `public`; ordinary app pages are `member`;
  `/scheduler`, `/flow`, `/urls`, `/dashboard` and other admin or debug
  pages are `operator`.
- `group` is free text, default `Apps`. The house groups are **Work**,
  **Publishing**, **Money**, **Commerce**, **Warehouse** and **System**.
- `order` is an integer, default `100`. Packages band it by group (Work
  in the 0s, Publishing the 200s, Money the 300s, Commerce the 400s,
  Warehouse the 500s, System the 600s) because a group sorts where its
  earliest entry sorts — that way the reading order of the groups falls
  out of the same number, instead of a seventh list holding it.

Three properties make declaring a door safe to do repeatedly:

- **The operator's `hidden` survives.** `operator_hidden` is a separate
  boolean column and an install never writes it on an existing row. The
  package says what the app IS; the operator says what they want to SEE.
  An upgrade that silently put back a page somebody deliberately removed
  would be the same class of incident as one that restarts a paused
  nightly pass — so an operator flips `operator_hidden` (a `role:manager`
  update on `nav_entries`) instead of editing somebody else's manifest.
- **Unchanged means unwritten.** Every package-owned field — label, path,
  blurb, group, surface, order — is compared before anything is written,
  so a plan that reports `unchanged` is one where nothing at all is about
  to happen. A dry run that under-reports is worse than no dry run.
- **Removing an entry does not delete the row.** Same as the rest of
  install: deregistering never destroys.

Two things are refused, and one deliberately is not:

- A **duplicate id inside one manifest**, and an **invalid surface, path
  or order**, fail at parse time.
- A **collision with another package** blocks the install: an id already
  registered by a different package, or a path a different package
  already claims. Whichever installed last would otherwise silently win,
  and the loser's menu entry would point somewhere its own package never
  chose.
- Whether a path is actually **served** is NOT checked. Routing has three
  sources (convention, `site_routes` records, `views` records) and two of
  them are data the same install may be about to seed, so resolving a
  route here would produce false blockers. The registry records the door
  the package intends; `/urls` is where you go to see what the server
  actually answers.

If the `nav_entries` collection does not exist yet (app-nav is not
installed), nav entries are reported as `skipped` with a reason and the
install proceeds. A package must never fail because the navigation app is
absent. The corollary is an ordering note for a fresh box: **install
`app-nav` first**, or reinstall afterwards, since entries declared before
the registry exists are skipped rather than queued.

### The opt-out

`tests/test_package_nav.py` asserts that every package shipping a `site_*`
object or a `site_routes` seed declares at least one nav entry. That test
is the thing that stops the drift coming back. Some packages legitimately
serve a URL that is not a door — a mounted widget (`site_thread`,
`site_share`), a fragment inside another page
(`site_materialize_run_button`), a POST endpoint (`site_timer_actions`),
or a per-record document reached from the record itself
(`site_packing_slip`, `site_receiving_sheet`, `site_return_form`). Those
declare:

```json
"nav_optional": true
```

which makes "this app has no front page" a reviewable claim in the
manifest rather than an omission nobody notices.

## Authoring Workflow

1. Create the package directory and manifest.
2. Write the objects (see [`object-authoring.md`](object-authoring.md) for method shape, state, logs,
   `request["_identity"]`, and HTML form patterns).
3. Write each schema JSON (`{"fields": [{"name": "id"}, ...]}` — see the
   schemas section of [`http-api-contract.md`](http-api-contract.md) for validation and field
   permission options).
4. Seed TSV: first row is the header, tab-separated, and must include `id`.
5. Dry-run before any install:

   ```http
   GET /packages/{package_id}?dry_run=true
   Authorization: Token <token>
   ```

   The plan reports what would be created, replaced, merged, applied, or
   skipped, with warnings. Nothing is written.
6. Install (requires `DBBASIC_ENABLE_PACKAGE_INSTALLS=true` plus the admin
   gate):

   ```http
   POST /packages/{package_id}/install
   Authorization: Token <token>
   ```

7. Verify: run the objects, read the records, check
   `GET /packages/{package_id}/changes` for the install changelog, and use
   `POST /packages/{package_id}/restore` if the install needs to be rolled
   back (requires `DBBASIC_ENABLE_PACKAGE_RESTORE=true`).

## A Minimal Working Example

`packages/hello-world/` in this repository is the smallest complete package.
`packages/admin-write-probe/` is the smallest complete *app-shaped* package:
one HTML page object, one schema, one seed file, exercising records end to
end. `packages/app-projects/` and `packages/app-notes/` are the reference
*user apps*: an owner-scoped schema with `search` metadata, permission
rules granting signed-in users their own rows (`row_filter` on
`$user_id`), and a signed-in page that reads and writes records with the
visitor's session cookie. Start a new app package from `app-notes`.

`app-notes` also shows the public/private pattern: a boolean `is_public`
field plus a second permission rule (`principal: public`, `row_filter:
{"is_public": "true"}`) makes shared records readable by anyone across
every surface — pages, records API, MCP, and search — with no
visibility code in the app itself. Its permalink page (`site_note_view`)
expects a `site_routes` record mapping `/notes/{note_id:uuid}` to it;
route records are site data, so each deployment adds them next to its
other routes.

Object files inside packages are normal DBBASIC objects. A package page that
renders per-user content reads `request["_identity"]`; a package form posts to
its own object id and writes records through the object runtime. Signed-in
browser users carry their session cookie automatically.

## Rules For Generated Packages

- Never put secrets, tokens, real hostnames, or private paths in package
  content — packages are meant to be shareable source.
- Keep one package per app primitive; use `dependencies` to declare (not yet
  resolve) cross-package needs.
- Bump `version` on any content change so install changelogs and restore
  points stay meaningful.
- Prefer seed data that demonstrates the schema without pretending to be real
  data.
- Dry-run output is the review artifact: a package that cannot explain itself
  in a dry-run plan is not ready to install.
