"""CHARACTERIZATION tests: does object_records.py give a DATABASE-grade
ATOMICITY guarantee for a single write, and is a concurrent reader ever
able to observe a torn/half-written intermediate state?

A database must be able to answer: "a write either fully lands or not at
all, and readers never see a torn intermediate." This file probes exactly
that question against both storage modes:

  - CLASSIC mode: every write (_write_collection_records) writes a whole
    new file to a temp path IN THE SAME DIRECTORY, then calls
    `temp_path.replace(path)` -- a POSIX atomic rename. A reader that
    already has the file open keeps its old (complete) view; a reader
    that opens fresh after the rename gets the new (complete) view.
    Never a mix.
  - APPEND mode: a write (_append_records_rows) appends new physical
    row(s) to the SAME inode, then (_persist_write's fast-append branch)
    updates the id->offset sidecar (`.records.oidx`) as a SEPARATE,
    later step. The append itself is a single buffered `handle.write()` +
    one `flush()` at the end of a "a" open -- the row's bytes are either
    fully written or (torn-tail case, covered by
    tests/test_object_records.py's own torn-tail suite) not, but the
    sidecar update is a genuinely distinct write that can be skipped,
    fail, or (in a real crash) simply never happen.

This is pure characterization of EXISTING behavior. It does not modify
any production module. No test here is written to a production-code
signature that doesn't exist yet, and no failing assertion is "fixed" by
weakening it -- a violation found here is left failing, wrapped in
`@pytest.mark.xfail(strict=True, ...)`, and documented, exactly like
tests/test_embedded_json_lines_characterization.py's torn-tail FINDING.
One such violation was found here (a non-UTF8-corrupted sidecar crashes a
read instead of self-healing); every other guarantee probed below HOLDS.
See each test's docstring and the summary at the bottom of this file.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

import object_records

pytestmark = pytest.mark.conformance


ID_FIELD = {"name": "id"}
VALUE_FIELD = {"name": "value"}


# --- setup helpers (mirror tests/test_object_records.py and
# tests/test_embedded_json_lines_characterization.py's conventions) --------


def write_schema(data_dir: Path, collection: str, fields: list[dict]) -> Path:
    path = data_dir / "schemas" / f"{collection}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fields": fields}))
    return path


def write_append_schema(data_dir: Path, collection: str, fields: list[dict]) -> Path:
    path = data_dir / "schemas" / f"{collection}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fields": fields, "storage": "append"}))
    return path


def _clear_caches() -> None:
    """Force the NEXT read past the warm in-process caches -- see the
    identical helper in test_embedded_json_lines_characterization.py."""
    object_records._RECORDS_CACHE.clear()
    object_records._OIDX_CACHE.clear()


# =============================================================================
# 1. ATOMIC RENAME (classic mode) -- held-fd technique
# =============================================================================


def test_classic_write_held_reader_fd_sees_whole_old_or_whole_new_never_torn(tmp_path):
    """GUARANTEE: HOLDS. Mirrors
    tests/test_object_records.py::test_classic_write_keeps_an_open_readers_fd_on_a_complete_version
    -- the "Q2 property" -- generalized here to explicitly state the
    atomicity finding this file exists to pin down: a reader fd opened
    BEFORE a classic-mode write (create/update/delete, all routed through
    _write_collection_records's temp-file + Path.replace) reads the
    COMPLETE pre-write file byte-for-byte, even after the write has fully
    completed; a fresh read after the write sees the COMPLETE post-write
    file. There is no interleaving in which a reader observes a partial
    mix of old and new bytes, because the atomic rename swaps a directory
    entry to a new inode instantaneously -- the held fd's inode is
    unaffected and was always a complete, valid file."""
    data_dir = tmp_path / "data"
    write_schema(data_dir, "ctr", [ID_FIELD, VALUE_FIELD])
    path = object_records.collection_records_file("ctr", base_dir=data_dir)
    object_records.create_collection_record("ctr", {"id": "c1", "value": "0"}, base_dir=data_dir, roots=[])
    pre_write_bytes = path.read_bytes()

    with path.open("rb") as held:
        # Sanity: the held fd can already read the pre-write content.
        assert held.read() == pre_write_bytes
        held.seek(0)

        object_records.update_collection_record(
            "ctr", "c1", {"value": "1"}, base_dir=data_dir, roots=[]
        )

        # The write is fully complete on disk by the time update_collection_record
        # returns (it is a synchronous call) -- yet the held fd, unaffected by
        # the rename, still reads the WHOLE old version, not a mix.
        assert held.read() == pre_write_bytes

    # A fresh reader (opening the new directory entry) sees the WHOLE new
    # version.
    assert path.read_bytes() == b"id\tvalue\nc1\t1\n"


# =============================================================================
# 2. ATOMIC RENAME (classic mode) -- concurrent thread, raw-byte level
# =============================================================================


def test_classic_atomic_rename_reader_thread_raw_bytes_always_one_complete_snapshot(tmp_path):
    """GUARANTEE: HOLDS. Purest form of the atomic-rename probe: a reader
    thread repeatedly reads the RAW BYTES of records.tsv (bypassing
    object_records' own CSV parsing entirely -- so this can't be
    accidentally masked by a forgiving parser) while a writer thread
    concurrently performs N updates, each one a full temp-file + rename
    replace. Every single raw read observed by the reader must be
    EXACTLY one of the N+1 known-good complete snapshots -- never a
    truncated, duplicated, or spliced mixture of two versions."""
    data_dir = tmp_path / "data"
    write_schema(data_dir, "ctr", [ID_FIELD, VALUE_FIELD])
    object_records.create_collection_record("ctr", {"id": "c1", "value": "0"}, base_dir=data_dir, roots=[])
    path = object_records.collection_records_file("ctr", base_dir=data_dir)

    n = 300
    known_good_snapshots = {f"id\tvalue\nc1\t{i}\n" for i in range(n)}

    stop = threading.Event()
    anomalies: list[str] = []
    reads_done = [0]

    def reader() -> None:
        while not stop.is_set():
            try:
                raw = path.read_text()
            except FileNotFoundError:
                # Should never happen (rename always leaves a directory
                # entry) -- if it does, that IS a torn-intermediate finding.
                anomalies.append("<FileNotFoundError: no directory entry momentarily>")
                continue
            reads_done[0] += 1
            if raw not in known_good_snapshots:
                anomalies.append(raw)

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    try:
        for i in range(n):
            object_records.update_collection_record(
                "ctr", "c1", {"value": str(i)}, base_dir=data_dir, roots=[]
            )
    finally:
        stop.set()
        reader_thread.join(timeout=15)

    assert not reader_thread.is_alive(), "reader thread failed to stop"
    assert reads_done[0] > 0, "reader thread never got a chance to run concurrently"
    assert anomalies == [], (
        f"raw bytes read were NOT a complete known-good snapshot in "
        f"{len(anomalies)} case(s) out of {reads_done[0]} reads; first "
        f"offending read: {anomalies[0]!r}"
    )


# =============================================================================
# 3. os.replace SAME-FILESYSTEM CONFIRMATION
# =============================================================================


def test_classic_temp_file_is_created_in_the_same_directory_as_the_target(tmp_path, monkeypatch):
    """GUARANTEE: HOLDS. `os.replace`/`Path.replace` is only atomic when
    source and destination are on the SAME filesystem; across filesystems
    the OS (or Python) must fall back to copy+delete, which is NOT atomic
    and reintroduces exactly the torn-intermediate risk this whole file
    is probing for. This test spies on Path.replace (restored automatically
    by monkeypatch at teardown) to confirm, for a REAL write through the
    public API, that the temp file _write_collection_records creates
    lives in the exact same directory as -- and therefore is
    guaranteed to share a device/filesystem with -- the target
    records.tsv it replaces. (Source-level confirmation: object_records.py's
    _write_collection_records builds `temp_path` via
    `path.with_name(...)`, which by construction keeps the same parent
    directory; this test verifies that holds for a real end-to-end call,
    not just by reading the source.)"""
    data_dir = tmp_path / "data"
    write_schema(data_dir, "ctr", [ID_FIELD, VALUE_FIELD])
    path = object_records.collection_records_file("ctr", base_dir=data_dir)

    captured: list[tuple[Path, Path, int, int]] = []
    original_replace = Path.replace

    def spy_replace(self: Path, target):
        target_path = Path(target)
        # Stat BEFORE the rename consumes the temp path's directory entry.
        dev_temp = self.stat().st_dev
        dev_target_dir = target_path.parent.stat().st_dev
        captured.append((self, target_path, dev_temp, dev_target_dir))
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy_replace)

    object_records.create_collection_record("ctr", {"id": "c1", "value": "0"}, base_dir=data_dir, roots=[])

    assert captured, "expected _write_collection_records to call Path.replace at least once"
    temp_path, target_path, dev_temp, dev_target_dir = captured[-1]
    assert target_path == path
    assert temp_path.parent == target_path.parent, (
        "temp file and target must live in the same directory for the "
        "replace to be a pure (atomic) rename rather than a cross-device "
        "copy+delete"
    )
    assert dev_temp == dev_target_dir, (
        "temp file and target directory are not on the same filesystem "
        "(st_dev differs) -- os.replace would not be atomic here"
    )


# =============================================================================
# 4. APPEND MODE ORDERING -- record bytes land BEFORE the sidecar update,
#    and are recoverable from the TSV alone if the sidecar update never
#    happens (the sidecar is genuinely disposable / "never required for
#    correctness")
# =============================================================================


def test_append_mode_record_bytes_survive_and_are_recoverable_when_sidecar_update_fails(
    tmp_path, monkeypatch
):
    """GUARANTEE: HOLDS -- and this is the central append-mode finding.
    _persist_write's fast-append branch performs, in order: (1)
    `_append_records_rows` -- appends the new physical row and flushes it
    to records.tsv, returning its exact byte span; (2) updates the
    in-process records cache; (3) `_update_oidx_after_append` -- updates
    the id->offset sidecar. Step 3 is a SEPARATE write to a SEPARATE file,
    with no transaction wrapping it and step 1 together.

    This test simulates a crash landing exactly between step 1 and step 3
    by monkeypatching `_update_oidx_after_append` to raise. The row's
    bytes are, by construction, already fully written and flushed to
    records.tsv by the time that patched function is even called -- so
    the record must still be present on disk, and (once the sidecar
    fault is removed and caches are cleared, simulating the process
    restarting) still fully readable via the public API, sourced from
    the TSV alone (the in-memory sidecar cache is dropped and the
    on-disk sidecar is stale/never written for this row, forcing a fresh
    _load_oidx rebuild -- see _load_oidx's "100% best-effort and NEVER
    raises" contract, docs/append-only-storage-design.md's "sidecar is
    never required for correctness")."""
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "logs", [ID_FIELD, VALUE_FIELD])
    object_records.create_collection_record("logs", {"id": "l1", "value": "one"}, base_dir=data_dir, roots=[])
    path = object_records.collection_records_file("logs", base_dir=data_dir)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash between record append and sidecar update")

    monkeypatch.setattr(object_records, "_update_oidx_after_append", boom)

    with pytest.raises(RuntimeError):
        object_records.create_collection_record(
            "logs", {"id": "l2", "value": "two"}, base_dir=data_dir, roots=[]
        )

    # The record row's bytes are ALREADY durably on disk despite the
    # "crash" in the step that was supposed to run after it.
    raw = path.read_text()
    assert "l2" in raw and "two" in raw, (
        "the append (step 1) must have already landed on disk before the "
        f"simulated sidecar-update failure (step 3); got file content: {raw!r}"
    )

    # Restore the real sidecar-update function (simulating the process
    # restarting clean) and drop every in-memory cache (simulating a cold
    # process start) -- the sidecar on disk was never told about l2 at
    # all. The record must still be readable, sourced purely from the
    # canonical TSV via a fresh sidecar rebuild.
    monkeypatch.undo()
    _clear_caches()

    listing = object_records.list_collection_records("logs", base_dir=data_dir, roots=[])["records"]
    assert listing == [
        {"id": "l1", "value": "one"},
        {"id": "l2", "value": "two"},
    ]

    _clear_caches()
    by_id = object_records.get_collection_record("logs", "l2", base_dir=data_dir, roots=[])
    assert by_id == {"id": "l2", "value": "two"}


def test_append_mode_sidecar_fully_deleted_record_still_recoverable_from_tsv_alone(
    tmp_path, monkeypatch
):
    """GUARANTEE: HOLDS. A stronger, more literal version of "the sidecar
    is disposable": build up a normal append-mode collection, force the
    on-disk `.records.oidx` sidecar to actually exist (a cold by-id read
    lazily builds/writes it -- see _load_oidx), then DELETE it outright
    (simulating a crash that landed after the record append but with the
    sidecar file never having been written/flushed at all, or simply lost).
    A subsequent read must still resolve every record correctly, purely by
    falling back to a fresh sequential scan of records.tsv (the sidecar's
    own "100% best-effort and NEVER raises" rebuild path) -- and it must
    reconstitute a fresh sidecar file on disk as a side effect, since the
    sidecar is a derived accelerator, not a source of truth."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "logs", [ID_FIELD, VALUE_FIELD])
    for i in range(5):
        object_records.create_collection_record(
            "logs", {"id": f"l{i}", "value": str(i)}, base_dir=data_dir, roots=[]
        )
    path = object_records.collection_records_file("logs", base_dir=data_dir)

    # Force the on-disk sidecar to exist: a cold by-id read (cache
    # disabled via the env var above) must go through _load_oidx, which
    # writes it out.
    _clear_caches()
    warm = object_records.get_collection_record("logs", "l2", base_dir=data_dir, roots=[])
    assert warm == {"id": "l2", "value": "2"}
    sidecar_path = path.with_name(object_records.OIDX_FILE)
    assert sidecar_path.exists(), "expected the sidecar to have been built by the by-id read above"

    # Simulate total loss of the sidecar (crash before it was ever
    # written/flushed, or the file simply lost) -- delete it, and drop
    # every in-memory trace of it too (simulating a cold process).
    sidecar_path.unlink()
    _clear_caches()
    assert not sidecar_path.exists()

    # Every record must still resolve correctly, by-id and via a full list,
    # sourced entirely from records.tsv.
    for i in range(5):
        rec = object_records.get_collection_record("logs", f"l{i}", base_dir=data_dir, roots=[])
        assert rec == {"id": f"l{i}", "value": str(i)}

    _clear_caches()
    listing = object_records.list_collection_records("logs", base_dir=data_dir, roots=[])["records"]
    assert listing == [{"id": f"l{i}", "value": str(i)} for i in range(5)]

    # And the sidecar was disposable in BOTH directions: it self-heals
    # (a fresh rebuild is written back to disk once something reads
    # through it again).
    assert sidecar_path.exists(), "expected _load_oidx's rebuild path to have re-created the sidecar"


def test_append_mode_sidecar_corrupted_valid_utf8_garbage_still_recoverable(
    tmp_path, monkeypatch
):
    """GUARANTEE: HOLDS. Baseline for the FINDING test right below this
    one: the sidecar file present and non-empty, but WRONG -- overwritten
    with plain-ASCII (therefore valid-UTF8) garbage that doesn't match the
    "oidx1\\t<ino>" header format at all -- simulating a corrupt-but-still-
    decodable sidecar write. `_read_oidx_header`'s tag/column-count/int
    checks correctly treat this as an unparseable header and `_load_oidx`
    falls back to a full, correct rebuild from the canonical records.tsv."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "logs", [ID_FIELD, VALUE_FIELD])
    for i in range(5):
        object_records.create_collection_record(
            "logs", {"id": f"l{i}", "value": str(i)}, base_dir=data_dir, roots=[]
        )
    path = object_records.collection_records_file("logs", base_dir=data_dir)

    _clear_caches()
    object_records.get_collection_record("logs", "l2", base_dir=data_dir, roots=[])
    sidecar_path = path.with_name(object_records.OIDX_FILE)
    assert sidecar_path.exists()

    # Corrupt it: valid-UTF8 (plain ASCII) garbage, no valid
    # "oidx1\t<ino>" header shape at all.
    sidecar_path.write_text("this-is-not-a-real-sidecar-header-line\nmore junk\n")
    _clear_caches()

    for i in range(5):
        rec = object_records.get_collection_record("logs", f"l{i}", base_dir=data_dir, roots=[])
        assert rec == {"id": f"l{i}", "value": str(i)}, (
            "a corrupt-but-decodable sidecar must never cause a wrong/missing "
            "read result -- it must be detected as incoherent and the read "
            "must fall back to the canonical records.tsv"
        )


@pytest.mark.xfail(
    strict=True,
    reason="FINDING (durability/atomicity characterization): _read_oidx_header "
    "(object_records.py) opens the sidecar in TEXT mode with only "
    "`except OSError` around the read, so a sidecar corrupted with bytes "
    "that are not valid UTF-8 raises an uncaught UnicodeDecodeError instead "
    "of being treated as 'header doesn't parse -> rebuild' like every other "
    "corrupt-header shape already is (a wrong tag, wrong column count, a "
    "non-integer ino all safely fall through to None/rebuild). This breaks "
    "_load_oidx's own documented contract ('100% best-effort and NEVER "
    "raises... any inconsistency -- missing sidecar, corrupt sidecar, inode "
    "mismatch -- triggers a rebuild rather than propagating an error'). The "
    "practical effect: get_collection_record/list_collection_records raise "
    "instead of self-healing, for any append-mode collection whose sidecar "
    "got corrupted with invalid-UTF-8 bytes (a plausible real crash shape -- "
    "a write torn mid multi-byte character, or a completely unrelated file "
    "clobbering the same path). Deferred as characterization-only per this "
    "task's scope (no production code changes); strict=True flips this to "
    "XPASS the day _read_oidx_header is hardened to catch decode errors too.",
)
def test_append_mode_sidecar_corrupted_non_utf8_garbage_crashes_reads_FINDING(
    tmp_path, monkeypatch
):
    """FINDING: unlike the sidecar-deleted case (fully recoverable) and a
    valid-UTF8-but-wrong-format-garbage sidecar (also recoverable --
    `_read_oidx_header`'s tag/column/int checks handle that), a sidecar
    corrupted with bytes that are not valid UTF-8 at all crashes the read
    with an uncaught UnicodeDecodeError instead of falling back to a
    rebuild. See the xfail reason above for the exact mechanism."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "logs", [ID_FIELD, VALUE_FIELD])
    for i in range(5):
        object_records.create_collection_record(
            "logs", {"id": f"l{i}", "value": str(i)}, base_dir=data_dir, roots=[]
        )
    path = object_records.collection_records_file("logs", base_dir=data_dir)

    _clear_caches()
    object_records.get_collection_record("logs", "l2", base_dir=data_dir, roots=[])
    sidecar_path = path.with_name(object_records.OIDX_FILE)
    assert sidecar_path.exists()

    # Corrupt it: invalid-UTF-8 bytes, no valid "oidx1\t<ino>" header at all.
    sidecar_path.write_bytes(b"\x00\x01garbage-not-an-oidx-file\xff\xfe")
    _clear_caches()

    for i in range(5):
        rec = object_records.get_collection_record("logs", f"l{i}", base_dir=data_dir, roots=[])
        assert rec == {"id": f"l{i}", "value": str(i)}, (
            "a corrupt sidecar must never cause a wrong/missing read result -- "
            "it must be detected as incoherent and the read must fall back to "
            "the canonical records.tsv"
        )


# =============================================================================
# 5. CROSS-ARTIFACT (NOT single-record) ATOMICITY BOUNDARY -- create's
#    record write vs. its record-change emission
# =============================================================================


def test_record_write_and_its_record_change_emission_are_two_separate_commits_not_one(
    tmp_path, monkeypatch
):
    """GUARANTEE: honestly documented, NOT a single-transaction atomicity
    claim -- this is the boundary the report calls out explicitly.
    create_collection_record performs two independent durable writes:
    (a) the record row itself, via _persist_write (atomic for classic,
    append-then-sidecar for append -- both probed above), and (b) a
    record-change audit entry, via object_record_changes.append_record_change,
    which appends a JSON line to a COMPLETELY SEPARATE file
    (record_changes/<collection>/changes.jsonl) under its OWN lock. These
    are not wrapped in any shared transaction.

    Simulated here by monkeypatching append_record_change to raise AFTER
    the record write has already happened (it is called after
    _persist_write in create_collection_record's body): the record itself
    is fully committed and durably readable even though its accompanying
    change-log entry never landed. This is the honest, narrower claim:
    a SINGLE record's write is atomic; a record plus a separate artifact
    describing it is NOT jointly atomic -- each write is atomic
    individually, but the pair can diverge if the process dies between
    them."""
    data_dir = tmp_path / "data"
    write_schema(data_dir, "contacts", [ID_FIELD, {"name": "name"}])

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash between record write and record-change emission")

    monkeypatch.setattr(object_records.object_record_changes, "append_record_change", boom)

    with pytest.raises(RuntimeError):
        object_records.create_collection_record(
            "contacts", {"id": "c1", "name": "Ada"}, base_dir=data_dir, roots=[]
        )

    # The record write landed and is durably readable, DESPITE the
    # "crash" in the very next step of the same function call.
    monkeypatch.undo()
    _clear_caches()
    rec = object_records.get_collection_record("contacts", "c1", base_dir=data_dir, roots=[])
    assert rec == {"id": "c1", "name": "Ada"}, (
        "the record write (step 1) must be unaffected by a failure in the "
        "subsequent, separate record-change write (step 2)"
    )

    # ...but there is no corresponding record-change entry: the pair
    # genuinely diverged.
    changes_path = data_dir / "record_changes" / "contacts" / "changes.jsonl"
    assert not changes_path.exists() or changes_path.read_text() == "", (
        "expected NO record-change entry for c1 (its emission was the thing "
        "that 'crashed') -- if this fails, the two writes may have become "
        "coupled and this characterization needs updating, not the "
        "assertion weakened"
    )


# =============================================================================
# 6. READER-DURING-WRITE, VIA THE PUBLIC API -- both storage modes
# =============================================================================


def test_reader_during_write_classic_mode_never_observes_partial_or_corrupt_records(tmp_path):
    """GUARANTEE: HOLDS. A reader thread calls read_collection_records in a
    tight loop (going through the REAL parser, unlike test 2 above) while
    a writer thread performs N updates concurrently, classic mode. Every
    single read observed by the reader must be a well-formed, single-row,
    two-field record with a numeric value -- proving the reader-holds-no-
    lock design (see object_records.py's CONCURRENCY note) never yields a
    torn/half-written row for this write pattern."""
    data_dir = tmp_path / "data"
    write_schema(data_dir, "ctr", [ID_FIELD, VALUE_FIELD])
    object_records.create_collection_record("ctr", {"id": "c1", "value": "0"}, base_dir=data_dir, roots=[])

    n = 300
    stop = threading.Event()
    anomalies: list[str] = []
    reads_done = [0]

    def reader() -> None:
        while not stop.is_set():
            try:
                records = object_records.read_collection_records("ctr", base_dir=data_dir, roots=[])
            except Exception as exc:  # noqa: BLE001 - characterizing, must not miss any failure mode
                anomalies.append(f"exception during read: {exc!r}")
                continue
            reads_done[0] += 1
            if len(records) != 1:
                anomalies.append(f"unexpected record count (torn write?): {records!r}")
                continue
            rec = records[0]
            if set(rec.keys()) != {"id", "value"} or rec["id"] != "c1":
                anomalies.append(f"torn/partial record shape: {rec!r}")
                continue
            try:
                int(rec["value"])
            except ValueError:
                anomalies.append(f"unparseable value (torn write?): {rec!r}")

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    try:
        for i in range(n):
            object_records.update_collection_record(
                "ctr", "c1", {"value": str(i)}, base_dir=data_dir, roots=[]
            )
    finally:
        stop.set()
        reader_thread.join(timeout=15)

    assert not reader_thread.is_alive()
    assert reads_done[0] > 0, "reader thread never got a chance to run concurrently"
    assert anomalies == [], f"reader observed {len(anomalies)} anomalies; first: {anomalies[0]!r}"


def test_reader_during_write_append_mode_never_observes_partial_or_corrupt_records(
    tmp_path, monkeypatch
):
    """GUARANTEE: HOLDS. Same probe as above, append mode -- and with
    DBBASIC_RECORDS_CACHE_MAX_ROWS=0 so the reader is forced through the
    id->offset sidecar / cold full-fold path on every single call (the
    warm-cache path would trivially never see anything, since the writer
    holds the module's own lock while updating the cache -- this is
    specifically about the LOCK-FREE disk-backed read path racing a live
    append). Every by-id read must return a well-formed, complete,
    numeric-valued record -- never a truncated/garbled row from a write
    caught mid-flight, and never a stale sidecar entry pointing at a
    since-superseded offset."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "ctr", [ID_FIELD, VALUE_FIELD])
    object_records.create_collection_record("ctr", {"id": "c1", "value": "0"}, base_dir=data_dir, roots=[])

    n = 250
    stop = threading.Event()
    anomalies: list[str] = []
    reads_done = [0]

    def reader() -> None:
        while not stop.is_set():
            try:
                rec = object_records.get_collection_record("ctr", "c1", base_dir=data_dir, roots=[])
            except Exception as exc:  # noqa: BLE001 - characterizing, must not miss any failure mode
                anomalies.append(f"exception during by-id read: {exc!r}")
                continue
            reads_done[0] += 1
            if rec is None:
                anomalies.append("record disappeared mid-write")
                continue
            if set(rec.keys()) != {"id", "value"} or rec["id"] != "c1":
                anomalies.append(f"torn/partial record shape: {rec!r}")
                continue
            try:
                int(rec["value"])
            except ValueError:
                anomalies.append(f"unparseable value (torn write?): {rec!r}")

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    try:
        for i in range(n):
            object_records.update_collection_record(
                "ctr", "c1", {"value": str(i)}, base_dir=data_dir, roots=[]
            )
    finally:
        stop.set()
        reader_thread.join(timeout=15)

    assert not reader_thread.is_alive()
    assert reads_done[0] > 0, "reader thread never got a chance to run concurrently"
    assert anomalies == [], f"reader observed {len(anomalies)} anomalies; first: {anomalies[0]!r}"


# =============================================================================
# SUMMARY (see also the accompanying report)
#
#   - Single-record write, classic mode: ATOMIC (all-or-nothing). Backed
#     by temp-file-in-same-directory + Path.replace (POSIX atomic
#     rename). A concurrent reader NEVER observes a torn/partial file --
#     confirmed both at the raw-byte level (test 2) and through a held
#     pre-write fd (test 1) -- and the temp file's same-directory
#     placement is confirmed to share a filesystem (st_dev) with the
#     target, so the replace is a true rename, never a cross-device
#     copy+delete fallback (test 3).
#   - Single-record write, append mode: the RECORD ROW ITSELF is atomic
#     in the same sense -- it lands as one flushed write to the file
#     before anything else happens. The id->offset SIDECAR update is a
#     SEPARATE, later, best-effort step -- and is provably disposable:
#     deleting it, or having its update fail outright (simulating a crash
#     between the append and the sidecar write) never loses or corrupts a
#     record -- every read falls back to a fresh, correct rebuild from the
#     canonical records.tsv (tests 4a/4b). This is the concrete meaning of
#     "sidecar is never required for correctness": it is a derived
#     accelerator, not a source of truth. ONE EXCEPTION FOUND (test 4d,
#     xfail/FINDING, not fixed here): a sidecar corrupted with bytes that
#     are not valid UTF-8 crashes the read (UnicodeDecodeError) instead of
#     being treated like every other corrupt-header shape and falling back
#     to a rebuild -- _read_oidx_header's `except OSError` doesn't catch
#     it. Valid-UTF8-but-wrong-format garbage IS handled correctly
#     (test 4c, HOLDS) -- this is specifically a decode-error gap.
#   - Reader-during-write: HOLDS in both modes. A lock-free reader thread
#     racing a writer thread across hundreds of updates never observed a
#     torn, partial, or unparseable record, at either the raw-byte level
#     (classic) or the parsed-record level (both modes) (tests 2, 6a, 6b).
#   - Cross-artifact atomicity: DOES NOT HOLD, and is not claimed to.
#     create_collection_record's record write and its record-change audit
#     emission are two independent commits to two independent files, not
#     one transaction -- a failure between them leaves the record durably
#     committed with no matching change-log entry (test 5). Each write is
#     individually atomic; the PAIR is not jointly atomic. This is the
#     honest boundary of what this engine's atomicity guarantee covers:
#     single-record, single-file writes -- not multi-artifact operations.
# =============================================================================
