# Status

DBBASIC Object Server is ready for controlled staging and internal app
dogfooding. It is not ready to expose arbitrary code execution to untrusted
public users.

## Usable Now

- Single-VM ASGI server behind a reverse proxy
- Python object execution for operator-controlled objects
- Source reads, gated source writes, source versions, and rollback
- Object state, logs, log rotation/compression, files readback, and metadata
- TSV-backed collection records with UUIDv4 IDs by default
- Schema metadata, validation, field permissions, schema history, and rollback
- Collection record change history
- Event publishing, event retention, subscriptions, and callback delivery state
- Package manifest discovery, dry-runs, gated installs, install changelogs,
  restore points, and restore API
- File-backed accounts, users, sessions, and permission subjects
- Permission policy storage, check API, audit mode, readiness status, row
  filters, field redaction, opt-in enforcement, and readiness-gated rollout
- Request size limits, request concurrency limits, execution concurrency limits,
  rate limits, wall-clock timeout path, and health/capacity metrics
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
- Default-on permissions for every route after login/auth gateway integration
- Browser login/session UX
- CPU and memory isolation for untrusted object code
- File upload/delete from untrusted users
- Fully managed scheduler/queue/admin dashboard API
- One-command installer
- Cluster correctness claims

## Next Work

1. Connect sessions to a real login or trusted auth gateway flow.
2. Make permission enforcement default-on after the login/auth gateway is wired.
3. Expose scheduler, queue, job, and daemon status through HTTP.
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
