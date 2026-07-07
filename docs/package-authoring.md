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
  "migrations": []
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
- `seed` TSV is written only when the target collection records file does not
  already exist — seed never overwrites live data.
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
- The HTTP install route creates a restore point first and appends changelog
  rows under `data/package_changes/{package_id}/changes.jsonl`.

## Authoring Workflow

1. Create the package directory and manifest.
2. Write the objects (see `object-authoring.md` for method shape, state, logs,
   `request["_identity"]`, and HTML form patterns).
3. Write each schema JSON (`{"fields": [{"name": "id"}, ...]}` — see the
   schemas section of `http-api-contract.md` for validation and field
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
