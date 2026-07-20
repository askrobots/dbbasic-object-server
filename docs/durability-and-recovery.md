# Durability & Recovery

How the object server keeps your data safe, and why you can still read it in
fifty years.

## The design principle: plain text is the source of truth

Every record lives in a tab-separated-values file on disk —
`data/collections/<name>/records.tsv` — that you can open with `cat`, `grep`,
a spreadsheet, or any programming language's CSV reader, on any operating
system, with no special software. There is no proprietary binary format your
data can be trapped inside.

Everything that makes the store *fast* — the id→offset index, the in-memory
read cache — is **derived from the TSV and rebuildable from it**. Delete every
index and cache and the database is byte-for-byte identical; you lose speed,
never a byte. The canonical data is exactly the set of `.tsv` files, and
nothing else is load-bearing.

> **Why it matters:** a binary database entombs your bytes in a format that
> needs *its own software* to read. Plain text means your data outlives the
> software that wrote it.

## Graceful degradation

Because the store is plain text, corruption **degrades gracefully**. A damaged
`.tsv` — or a truncated backup of one — can be opened in any editor and
salvaged by hand: at worst a single line is garbled; the file never becomes
"won't open." A corrupted binary database page, by contrast, can render the
whole database unreadable without vendor-specific recovery tools, and a torn
write can damage internal tree structure catastrophically. Plain text has no
structure to catastrophically damage.

## Crash safety

- **Classic collections** are rewritten with a temp-file-plus-atomic-rename.
  A reader always sees either the entire previous version or the entire new
  one — never a half-written file.
- **Append-only collections** (used for high-write logs) treat an interrupted
  write as a torn tail: only the last, incomplete entry is ignored on read;
  every fully-written entry before it survives.
- **Concurrency** is serialized with a kernel file lock (`flock`) that applies
  across both threads *and* separate processes, so concurrent writers on a
  multi-worker deployment cannot corrupt or lose each other's writes.

## Recovery by replay: backup + log

Alongside each collection, every mutation is also appended to an independent
**change log** — `record_changes/<collection>/changes.jsonl` — recording, for
each create/update/delete: a timestamp, the actor, and full *before* and
*after* snapshots of the record.

This gives you the classic "restore a backup and replay the log" recovery
model, in plain text:

- **Reconstruct a collection from its change log alone.** Replaying the log in
  chronological order — apply each `after` snapshot on create/update, drop the
  record on delete — reproduces the collection's exact current state. If a
  collection file is ever lost or damaged, the change log rebuilds it.
- **Point-in-time recovery.** Restore a plain-text backup, then replay the
  change log *forward* to a chosen timestamp.
- **The change log is itself crash-safe by construction.** It is line-based
  (JSON-per-line); a read skips any incomplete final line, so a crash
  mid-append costs only that one in-flight entry.

The result is **two independent plain-text records of every change** — the
collection file and the change log — either of which can reconstruct the
other. Your recovery, like your data, needs no special software: a short
script replays the log.

## How we test durability

Durability is verified by a dedicated conformance suite, run deliberately
(like a benchmark) rather than on every commit, covering:

- **Character-set fidelity** — ASCII and control bytes, multibyte UTF-8,
  emoji and grapheme clusters, adversarial Unicode, and arbitrary JSON, each
  round-tripped byte-exact through every storage and read path, with no silent
  normalization.
- **Crash recovery** — a write interrupted at *every byte offset* is simulated
  and the store is checked to recover to a valid state with no loss of
  committed data.
- **Concurrency** — real concurrent processes writing the same collection,
  checked for lost updates and corruption.
- **Atomicity** — single-record writes are all-or-nothing, and a reader never
  observes a torn intermediate.
- **Backup, restore, and compaction** — verified byte-exact, including exotic
  content, and crash-safe (an interrupted compaction never damages the
  original).
- **Plain-text rebuild and replay** — deleting every index leaves the data
  fully intact and queryable from the TSV alone; a collection is reconstructed
  exactly from its change log.

The philosophy follows the databases that made testing their reputation: a
visible, exhaustive test suite is itself the assurance that the data is safe.
