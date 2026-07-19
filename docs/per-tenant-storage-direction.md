# Per-Tenant Storage — The SaaS Decomposition

A direction note, not a build plan. Recorded alongside
docs/append-only-storage-design.md because the two together answer "how far
can one TSV go, and what happens at SaaS scale" — and the answer to the
second is not "a bigger table."

## The Observation

The measured storage envelope (docs/append-only-storage-design.md: tens of
thousands of rows interactive today, tens of millions after append-only)
looks like a limit under the one-big-table model. Per-tenant storage inverts
it: a SaaS with 100,000 customers and "a billion orders" does not have a
billion-row problem — it has 100,000 ten-thousand-row problems, each of
which sits in the comfortable center of the measured envelope. The giant
commingled table is an artifact of shared-database design, not a law of
SaaS.

## What Decomposition Buys

- **Physical isolation.** Row-level security (`WHERE tenant_id = ?`, one
  missing predicate from a breach) becomes filesystem separation. The
  permission engine's row filters harden into file boundaries.
- **GDPR and export for free.** Right-to-be-forgotten is removing the
  tenant's directory. "Export my data" is handing over the tenant's own
  TSV files — human-readable, Excel-openable, no export pipeline.
- **Per-customer backup/restore already exists.** The scoped restore +
  read-only preview shipped this cycle applies per tenant with zero new
  code: "restore customer X's notes to yesterday" is the same operation.
- **Per-tenant migrations.** Package upgrades ride the provenance/reconcile
  system tenant-by-tenant: incremental rollout, conflicts parked per
  tenant, no all-customers-at-once schema locks.
- **Noisy neighbors decompose.** One tenant's huge collection cannot slow
  another's small one; the records-cache LRU bound doubles as per-tenant
  cache fairness.
- **Tenant mobility is file movement.** Rebalancing tenants across VMs,
  or graduating one outlier to its own instance or to the per-collection
  backend escape hatch, is `mv` of a directory. Shared-nothing per tenant;
  horizontal scale without a distributed database.
- **Metering is `du`.** Storage billing per tenant is a directory size.

## Honest Costs

- **Cross-tenant queries become scatter-gather.** Admin analytics ("orders
  today, all customers") need rollup collections maintained by the daemon
  (aggregates are just another collection), or a scan across tenant dirs.
  This is the standard sharding trade and should be designed as rollups,
  not ad-hoc scans.
- **File-count hygiene.** 100k tenants x N collections is ~1M files —
  fine for modern filesystems, but the directory layout should shard
  (e.g. tenants/ab/abcd.../collections/...) from day one.
- **The single-TSV invariant holds per collection per tenant** — and the
  whole-instance view ("all tenants' notes as one TSV") is itself just a
  deterministic concatenation with a tenant column, satisfying the
  emit-one-file rule at the aggregate level when ever needed.

## Prior Art (the industry is arriving here)

Cloudflare Durable Objects (SQLite per entity, storage co-located with
logic), Turso (database-per-tenant as a product), the SQLite-per-user
pattern generally. Per-tenant single-file storage next to per-tenant code
is the current direction of travel; this system's version is the flavor
you can `cat`.

Also the oldest advice in the flat-file lineage: Strozzi NoSQL's "Big
tables" doc opens with *"big tables should be avoided in the first place"* —
use the Unix filesystem to pre-organize relations and keep each one small.
Per-tenant decomposition is that doctrine, systematized.

## Current State and Gap

Today the platform is multi-tenant by *rows* (owner_id row filters, one
shared collection per name) and multi-site by *hosts* (site_hosts). The gap
this note names: collections themselves are instance-global. The eventual
shape is tenant-scoped collection roots under one instance — same schemas,
same permission engine, same generators, same API surface, with the tenant
resolved from identity the way hosts are resolved from requests today. Not
scheduled; recorded so the append-only and cache work keep it reachable
(nothing in either conflicts with tenant-scoped roots — both operate per
file).
