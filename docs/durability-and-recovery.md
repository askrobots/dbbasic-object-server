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
  every fully-written entry before it survives. **One documented exception —
  see [Known gap](#known-gap-torn-tail-repair-on-the-write-path) below.**
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
  and the store is checked to recover to a valid state. This holds for
  single-line rows; for rows containing a newline inside a quoted field it
  does **not** yet — the simulation maps that failure rather than passing it,
  and the tests are held as strict xfails. See [Known gap](#known-gap-torn-tail-repair-on-the-write-path).
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

## Known gap: torn-tail repair on the write path

**Status: open.** Stated here because a durability document that omits it is
worse than no document — a reader plans around the guarantee they were given.

Before an append lands, `object_records._repair_torn_tail` trims any partial
row left by an interrupted write. That trim finds the last row boundary by
scanning backwards for a newline, and **that scan is quote-blind**: a newline
*inside* a quoted multi-line field looks exactly like a row boundary. So the
repair can cut mid-field, leave a fragment with an unclosed quote, and let the
next appended row be swallowed by it.

**Scope.** Only rows containing an embedded newline — in practice a field
holding JSON, an address block, or a multi-line note. Single-line rows are
unaffected. Within an affected row the exposure covers roughly 97% of its
write window.

**The read path is already fixed.** `_drop_torn_tail` uses a quote-aware
scan (`_committed_prefix_len`). The write path was changed to match and the
change was reverted: correct in isolation, but it regressed the ordinary
single-line self-heal under the full test suite in a way that was never fully
explained. It is being redone deliberately rather than reapplied.

**Also affected:** `object_backup_index._drop_torn_tail` carries the same
quote-blind check, has no test covering it, and sits in the restore path.

**How it is tracked.** Three strict-xfail acceptance tests already assert the
correct behaviour and map the trigger surface. They fail loudly the day the
bug is fixed, at which point they become ordinary regression tests:

- `tests/test_durability_torn_write_characterization.py` — every offset from
  the row's first embedded newline to its last byte
- `tests/test_embedded_json_lines_characterization.py` — the original
  silent-resurrection-and-cascade finding
- `tests/test_durability_torn_write_characterization.py` — a torn *header*,
  which reports success while truncating the file to zero bytes

**What to do meanwhile.** Nothing, for most deployments: the affected shape is
a quoted embedded newline in an append-only collection, interrupted by process
death mid-write. If you store multi-line text in a high-write append
collection and cannot tolerate the risk, keep that collection in classic
(rewrite) mode, which is unaffected.
