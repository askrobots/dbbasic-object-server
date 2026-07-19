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
  (returns rows/bytes before and after). CLI/HTTP surfaces may wrap this
  later.
- Automatically: when a read observes more dead rows than live ones and
  the file exceeds `DBBASIC_APPEND_COMPACT_MIN_ROWS` (default 10000)
  physical rows, the **next write** compacts instead of appending. Reads
  never rewrite anything.

### Crash behavior

A write interrupted mid-row leaves a torn final line. Readers ignore it;
the next writer truncates it away before appending. Compaction and classic
mode keep full temp+rename atomicity.

### Current limits (honest)

- Collections larger than `DBBASIC_RECORDS_CACHE_MAX_ROWS` (default
  500k) fall out of the in-process cache, so append-mode writes above
  that size pay a full fold-parse per write (~2.7s at 1M) until the
  planned id→offset sidecar lands (design doc, item 4). At or below the
  threshold, writes are milliseconds.
- Cold reads of an append file cost ~1.6x a classic parse (fold
  overhead). Compaction restores parity.
- Backup **preview/diff** does not yet interpret `_op`: for append-mode
  collections it may show `_op` as a field and count a tombstoned record
  as live. Backup/restore of the file itself is unaffected (the file is
  opaque bytes to backup). Known follow-up.

## Choosing

Classic for ordinary app data (the default is correct). Append for
write-hot or log-shaped collections: event logs, impressions, telemetry,
game/world state, anything updated far more often than it is read cold.
When in doubt, stay classic — switching later is one schema edit.
