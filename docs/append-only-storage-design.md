# Append-Only Storage — Design Decisions

Decisions for the next storage phase: making collection writes O(1) while
keeping the system's defining constraint intact. Written before
implementation, like docs/event-hooks-decisions.md, so the direction is on
record with its reasoning.

## The Invariant (decided first, everything else follows)

**A collection either IS one `records.tsv` file, or can deterministically
emit one.** The single TSV is the source of truth: complete on its own,
greppable, diffable, backed up with `cp`. Anything clever — indexes, caches,
offsets — is a *sidecar*: derived, disposable, and rebuildable from the TSV
alone. No storage feature may ever be added that cannot be flattened back to
the single file. (This also binds any future alternate backend: its contract
includes materializing the canonical TSV on demand.)

## Why Change Anything

Measured (2026-07-12, M1, local SSD, after the read-cache work): `create` and
`update` rewrite the whole file per record — 10.5s at 1M rows, 2.5min at 5M.
Reads scale fine (O(1) indexed `get` holds at millions of rows); writes are
the wall. Motivating workloads: game-server state (write-hot large
collections), ad-exchange impression logs, price history — all append-shaped.

## The Design

Append-only *within the single file*:

- **Create** appends one row. O(1).
- **Update** appends a superseding row with the same id. Reads resolve
  **last-wins** per id. (Note: today's duplicate-id behavior is
  first-occurrence-wins; this flips deliberately and must be called out in
  the change.)
- **Delete** appends a tombstone row (representation decided at
  implementation — a reserved marker that cannot collide with user data and
  survives the csv layer; it must be visible and obvious in a raw `cat`).
- **Compaction** is the only remaining whole-file rewrite: collapse to live
  rows via the existing atomic temp+rename, run on a schedule or when the
  superseded-row ratio passes a threshold — never inline in a request. Until
  compaction, the file contains its own row history: grep shows every version
  a record went through. That is a feature (an inspectable audit trail), not
  merely tolerated.
- **New-column handling**: today a new field triggers a full-file header
  rewrite. Append-only tolerates rows shorter than the current header (they
  read as empty — the semantics that already exist), so a header change can
  ride the next compaction rather than forcing an immediate rewrite.

## Sidecars

- **id → byte-offset index** file per collection: maps each id to the offset
  of its *latest* row. Enables point reads by seek without parsing the file.
  Derived and disposable — rebuild is one sequential scan. Never required for
  correctness; readers without the sidecar fall back to scan.
- **The in-process read cache** (already shipped) composes into incremental
  reads: cache entries remember the byte offset they consumed; when a stat
  signature changes by growth alone, parse only the tail delta — O(new rows),
  not O(file). This dissolves the "big hot collection vs. cache eviction"
  tension for write-hot workloads.
- Cache-comment revision required at implementation: the current
  `_RECORDS_CACHE` reasoning leans on every write producing a fresh inode via
  atomic rename. Appends mutate in place (same inode; mtime/size change).
  Signature checks still detect change, but the comment's argument must be
  rewritten for the append case, including the torn-tail rule below.

## Crash Safety

Rename-atomicity is replaced (for appends) by the classic log rule: a torn
final line — no trailing newline, or a row that fails to parse at EOF — is
ignored by readers and truncated/overwritten by the next writer. Appends are
flushed per write; fsync policy is a deliberate knob (default: flush, no
fsync per row — same durability class as today's writes; an env for
fsync-per-append where a workload wants it). Compaction keeps full
temp+rename atomicity.

## Concurrency

Writers still serialize under the existing per-collection file lock — but the
critical section shrinks from "rewrite everything" to "append one line," so
effective write throughput rises by orders of magnitude without new
machinery. Readers remain lock-free; the stat-signature cache covers
cross-process coherence, as today.

## What This Buys, Honestly

- The measured interactive ceiling moves from tens of thousands of rows to
  **tens of millions** for ordinary use, and far beyond for append-heavy,
  read-by-id workloads (a 15GB file appends in microseconds and point-reads
  by seek).
- What stays hard: rich secondary queries (by customer, by date range, by
  status) still scan unless a sidecar index for that field exists. Secondary
  sidecar indexes are future work, same rules: derived, disposable.
- What is out of scope on purpose: cross-collection transactions,
  replication, multi-writer scaling. Single VM, one honest file, is still the
  product.

## Migration and Compatibility

Opt-in per collection (schema flag or storage marker), default unchanged —
existing collections keep exact current behavior until switched. A compacted
append-only file is byte-compatible with a classic one (header + unique
rows), so switching a collection back is: compact, remove the marker.
Package installs, backups, and restore already treat `records.tsv` as opaque
content and keep working; the backup CLI gains nothing to learn (the sidecar
index is rebuildable, so backups may ignore it entirely).

## The Escape Hatch (documented now, built never — until real need)

If a collection someday truly outgrows one file (billions of rows, heavy
secondary queries), the answer is a **pluggable per-collection backend**
under the identical schema/permission/API surface — SQLite being the obvious
candidate (single file, stdlib, public domain). The invariant above still
binds it: such a backend must emit the canonical `records.tsv` on demand.
This paragraph exists so nobody ever "improves" the TSV engine into a
half-database; past the envelope, graft a real one underneath instead.

## Build Order

1. Tail-delta read cache (offset-tracking) — useful even before append-only
   lands, zero format change.
2. Append-only create/update/delete + torn-tail recovery + last-wins reads,
   behind the per-collection opt-in.
3. Compaction (manual/admin trigger first, then scheduled via the daemon).
4. id→offset sidecar for seek-based point reads on large collections.
5. Secondary-field sidecars — only when a named workload demands one.
