# Collection Storage Modes

A collection stores its records one of two ways. Both live in the same
single `records.tsv`, both are served by the same API, permissions, cache,
and generators, and both satisfy the invariant in
[`append-only-storage-design.md`](append-only-storage-design.md): the file
alone is the complete truth.

## Classic (the default)

Every write atomically rewrites the whole file (temp file + rename). Every
row appears exactly once. This is the right mode for what collections
usually are — hundreds to tens of thousands of records — and it is what
every collection uses unless its schema says otherwise. Nothing existing
changed modes; there is no migration.

- Write cost grows with collection size (measured: ~4ms at 1k rows,
  ~284ms at 100k, ~2.8s at 1M — per write).
- The file never contains history; `cat` shows current state only.

## Append (opt-in per collection)

Declare it in the schema:

```json
{ "name": "events_log", "storage": "append", "fields": [ ... ] }
```

The collection becomes a last-wins log inside the same file: create,
update, and delete each append one row instead of rewriting. An internal
first column `_op` marks ordinary rows (empty) and deletions (`del`) — it
is never visible in schemas, API responses, or returned records, and no
schema may declare a field named `_op`. Reads fold the log newest-wins;
list order matches classic mode exactly.

- Write cost is near-constant (measured: create ~4ms at 100k rows, ~34ms
  at 1M; update ~1–9ms — versus seconds in classic mode at that scale).
- Until compaction, `cat` shows the row's full history and deletions —
  an inspectable audit trail, by design.
- Mode transitions are just writes: adding `"storage": "append"` takes
  effect on the next write (one final rewrite adds `_op`); removing it
  compacts back to a classic file on the next write.

### Compaction (the VACUUM analog)

Superseded rows and tombstones accumulate until compaction folds the file
back to live rows only (atomic rewrite, under the writer lock):

- Manually: `object_records.compact_collection("name", base_dir=...)`
  (returns rows/bytes before and after).
- Automatically: when a read observes more dead rows than live ones and
  the file exceeds `DBBASIC_APPEND_COMPACT_MIN_ROWS` (default 10000)
  physical rows, the **next write** compacts instead of appending. Reads
  never rewrite anything.
- Via HTTP (admin-gated, same posture as other mutating admin record
  routes): `POST /admin/collections/{collection}/compact` runs
  `compact_collection` and returns its rows/bytes-before/after summary
  plus `duration_ms`. 404 for an unknown collection, 400 for a collection
  not currently in append storage mode. `GET /admin/storage` reports
  per-collection compaction observability -- `{"physical_rows",
  "live_rows", "dead_rows", "bloat_ratio", "file_bytes",
  "sidecar_present", "compaction_flagged"}` for every append-mode
  collection (`object_records.append_collection_stats` /
  `list_append_collection_stats`). This endpoint is cheap by
  construction: it never folds records.tsv to answer, preferring a warm
  in-process cache entry or the id->offset sidecar's own row count; a
  collection neither can answer comes back with `"estimated": true` and
  only `file_bytes` populated (`physical_rows`/`live_rows` null) rather
  than paying an O(file) parse on a status call.
- Via the daemon (object_daemon.py, optional -- nothing else depends on
  it running): `process_compactions` polls every
  `DBBASIC_COMPACTION_INTERVAL_SECONDS` (default 3600, tracked by a
  `.compaction_last_run` marker file under the data dir rather than an
  in-process timer, so the interval survives a daemon restart) and
  compacts any append-mode collection whose `bloat_ratio` is at or above
  `DBBASIC_COMPACTION_BLOAT_RATIO` (default 1.0 -- dead rows at least
  matching live rows) AND whose physical row count is at or above
  `DBBASIC_APPEND_COMPACT_MIN_ROWS`. A failure compacting one collection
  is logged and skipped; it never stops the rest of the pass.

### Crash behavior

A write interrupted mid-row leaves a torn final line. Readers ignore it;
the next writer truncates it away before appending. Compaction and classic
mode keep full temp+rename atomicity.

### Current limits (honest)

- Collections larger than `DBBASIC_RECORDS_CACHE_MAX_ROWS` (default
  500k) fall out of the in-process cache. Point operations (create's
  duplicate check, get, update, delete) no longer pay a full fold-parse
  above that size: an id→offset sidecar (design doc, item 4;
  `.records.oidx` next to `records.tsv`, derived and disposable) answers
  them by seek instead (measured: create/update ~1ms, get ~0.3ms at 1M
  rows, versus ~2.8s for a full fold-parse before the sidecar existed).
  The sidecar builds lazily on first use past the threshold (one
  sequential scan, ~2s at 1M rows) and self-heals from any inconsistency
  by rebuilding. **List and read-all-records stay unchanged** above the
  threshold: they still fold the whole file, since ordering and
  windowing need the full picture -- a secondary sidecar for that is
  future work (design doc, item 5). At or below the threshold, nothing
  changed: the in-process cache still serves everything, sidecar or not.
- At or below the threshold, a WARM cache entry for an append-mode
  collection that grows in place (another process, the CLI, or a sibling
  worker appending — same inode, size only) is caught up with a
  tail-delta fold of just the new rows instead of a full re-parse of the
  whole file (design doc, Sidecars: "when a stat signature changes by
  growth alone, parse only the tail delta"). Measured: reading a
  400,000-row collection right after an external +100-row append dropped
  from ~742–815ms (full re-fold every time, regardless of how small the
  growth was) to ~74–100ms (fold just the 100 new rows) — roughly 8x at
  this size, and the gap widens with collection size since the old cost
  was O(file) and the new one is O(delta). A delta containing any
  deletion falls back to a full fold (deletes shift every later row's
  position, which the cheap merge doesn't model); growth past the cache
  threshold is still evicted per the existing row-count rule above.
- Cold reads of an append file cost ~1.6x a classic parse (fold
  overhead). Compaction restores parity.
- Backup **preview/diff** interprets `_op`: for append-mode collections
  (backup side, live side, or both — a backup taken before a mode switch
  compares fine against a now-classic or now-append live file) it folds
  the log last-wins-by-id before diffing, so `_op` never appears as a
  field and a tombstoned id is treated as absent, not live. Backup/restore
  of the file itself is unaffected either way (the file is opaque bytes to
  backup).

## Choosing

Classic for ordinary app data (the default is correct). Append for
write-hot or log-shaped collections: event logs, impressions, telemetry,
game/world state, anything updated far more often than it is read cold.
When in doubt, stay classic — switching later is one schema edit.
