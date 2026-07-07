# Why DBBASIC — The Advantages, Honestly Stated

DBBASIC is a small server with strong opinions. This page states the
advantages plainly, and the boundaries next to them, because a claim
without its boundary is marketing.

## Change is live

Objects load per execution: edit an object and the next request serves
the new code. Schemas, permission rules, routes, and site pages are
data, changed over HTTP. Only changes to the server file itself need a
restart (seconds). There is no build, no deploy pipeline, no migration
ceremony.

Safety comes from **reversibility instead of ceremony**: object source
is versioned with rollback, schemas keep history, records keep
changelogs, permission changes and admin actions are audited. Ship in
steps of one; undo in steps of one.

## Apps are data

An application is a package: schema + permission rules + optionally one
page object ([the app suite](app-packages.md)). The server grows only
when a genuinely new *kind* of capability appears — search, sharing,
file storage, AI — and each lands once, generically, then every app
uses it by declaring so in its schema.

The consequence: **capability grows with data, not code.** Add a
schema for any obscure domain and it immediately has validated writes,
generated forms, search, sharing, MCP tools, and AI conversation about
it. Nobody ships function-calling for your beekeeping records; here it
is free.

## One permission engine, every surface

Browser pages, the records API, MCP agents, global search, file
downloads, and AI tool calls all pass the same policy: role rules, row
filters (`$user_id`, `$accessible_projects`, `$owned_projects`), field
redaction, audit. A rule written once binds everywhere — no surface can
leak what another surface hides. Sharing is records (`project_access`
rows), so granting and revoking are audited record writes.

## Fast because it does less

A request here is: a dict-router match, a TSV read the OS page cache
already holds, permission string-checks, JSON out. No ORM, no
connection pool, no query planner, no middleware stack, no signal
cascade. At personal-and-team data sizes (thousands of rows per
collection), scanning a hot file in-process beats an indexed query
across a socket.

**Boundary:** this inverts somewhere past hundreds of thousands of rows
in one collection. Collections are per-file, so a hot one can gain an
index later without changing the platform; the search contract promises
no ranking, so an index can slot in behind it.

## AI-native without AI lock-in

The whole admin surface is MCP; agents operate the server with their
own identities and audited sessions. Users store their own provider
keys (write-only), pick their own models, and hand the AI a chosen
subset of tools ([the shell](shell-and-ai.md)). The server never meters
your tokens or requires a vendor. An AI acting for a user is exactly as
powerful as the user — no more.

## Yours

Stdlib-only Python plus uvicorn; one VM; data in human-readable TSV and
JSON files you can grep, back up, and take elsewhere. Small enough for
one person to read. MIT licensed, with the operator console
([Scroll](https://github.com/askrobots/dbbasic-scroll)) open source
too. Multi-domain hosting means one instance serves many sites with one
identity store and one audit trail.

## Current boundaries, plainly

- One VM; no cluster correctness claims.
- Realtime is polling today; websocket push is the next platform slice.
- No background-job runtime yet (PDF extraction, thumbnails, scheduled
  AI work wait on it).
- Untrusted arbitrary code execution is not offered; object writes are
  operator/admin actions.
- Open signup and password reset flows are not built; identities are
  operator-created today.

The direction on each is recorded in [status](status.md). The rule for
new capability is unchanged: it must pass the same permission engine,
audit trail, and reversibility bar as everything before it.
