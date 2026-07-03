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
  a built-in browser `/login` + `/logout` flow, request identity injected into
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
- Self-service signup, password reset flows, and login attempt lockout
- Session admin gates are implemented, but opt-in with
  `DBBASIC_ENABLE_SESSION_ADMIN_GATES=true`
- CPU and memory isolation for untrusted object code
- File upload/delete from untrusted users
- Fully managed event delivery/admin control API
- One-command installer
- Cluster correctness claims

## Next Work

1. Retire the `dbbasic_probe` Caddy exceptions now that Scroll writes through
   admin aliases, or open runtime object routes more broadly under
   enforcement.
2. Enable session admin gates on staging so Scroll can operate on an
   admin-role session instead of the raw deployment token.
3. Add event delivery controls after scheduler and queue controls stabilize.
4. Add file upload/delete with quotas, content checks, permissions, and audit.
5. Add CPU/memory isolation and a better worker boundary for untrusted code.
6. Wire Scroll to the public identity, permissions, package, event, backup, and
   status APIs.
7. Create the repeatable single-VM installer.

## Release Rule

The first production release should be honest about the boundary: one VM,
operator-controlled objects, server-enforced permissions, backups, logs,
versions, packages, and Scroll integration. Distributed cluster behavior and
public untrusted code execution should stay explicitly experimental until they
have separate hardening and tests.
