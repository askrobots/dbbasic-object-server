# Status

DBBASIC Object Server is ready for controlled staging, internal app
dogfooding, and invited-user apps behind browser login with permission
enforcement on. It is not ready to expose arbitrary code execution to
untrusted public users.

## Usable Now

- Single-VM ASGI server behind a reverse proxy
- Python object execution for operator-controlled objects
- Source reads, gated source writes, source versions, rollback, and source
  change history
- Object state, logs, log rotation/compression, files readback, and metadata
- TSV-backed collection records with UUIDv4 IDs by default
- Schema metadata, validation, field permissions, schema history, and rollback
- Collection record change history
- Event publishing, event retention, subscriptions, and callback delivery state
- Package manifest discovery, dry-runs, gated installs, install changelogs,
  restore points, and restore API
- A small installable `system-dashboard` package for public staging visibility
- A small installable `admin-write-probe` package for testing object state writes
  and admin-token-gated collection record writes on a narrow public route
- File-backed accounts, users, self-service session inspection/revocation, and
  permission subjects
- Password credentials (scrypt, stored outside user records), admin password
  set/reset routes, opt-in password login, session cookies with origin checks,
  a built-in browser `/login` + `/logout` flow with per-identifier login
  lockout, request identity injected into
  object execution (`request["_identity"]`), and a Django-style shell CLI for
  bootstrap and user management
- Permission policy storage, check API, audit mode, readiness status, row
  filters, field redaction, opt-in enforcement, and rollout gates for recovery,
  identity, and policy safety
- A documented starter policy (`object_permission_store.starter_policy_payload`)
  and a completed audit-then-enforce rollout on public staging: enforcement is
  ON at object.dbbasic.com with public grants for the public pages and probe
  reads, registered-user grants for object execution and probe writes, and
  admin-role bypass
- Request size limits, request concurrency limits, execution concurrency limits,
  rate limits, wall-clock timeout path, and health/capacity metrics
- Token-gated admin status with detailed health, inventory, capability flags,
  package posture, and permission readiness for Scroll/operator dashboards
- Token-gated admin object inspection for Scroll/operator source, state, logs,
  versions, metadata, files, and source-change views without exposing broad
  `/objects` execution routes through the reverse proxy
- Token-gated admin file inventory, download, and gated write routes for
  Scroll/operator file views; upload/delete remain deployment-disabled unless
  `DBBASIC_ENABLE_FILE_WRITES=true`
- Token-gated admin collection and schema inspection for Scroll/operator
  collection, record, changelog, schema, and schema-version views without
  exposing broad write-capable `/collections*` or `/schemas*` routes
- Token-gated admin record create/update/delete and schema replace/rollback
  aliases so Scroll/operator screens can run the data loop (define schema,
  write records, validate, audit, emit events) through the narrow admin surface
- Token-gated daemon status with read-only scheduler, queue, event delivery,
  retention, and cleanup posture for Scroll/operator dashboards
- Token-gated scheduler and queue control APIs for trusted operator screens,
  using the same daemon-compatible TSV state
- An MCP endpoint (`POST /api/mcp`, JSON-RPC 2.0) so AI agents can run the
  full object and data loops through the same gated admin surface, with
  per-agent session identity in the audit trail
- Opt-in site routing (`DBBASIC_ENABLE_SITE_ROUTES`) for clean website URLs:
  convention (`/about` -> `site_about`), a `site_routes` records table with
  `{param}`/`{param:uuid}` patterns, and `site_404`, all resolving through
  the policy-enforced execution path
- Multi-domain hosting on one server: a `site_hosts` records table maps each
  domain to its own object prefix, home, and 404, with host-scoped route
  patterns, so many websites share one runtime, identity store, and audit
  trail. Be aware: users and credentials are ONE shared store across all
  domains on an instance — one login works everywhere, and per-site member
  separation is done with accounts and policy grants, not separate user
  tables. Domains needing fully separate identity belong on their own server
  instance.
- Schema-driven global search (`GET /api/search` + the `global_search` MCP
  tool): collections opt in with `search.fields`, and results respect
  permission row filters and field redaction per caller
- A twelve-package application suite (projects with self-serve sharing
  grants, notes, tasks with an enforced status lifecycle, contacts,
  articles, links, events, files, templates, timers, the shell, and the
  collaboration layer of comments/feed/notifications) — every app is
  schema + permission rules + at most one page object, with no
  app-specific server code (see `app-packages.md`)
- Per-user AI: write-only provider key storage, model choice per request,
  and `POST /api/ai/chat` — an AI turn that can call a caller-chosen
  subset of the MCP tools with the caller's own credentials, so an AI
  acting for a user is never more powerful than the user
  (see `shell-and-ai.md`)
- User file storage with per-user disk quotas, where downloads are
  authorized against each file's metadata record — owner rows, public
  links, and project sharing govern files like any other record
- Runtime backups, restore helpers, deployment checks, GitHub Actions tests, and
  a working public staging deployment shape

## Good Fit Today

- Local development
- Private or internal apps
- Public pages backed by reviewed objects
- Controlled staging on one VM
- Scroll/API integration work
- Migrating small Django/PostgreSQL data into object records and schemas
- Package authoring and install dry-runs

## Not Ready Yet

- Open signup where strangers can run arbitrary Python code
- Public source writes
- Enforcement-on as the shipped default (it is opt-in per deployment; staging
  has it on)
- Policy checks for the admin-token-only surfaces (identity, permissions,
  schemas, daemon, events, packages stay admin-gated by design)
- Self-service signup and password reset flows
- Session admin gates are implemented, but opt-in with
  `DBBASIC_ENABLE_SESSION_ADMIN_GATES=true`
- CPU and memory isolation for untrusted object code
- Fully managed event delivery/admin control API
- Realtime push (websockets/SSE) — polling works today; push is the next
  platform slice
- A background-job runtime (long media transcodes, PDF text extraction,
  thumbnails, scheduled AI work wait on it)
- Cluster correctness claims

## Next Work

The application suite is ported (see `app-packages.md`) and the single-VM
installer and quickstart exist (`scripts/install.sh`, `quickstart.md`).
The remaining work is platform capability, in rough priority order:

1. Realtime push over websockets (uvicorn already speaks the ASGI
   websocket protocol): authenticated connect, permission-checked
   subscriptions to record-change/ops/feed streams, and push on writes.
   Turns today's polling surfaces (dashboard, shell, coordination feed,
   notifications) into live streams.
2. Write-level project sharing (`$writable_projects`) and a per-family
   "builder role" so agents and collaborators can be scoped below admin.
3. A background-job runtime: submit → job record → worker object → result
   record + notification. Unlocks long media work, PDF extraction,
   thumbnails, and scheduled AI summarization.
4. Add CPU/memory isolation and a better worker boundary for untrusted code.
5. Self-service signup and password reset flows.
6. The remaining q9 apps that need infrastructure: messaging/email
   (mail server), finance/catalog (large but no infra blocker), billing.

## Release Rule

The first production release should be honest about the boundary: one VM,
operator-controlled objects, server-enforced permissions, backups, logs,
versions, packages, and Scroll integration. Distributed cluster behavior and
public untrusted code execution should stay explicitly experimental until they
have separate hardening and tests.
