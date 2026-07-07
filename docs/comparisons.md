# Comparisons — What DBBASIC Deletes, and What That Costs

Rails announced itself by giving developers an enemy: enterprise
ceremony. It was right, and it won. Twenty years later the ceremony
grew back in new clothes — deploy pipelines for one-line changes,
migration rituals, SaaS rent on your own data, and metered AI where
every question pays a vendor.

This page compares honestly. Every framework here is good software
built by serious people; the differences are about what each one makes
you carry. Where DBBASIC deletes something, the cost of the deletion is
stated too. Claims about speed and effort come with receipts from this
repository's own history.

## The one-line version of each

| Stack | What you carry | What DBBASIC deletes | What deleting costs |
|---|---|---|---|
| Django / Rails | ORM, migrations, deploys, middleware, signals, app server + DB | All of it: records are files, schemas are live data, changes serve on the next request | No relational joins; big-data queries need indexes you don't have yet |
| Enterprise / k8s | Clusters, YAML, service meshes, CI/CD, staging ladders | One VM, one process, systemd | No horizontal scaling story; deliberate single-box boundary |
| JS meta-frameworks | Build steps, bundlers, hydration, node_modules | Pages are one Python file emitting HTML + small fetch calls | No component ecosystem; you write your own widgets or generate them from schemas |
| SaaS suites (Basecamp, Notion, Airtable) | Monthly rent, their servers, their data model, their API limits | Self-hosted; your disk, your TSVs, greppable | You are the operator: backups and TLS are your (automated) chores |
| No-code platforms | A ceiling: when the builder can't express it, you're stuck | Schemas generate the easy 90%; real Python objects catch the rest | Objects are code; someone (or some AI) still writes them |
| Metered AI platforms | Per-seat AI pricing, vendor lock, their tools only | Bring your own key, pick any model, hand it your tools, audited as you | You pay your provider directly; no bundled "free" AI |

## Against the ceremony, specifically

**Migrations.** Django asks: edit models.py, generate a migration,
review it, run it in each environment, in order, forever. DBBASIC asks:
PUT the schema; it versions itself; rollback is one call. The cost:
no schema-enforced relational integrity beyond `relation` existence
checks — the engine validates pointers, not cascades. That is a
[stated design position](schema-forms.md), not an accident.

**Deploys.** The receipt: during one working day this repository
shipped twelve application packages, an AI runtime, file storage, and
a sharing model to a live server — the home page changed four times,
one page object shipped five times, and the only restarts were for the
core server file, each a few seconds. Every change was versioned and
reversible. The equivalent Django day is spent watching pipelines.

**Middleware and signals.** A q9-scale Django app ran six middleware
and hundreds of lines of signal handlers on every write. Here a
request is a router match, a cached file read, permission string
checks, JSON out. That is most of why a small Python file server
[outruns](why-dbbasic.md) a "more powerful" stack: the fastest layer
is the one that is not there.

**"But will it scale?"** Rails answered this correctly in 2005 and we
repeat it: optimize for the loop first. DBBASIC adds a number — TSV
scans in page cache are instant to roughly hundreds of thousands of
rows per collection, which is beyond most personal and team software,
and per-collection files mean a hot collection can gain an index later
without a platform rewrite.

## What Rails-era slogans become here

- *Convention over configuration* → **Schemas declare semantics,
  never widgets.** One declaration drives forms, tables, validation,
  search, AI tools, and permissions on every surface.
- *Don't repeat yourself* → **One permission engine, every surface.**
  A row filter written once binds the web page, the API, search,
  files, MCP agents, and AI tool calls identically.
- *Less software* → **Apps are data.** Eleven of twelve suite apps
  contain zero server code; several contain no code at all.
- *Optimize for programmer happiness* → **Optimize for the loop.**
  Edit → live → inspect → undo, in seconds, with history. For humans
  and for AI agents, which compounds it: the machine that writes the
  code is also freed from the ceremony.
- *Opinionated software* → unchanged, and still true. If you want
  microservices, joins, or a component framework, this is honestly not
  your server.

## The new argument Rails could not make

Rails freed the developer from ceremony. DBBASIC also frees the
*software* from rent. A person with a small VM gets the suite, the
data in files they can read, an AI operator that uses their own key
and obeys their own permissions, and an MCP surface any agent can
drive — MIT licensed, small enough to read end to end. The enemy is
not another framework. It is the idea that your own tools should bill
you monthly to change.
