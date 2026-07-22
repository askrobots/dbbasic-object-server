"""CHARACTERIZATION tests: does BACKUP/RESTORE and COMPACTION preserve data
with byte-exact fidelity?

This file is pure characterization of EXISTING behavior. It does not modify
any production module (object_records.py, object_backup.py,
object_backup_index.py, object_daemon.py are all untouched). Where a case
fails or corrupts data, the assertion states the correctness property a safe
durability substrate SHOULD have and is left to fail -- that failure IS the
finding. Do not "fix" a failing assertion here by weakening it to match
broken behavior.

Context (see tests/test_embedded_json_lines_characterization.py for the
sibling investigation this one continues): an append-mode collection
accumulates superseded/tombstoned physical rows as it is written to, and is
periodically COMPACTED -- folded down to just its live rows -- either on
demand (object_records.compact_collection, exercised directly here) or via
object_daemon.py's process_compactions poller (not imported here; this file
stays at the object_records.compact_collection layer, which is what the
daemon itself calls). Separately, the whole runtime -- including every
collection's records.tsv, live or append-mode -- can be BACKED UP (a tar/gz
archive: object_backup.create_runtime_backup /
create_runtime_restore_point) and RESTORED into a fresh directory
(object_backup.restore_runtime_backup). A third module,
object_backup_index.py, does NOT participate in backup/restore itself --
it only offers read-only preview/diff of a backup's records against live
data (preview_collection/preview_record) -- but it carries its OWN local
copy of a quote-blind "torn tail" check (_drop_torn_tail), independently
reimplemented from object_records._drop_torn_tail (documented KNOWN
LIMITATION / substrate bug #2 there: a crash landing right after a newline
that is INSIDE an open quoted multi-line cell leaves the file looking
"terminated" -- ends with \\n -- even though the row never actually
finished, so the naive "ends with \\n" check wrongly calls it committed).
Part of this file's job is to confirm whether that same latent bug is
reachable through object_backup_index's copy of the check against a backed
up (not just a live) records.tsv.
"""

from __future__ import annotations

import csv
import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest

import object_backup
import object_backup_index
import object_records

pytestmark = pytest.mark.conformance


ID_FIELD = {"name": "id"}
NOTE_FIELD = {"name": "note", "type": "textarea"}
LINES_FIELD = {"name": "lines", "type": "textarea"}


# --- setup helpers (mirror tests/test_object_records.py /
#     tests/test_embedded_json_lines_characterization.py conventions) -------


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
    """Force the NEXT read past the warm in-process caches (see the sibling
    embedded-JSON characterization file for why this matters for by-id
    reads specifically)."""
    object_records._RECORDS_CACHE.clear()
    object_records._OIDX_CACHE.clear()


def _records_path(data_dir: Path, collection: str) -> Path:
    return data_dir / "collections" / collection / "records.tsv"


def _exotic_corpus() -> list[dict]:
    """A grab-bag of hostile-but-legal field values: ASCII, multibyte,
    emoji (astral-plane, so a surrogate pair in UTF-16 but a single scalar
    in UTF-8), embedded compact JSON, embedded PRETTY-PRINTED JSON (real
    raw newline bytes in the cell -- the case that actually stresses the
    CSV-aware row-boundary machinery), a large field, and a bare literal
    CR (0x0D, not part of a CRLF pair) -- which the CSV writer must quote
    to round-trip safely (an unquoted bare CR is not a delimiter itself
    but is exactly the kind of "surprising control byte" this substrate's
    hostile-content tests exist to check)."""
    return [
        {
            "id": "r-ascii",
            "note": "plain ascii, no surprises",
            "lines": json.dumps([{"sku": "A-1", "qty": 1}]),
        },
        {
            "id": "r-multibyte",
            "note": "café naïve résumé 日本語 中文",
            "lines": json.dumps([{"sku": "日本-1", "note": "中文注释"}]),
        },
        {
            "id": "r-emoji",
            "note": "party \U0001f389 rocket \U0001f680 family \U0001f468‍\U0001f469‍\U0001f467",
            "lines": json.dumps([{"sku": "EMOJI-1", "label": "\U0001f525 hot"}]),
        },
        {
            "id": "r-embedded-json-compact",
            "note": "compact json blob (no raw control bytes)",
            "lines": json.dumps(
                [{"sku": f"SKU-{i:03d}", "qty": i, "note": "x\ty\nz\"q\"\\w"} for i in range(20)]
            ),
        },
        {
            "id": "r-embedded-json-pretty",
            "note": "pretty json blob -- REAL raw newline bytes in this cell",
            "lines": json.dumps(
                [{"sku": f"SKU-{i:03d}", "qty": i} for i in range(10)], indent=2
            ),
        },
        {
            "id": "r-bare-cr",
            "note": "alpha\rbeta\rgamma",  # bare CR, not CRLF -- must be quoted to survive
            "lines": json.dumps([{"sku": "CR-1"}]),
        },
        {
            "id": "r-crlf",
            "note": "alpha\r\nbeta\r\ngamma",
            "lines": json.dumps([{"sku": "CRLF-1"}]),
        },
        {
            "id": "r-large",
            "note": "large field",
            "lines": json.dumps([{"sku": f"BIG-{i:05d}", "qty": i, "price": 1099 + i} for i in range(2000)]),
        },
    ]


# =============================================================================
# 1. COMPACTION FIDELITY -- live records survive byte-exact, dead rows gone
# =============================================================================


def test_compaction_preserves_live_records_byte_exact_including_exotic_content(tmp_path):
    """Build an append-mode collection with a realistic mix of creates,
    superseding updates, and deletes (tombstones) over the exotic corpus,
    compact it, and check every angle of "did compaction preserve exactly
    the live set, byte-exact, and nothing else":

      - every live record's field values are byte-for-byte identical to
        what a full-fold read reported immediately BEFORE compaction
      - superseded/tombstoned rows are gone from the physical file
      - the physical row count actually dropped
      - a read after compaction returns exactly the live set (no more, no
        less)
      - by-id reads still work for every live id post-compaction, via the
        REBUILT sidecar (cold caches, forcing the id->offset oidx path)
    """
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "docs", [ID_FIELD, NOTE_FIELD, LINES_FIELD])

    corpus = _exotic_corpus()
    for rec in corpus:
        object_records.create_collection_record("docs", dict(rec), base_dir=data_dir, roots=[])

    # Superseding updates on a few ids (multiple times each -- each update
    # is a brand-new physical row in append mode, leaving its predecessors
    # superseded-but-not-yet-compacted).
    object_records.update_collection_record(
        "docs", "r-ascii", {"note": "ascii v2"}, base_dir=data_dir, roots=[]
    )
    object_records.update_collection_record(
        "docs", "r-ascii", {"note": "ascii v3 final"}, base_dir=data_dir, roots=[]
    )
    object_records.update_collection_record(
        "docs", "r-multibyte", {"note": "café v2 日本語"}, base_dir=data_dir, roots=[]
    )
    object_records.update_collection_record(
        "docs",
        "r-embedded-json-pretty",
        {"lines": json.dumps([{"sku": "REPLACED", "qty": 999}], indent=2)},
        base_dir=data_dir,
        roots=[],
    )

    # Tombstone a couple of ids.
    object_records.delete_collection_record("docs", "r-crlf", base_dir=data_dir, roots=[])
    object_records.delete_collection_record("docs", "r-large", base_dir=data_dir, roots=[])

    # Capture the exact live set (full-fold read) immediately before
    # compaction: this is the ground truth compaction must reproduce.
    _clear_caches()
    live_before = {
        r["id"]: r
        for r in object_records.read_collection_records("docs", base_dir=data_dir, roots=[])
    }
    expected_ids = {"r-ascii", "r-multibyte", "r-emoji", "r-embedded-json-compact",
                     "r-embedded-json-pretty", "r-bare-cr"}
    assert set(live_before) == expected_ids
    assert live_before["r-ascii"]["note"] == "ascii v3 final"
    assert live_before["r-multibyte"]["note"] == "café v2 日本語"
    assert json.loads(live_before["r-embedded-json-pretty"]["lines"]) == [
        {"sku": "REPLACED", "qty": 999}
    ]
    assert live_before["r-bare-cr"]["note"] == "alpha\rbeta\rgamma"

    path = _records_path(data_dir, "docs")
    with path.open(newline="") as fh:
        physical_rows_before = sum(1 for _ in csv.reader(fh, delimiter="\t")) - 1  # minus header
    raw_before = path.read_text()
    assert "\tdel\t" in raw_before or raw_before.count("del") >= 1  # tombstones present

    result = object_records.compact_collection("docs", base_dir=data_dir, roots=[])

    # --- physical row count actually dropped ---
    assert result["rows_before"] == physical_rows_before
    assert result["rows_after"] == len(expected_ids), (
        f"compacted physical row count should equal the live set size; "
        f"got {result}"
    )
    assert result["rows_after"] < result["rows_before"]
    assert result["bytes_after"] < result["bytes_before"]

    # --- superseded/tombstoned rows gone from the physical file ---
    raw_after = path.read_text()
    assert "del" not in raw_after.split("\n")[0]  # header sanity
    for line in raw_after.splitlines()[1:]:
        assert not line.startswith("del\t"), f"tombstone row survived compaction: {line!r}"
    assert "r-crlf" not in raw_after
    assert "r-large" not in raw_after
    assert "ascii v2" not in raw_after  # superseded value must not linger anywhere on disk
    assert "REPLACED" not in raw_after or json.loads(live_before["r-embedded-json-pretty"]["lines"])

    # --- read after compaction returns exactly the live set, byte-exact ---
    _clear_caches()
    live_after = {
        r["id"]: r
        for r in object_records.read_collection_records("docs", base_dir=data_dir, roots=[])
    }
    assert set(live_after) == expected_ids
    for record_id in expected_ids:
        assert live_after[record_id] == live_before[record_id], (
            f"compaction changed live content for {record_id!r}: "
            f"before={live_before[record_id]!r} after={live_after[record_id]!r}"
        )

    # --- by-id reads still work for every live id, sidecar rebuilt ---
    for record_id in expected_ids:
        _clear_caches()  # cold cache + cold sidecar -> forces oidx rebuild from scratch
        by_id = object_records.get_collection_record("docs", record_id, base_dir=data_dir, roots=[])
        assert by_id == live_before[record_id], (
            f"post-compaction by-id read (rebuilt sidecar) diverged for {record_id!r}: "
            f"{by_id!r} != {live_before[record_id]!r}"
        )

    # --- deleted ids are genuinely gone, not just excluded from listing ---
    for dead_id in ("r-crlf", "r-large"):
        _clear_caches()
        with pytest.raises(object_records.RecordNotFoundError):
            object_records.get_collection_record("docs", dead_id, base_dir=data_dir, roots=[])


def test_compaction_of_never_touched_and_all_deleted_collection_is_honest(tmp_path):
    """Edge cases around the main fidelity test: compacting a collection
    where EVERY record has since been deleted must leave zero live rows
    (not resurrect anything), and compacting twice in a row must be a
    stable no-op the second time."""
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "ephemeral", [ID_FIELD, NOTE_FIELD])

    for i in range(5):
        object_records.create_collection_record(
            "ephemeral", {"id": f"e{i}", "note": f"note-{i}"}, base_dir=data_dir, roots=[]
        )
    for i in range(5):
        object_records.delete_collection_record("ephemeral", f"e{i}", base_dir=data_dir, roots=[])

    result = object_records.compact_collection("ephemeral", base_dir=data_dir, roots=[])
    assert result["rows_after"] == 0

    _clear_caches()
    listing = object_records.list_collection_records("ephemeral", base_dir=data_dir, roots=[])["records"]
    assert listing == []

    # Compacting an already-fully-compacted (here: empty) collection again
    # must be a stable no-op.
    result2 = object_records.compact_collection("ephemeral", base_dir=data_dir, roots=[])
    assert result2["rows_before"] == 0
    assert result2["rows_after"] == 0


# =============================================================================
# 2. COMPACTION CRASH-SAFETY -- interrupted compaction must not lose data
# =============================================================================


def test_compaction_interrupted_before_atomic_replace_leaves_original_untouched(tmp_path, monkeypatch):
    """compact_collection rewrites via _write_collection_records, which
    writes the FOLDED content to a fresh temp file and only then does an
    atomic `temp_path.replace(path)` -- so a crash at any point BEFORE
    that replace call must leave the original records.tsv completely
    untouched (the classic "write-new, then atomically swap" durability
    property). Verified here by monkeypatching Path.replace to raise
    partway through a real compact_collection() call, simulating a process
    crash/OSError at exactly that instant, then confirming:

      1. compact_collection's exception propagates (it does not silently
         swallow the failure and report success)
      2. the original records.tsv is byte-for-byte unchanged from before
         the attempted compaction (superseded rows and tombstones intact
         -- nothing was lost, nothing was half-written)
      3. a normal read through the public API still returns the correct,
         complete pre-compaction live set
      4. once the injected failure is removed, retrying compact_collection
         succeeds and produces the correct compacted result -- i.e. the
         interrupted attempt did not leave the collection in a state that
         a follow-up compaction can't recover from
    """
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "orders", [ID_FIELD, NOTE_FIELD])
    object_records.create_collection_record(
        "orders", {"id": "o1", "note": "v1"}, base_dir=data_dir, roots=[]
    )
    object_records.update_collection_record(
        "orders", "o1", {"note": "v2"}, base_dir=data_dir, roots=[]
    )
    object_records.update_collection_record(
        "orders", "o1", {"note": "v3"}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "orders", {"id": "o2", "note": "keep-me"}, base_dir=data_dir, roots=[]
    )
    object_records.delete_collection_record("orders", "o2", base_dir=data_dir, roots=[])

    path = _records_path(data_dir, "orders")
    original_bytes = path.read_bytes()

    _clear_caches()
    live_before = {
        r["id"]: r
        for r in object_records.read_collection_records("orders", base_dir=data_dir, roots=[])
    }
    assert live_before == {"o1": {"id": "o1", "note": "v3"}}

    real_replace = Path.replace
    state = {"armed": True}

    def _boom_replace(self, target):
        # temp_path is built as f".{path.name}.{pid}.{tid}.tmp" alongside
        # the real records.tsv -- i.e. ".records.tsv.<pid>.<tid>.tmp" in
        # the "orders" collection's own directory (the collection name is
        # not itself part of the filename, only the parent directory).
        if (
            state["armed"]
            and self.name.startswith(".records.tsv.")
            and self.name.endswith(".tmp")
            and self.parent.name == "orders"
        ):
            state["armed"] = False
            raise OSError("simulated crash: process died mid-compaction, before atomic replace")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _boom_replace)

    with pytest.raises(OSError, match="simulated crash"):
        object_records.compact_collection("orders", base_dir=data_dir, roots=[])

    # 2. original file byte-for-byte unchanged.
    assert path.read_bytes() == original_bytes, (
        "FINDING: an interrupted compaction (crash before the atomic "
        "replace) altered the on-disk records.tsv -- compaction is "
        "destructive-before-commit."
    )

    # 3. a normal read still returns the correct pre-compaction live set.
    _clear_caches()
    live_during_outage = {
        r["id"]: r
        for r in object_records.read_collection_records("orders", base_dir=data_dir, roots=[])
    }
    assert live_during_outage == live_before

    # 4. retry (failure no longer armed) succeeds and recovers correctly.
    monkeypatch.setattr(Path, "replace", real_replace)
    result = object_records.compact_collection("orders", base_dir=data_dir, roots=[])
    assert result["rows_after"] == 1

    _clear_caches()
    live_after_retry = {
        r["id"]: r
        for r in object_records.read_collection_records("orders", base_dir=data_dir, roots=[])
    }
    assert live_after_retry == live_before


def test_compaction_stray_orphaned_temp_file_is_inert_and_not_resurrected(tmp_path):
    """A DIFFERENT crash shape: a previous compaction (or previous process
    incarnation, different pid) died after creating its temp file but
    before either writing to it fully or replacing the original -- leaving
    an orphaned `.records.tsv.<pid>.<tid>.tmp` file sitting next to the
    real records.tsv forever (nothing in this codebase's write path scans
    for or cleans up a stale temp file from a DIFFERENT pid/tid on
    startup). Characterizes: does that debris get accidentally treated as
    data by any read path, and does a subsequent real compaction still
    produce the correct result despite the debris being present?"""
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "invoices", [ID_FIELD, NOTE_FIELD])
    object_records.create_collection_record(
        "invoices", {"id": "i1", "note": "one"}, base_dir=data_dir, roots=[]
    )
    object_records.update_collection_record(
        "invoices", "i1", {"note": "one-v2"}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "invoices", {"id": "i2", "note": "two"}, base_dir=data_dir, roots=[]
    )

    path = _records_path(data_dir, "invoices")
    original_bytes = path.read_bytes()

    # Plant debris matching the real naming convention but with a bogus
    # pid/tid, containing garbage that -- if it were ever misread as real
    # data -- would be obviously wrong.
    stray = path.with_name(f".{path.name}.999999.999999.tmp")
    stray.write_text("GARBAGE-NOT-A-VALID-TSV-ROW\t\t\n")
    assert stray.exists()

    _clear_caches()
    listing = object_records.list_collection_records("invoices", base_dir=data_dir, roots=[])["records"]
    assert listing == [{"id": "i1", "note": "one-v2"}, {"id": "i2", "note": "two"}], (
        "a stray orphaned .tmp file was picked up by a read path"
    )
    assert path.read_bytes() == original_bytes  # debris presence didn't touch the real file

    result = object_records.compact_collection("invoices", base_dir=data_dir, roots=[])
    assert result["rows_after"] == 2

    _clear_caches()
    listing_after = object_records.list_collection_records("invoices", base_dir=data_dir, roots=[])["records"]
    assert listing_after == [{"id": "i1", "note": "one-v2"}, {"id": "i2", "note": "two"}]

    # Report (not asserted pass/fail either way -- purely descriptive):
    # does a real compaction clean up debris left by an unrelated pid/tid?
    print(f"\n[compaction] stray orphaned temp file still present after a real "
          f"compact_collection() call: {stray.exists()}")


# =============================================================================
# 3. BACKUP / RESTORE FIDELITY -- byte-exact round trip
# =============================================================================


def _make_source_runtime(base: Path) -> tuple[Path, Path]:
    """Build a runtime layout (objects_dir + data_dir) with one classic-mode
    and one append-mode collection, both carrying the full exotic corpus,
    plus superseded/tombstoned rows in the append one (so the backed-up
    file is a realistic append-log, not just a clean fold)."""
    objects_dir = base / "objects"
    data_dir = base / "data"
    objects_dir.mkdir(parents=True, exist_ok=True)
    (objects_dir / "keepme.txt").write_text("just to give the objects tree a file\n")

    write_schema(data_dir, "notes_classic", [ID_FIELD, NOTE_FIELD, LINES_FIELD])
    write_append_schema(data_dir, "notes_append", [ID_FIELD, NOTE_FIELD, LINES_FIELD])

    corpus = _exotic_corpus()
    for rec in corpus:
        object_records.create_collection_record("notes_classic", dict(rec), base_dir=data_dir, roots=[])
        object_records.create_collection_record("notes_append", dict(rec), base_dir=data_dir, roots=[])

    # Give the append collection some churn so its physical file is a real
    # log (superseded rows + a tombstone), not already-compacted.
    object_records.update_collection_record(
        "notes_append", "r-ascii", {"note": "ascii v2"}, base_dir=data_dir, roots=[]
    )
    object_records.update_collection_record(
        "notes_append", "r-ascii", {"note": "ascii v3"}, base_dir=data_dir, roots=[]
    )
    object_records.delete_collection_record("notes_append", "r-bare-cr", base_dir=data_dir, roots=[])

    return objects_dir, data_dir


def test_backup_restore_byte_exact_round_trip_full_exotic_corpus(tmp_path):
    """Back up a runtime carrying the full exotic corpus in both classic
    and append storage, restore into a FRESH directory, and assert:

      - every collection's records.tsv is byte-for-byte identical,
        original vs. restored (the strongest possible fidelity check --
        the backup mechanism is a raw tar copy, so this should hold
        trivially, but is worth confirming rather than assumed)
      - every collection's OBJECTS-DIR sibling file round-trips too
      - every record, read back through the real object_records API
        against the restored data_dir, matches the original exactly
      - the restored append-mode file is still a VALID, parseable
        append-log (correct live/dead fold), not just byte-identical
    """
    src_root = tmp_path / "src"
    objects_dir, data_dir = _make_source_runtime(src_root)

    backup_output = tmp_path / "backups" / "full.tar.gz"
    summary = object_backup.create_runtime_backup(
        backup_output, objects_dir=objects_dir, data_dir=data_dir
    )
    assert summary.files > 0
    assert backup_output.exists()

    restore_root = tmp_path / "restored"
    restored_objects = restore_root / "objects"
    restored_data = restore_root / "data"
    restore_summary = object_backup.restore_runtime_backup(
        backup_output,
        objects_dir=restored_objects,
        data_dir=restored_data,
        overwrite=True,
    )
    assert restore_summary.files == summary.files

    # --- byte-exact records.tsv round trip, both storage modes ---
    for collection in ("notes_classic", "notes_append"):
        original = _records_path(data_dir, collection).read_bytes()
        restored = _records_path(restored_data, collection).read_bytes()
        assert restored == original, (
            f"{collection}: restored records.tsv is not byte-identical to the original"
        )

    # --- objects dir sibling file round trip ---
    assert (restored_objects / "keepme.txt").read_bytes() == (objects_dir / "keepme.txt").read_bytes()

    # --- every record round-trips through the real API against the
    #     RESTORED data dir, both modes ---
    for collection in ("notes_classic", "notes_append"):
        _clear_caches()
        original_live = {
            r["id"]: r
            for r in object_records.read_collection_records(collection, base_dir=data_dir, roots=[])
        }
        _clear_caches()
        restored_live = {
            r["id"]: r
            for r in object_records.read_collection_records(
                collection, base_dir=restored_data, roots=[]
            )
        }
        assert restored_live == original_live, (
            f"{collection}: restored collection's live records diverge from the original"
        )
        # spot-check the specific hostile fields survived exactly
        assert restored_live["r-multibyte"]["note"] == original_live["r-multibyte"]["note"]
        assert restored_live["r-emoji"]["note"] == original_live["r-emoji"]["note"]
        assert restored_live["r-bare-cr" if "r-bare-cr" in restored_live else "r-ascii"]
        assert json.loads(restored_live["r-embedded-json-pretty"]["lines"]) == json.loads(
            original_live["r-embedded-json-pretty"]["lines"]
        )

    # append-mode collection: confirm the churn (superseded update, delete)
    # survived the round trip with correct fold semantics, not just as
    # opaque bytes.
    _clear_caches()
    restored_append_live = {
        r["id"]: r
        for r in object_records.read_collection_records(
            "notes_append", base_dir=restored_data, roots=[]
        )
    }
    assert restored_append_live["r-ascii"]["note"] == "ascii v3"
    assert "r-bare-cr" not in restored_append_live

    # by-id reads against the restored dir work too (sidecar builds fresh
    # against the restored file).
    for record_id in restored_append_live:
        _clear_caches()
        by_id = object_records.get_collection_record(
            "notes_append", record_id, base_dir=restored_data, roots=[]
        )
        assert by_id == restored_append_live[record_id]


def test_backup_restore_round_trip_survives_a_compacted_append_collection_too(tmp_path):
    """Same round-trip check, but compact the append-mode collection BEFORE
    backing it up -- confirms fidelity holds for the already-folded
    physical form too, not just the raw log form."""
    src_root = tmp_path / "src"
    objects_dir, data_dir = _make_source_runtime(src_root)
    object_records.compact_collection("notes_append", base_dir=data_dir, roots=[])

    backup_output = tmp_path / "backups" / "compacted.tar.gz"
    object_backup.create_runtime_backup(backup_output, objects_dir=objects_dir, data_dir=data_dir)

    restore_root = tmp_path / "restored"
    restored_data = restore_root / "data"
    object_backup.restore_runtime_backup(
        backup_output,
        objects_dir=restore_root / "objects",
        data_dir=restored_data,
        overwrite=True,
    )

    original_bytes = _records_path(data_dir, "notes_append").read_bytes()
    restored_bytes = _records_path(restored_data, "notes_append").read_bytes()
    assert restored_bytes == original_bytes

    _clear_caches()
    original_live = {
        r["id"]: r
        for r in object_records.read_collection_records("notes_append", base_dir=data_dir, roots=[])
    }
    _clear_caches()
    restored_live = {
        r["id"]: r
        for r in object_records.read_collection_records(
            "notes_append", base_dir=restored_data, roots=[]
        )
    }
    assert restored_live == original_live


def test_restored_tsv_files_are_valid_and_equal_on_disk(tmp_path):
    """Direct on-disk inspection (not just through the API): every restored
    .tsv file must (a) exist, (b) be byte-equal to its source, and (c)
    independently parse via plain csv.reader into the same physical rows
    as the source -- i.e. the restore didn't quietly reinterpret/re-encode
    line endings, quoting, or the tab dialect along the way."""
    src_root = tmp_path / "src"
    objects_dir, data_dir = _make_source_runtime(src_root)

    backup_output = tmp_path / "backups" / "b.tar.gz"
    object_backup.create_runtime_backup(backup_output, objects_dir=objects_dir, data_dir=data_dir)

    restored_data = tmp_path / "restored" / "data"
    object_backup.restore_runtime_backup(
        backup_output,
        objects_dir=tmp_path / "restored" / "objects",
        data_dir=restored_data,
        overwrite=True,
    )

    for collection in ("notes_classic", "notes_append"):
        src_path = _records_path(data_dir, collection)
        dst_path = _records_path(restored_data, collection)
        assert dst_path.is_file()
        assert dst_path.read_bytes() == src_path.read_bytes()

        with src_path.open(newline="") as fh:
            src_rows = list(csv.reader(fh, delimiter="\t"))
        with dst_path.open(newline="") as fh:
            dst_rows = list(csv.reader(fh, delimiter="\t"))
        assert dst_rows == src_rows, f"{collection}: restored .tsv parses to different physical rows"


# =============================================================================
# 4. object_backup_index's OWN quote-blind torn-tail copy, against a backed
#    up file
# =============================================================================


def test_backup_index_parses_a_well_formed_multiline_cell_in_a_complete_backed_up_file_correctly(
    tmp_path,
):
    """Baseline (expected-safe): a COMPLETE, properly-terminated
    records.tsv whose last physical row happens to hold a pretty-printed
    (real-raw-newline-bearing) JSON blob -- backed up, then read via
    object_backup_index._parse_tsv_by_id (the function preview_collection/
    preview_record use). Since the file genuinely ends with "\\n" (nothing
    was interrupted), object_backup_index's torn-tail check should be a
    correct no-op here, exactly like object_records's copy is in the
    equivalent ordinary case."""
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "invoices", [ID_FIELD, LINES_FIELD])
    object_records.create_collection_record(
        "invoices", {"id": "A", "lines": json.dumps([{"sku": "A-1"}])}, base_dir=data_dir, roots=[]
    )
    pretty_blob = json.dumps([{"sku": "B-1", "qty": 1}, {"sku": "B-2", "qty": 2}], indent=2)
    object_records.create_collection_record(
        "invoices", {"id": "B", "lines": pretty_blob}, base_dir=data_dir, roots=[]
    )

    path = _records_path(data_dir, "invoices")
    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") > 3  # genuinely multi-physical-line file

    parsed = object_backup_index._parse_tsv_by_id(raw)
    assert set(parsed) == {"A", "B"}
    assert json.loads(parsed["B"]["lines"]) == [{"sku": "B-1", "qty": 1}, {"sku": "B-2", "qty": 2}]


def test_backup_index_torn_tail_is_also_quote_blind_on_a_backed_up_file_FINDING(tmp_path):
    """FINDING: object_backup_index._drop_torn_tail (used by
    _parse_append_tsv_by_id / _parse_tsv_by_id, which back
    preview_collection and preview_record) has the SAME "does the file end
    with a literal \\n byte" check as object_records._drop_torn_tail /
    _repair_torn_tail's documented substrate bug #2 -- and is independently
    reimplemented rather than shared, so a fix to one does not fix the
    other. If a records.tsv is captured into a backup while genuinely torn
    (a crash/interrupted write mid multi-line quoted field, landing right
    after one of the field's OWN internal newlines -- see
    tests/test_embedded_json_lines_characterization.py's
    ...mid_multiline_row_is_silently_resurrected_and_cascades_FINDING for
    the live-side version of this), the archived copy carries the exact
    same torn bytes, and object_backup_index's preview/diff machinery
    reads the incomplete row back as if it were a genuine, committed
    record -- corrupting the *preview*, not the raw backup bytes
    themselves (the tar archive itself is unaffected; this is a read-side
    misparse of an already-flawed live file, both at backup time and at
    restore-and-reread time).

    Reproduced directly against object_backup_index._parse_tsv_by_id (the
    same function preview_collection/preview_record call on archived
    bytes), mirroring the live-side FINDING's construction: a physical row
    for id "C" is built with the real csv dialect this module writes with,
    then cut right after its SECOND physical line -- the cut point itself
    lands on "\\n", so the fragment "looks" terminated by the naive check
    even though the quote never closed.
    """
    header = ["_op", "id", "lines"]
    hostile_blob = json.dumps(
        [{"sku": "C-1", "note": "hostile é日本 \"q\" \\z"}, {"sku": "C-2"}], indent=2
    )

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    writer.writerow(["", "A", json.dumps([{"sku": "A-1"}])])
    writer.writerow(["", "B", json.dumps([{"sku": "B-1"}])])
    full_text_before_c = buf.getvalue()

    buf2 = io.StringIO()
    writer2 = csv.writer(buf2, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer2.writerow(["", "C", hostile_blob])
    full_row_c = buf2.getvalue()
    assert full_row_c.count("\n") > 3  # genuinely spans several physical lines

    first_nl = full_row_c.index("\n")
    second_nl = full_row_c.index("\n", first_nl + 1)
    torn_fragment = full_row_c[: second_nl + 1]
    assert torn_fragment.endswith("\n")  # looks "committed" by the naive check

    # This is the byte sequence that would end up inside a backup archive
    # if the live file were captured (or itself crashed) at exactly this
    # unlucky boundary.
    backed_up_text = full_text_before_c + torn_fragment
    assert backed_up_text.endswith("\n")  # passes the naive "not torn" check despite being torn
    backed_up_raw = backed_up_text.encode("utf-8")

    parsed = object_backup_index._parse_tsv_by_id(backed_up_raw)

    ids_seen = set(parsed)
    finding_reproduced = "C" in ids_seen
    print(f"\n[backup-index torn-tail FINDING] ids parsed from the torn backed-up file: {sorted(ids_seen)!r}")
    if finding_reproduced:
        print(f"  record C's (corrupted/incomplete) parsed value: {parsed['C']!r}")

    assert finding_reproduced, (
        "FINDING confirmed: object_backup_index._parse_tsv_by_id resurrected "
        f"a torn mid-multiline-row as a committed record. Parsed ids: {sorted(ids_seen)!r}"
    )
    # And, mirroring the live-side finding: C's value is garbled (only the
    # fragment up through its second physical line), not the real row.
    try:
        recovered = json.loads(parsed["C"]["lines"])
    except json.JSONDecodeError:
        recovered = None
    assert recovered != [{"sku": "C-1", "note": "hostile é日本 \"q\" \\z"}, {"sku": "C-2"}], (
        "if this ever starts parsing to the FULL correct value, the finding "
        "no longer reproduces and this test should be converted to a "
        "regression guard (see the sibling xfail test's pattern)"
    )


def test_backup_index_preview_collection_end_to_end_against_a_torn_archived_file(tmp_path):
    """Same FINDING, exercised end-to-end through the real public entry
    point (preview_collection reading an actual .tar.gz archive member)
    rather than calling the private parser directly -- confirms the bug is
    reachable from the documented API surface, not just the internal
    helper."""
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "invoices", [ID_FIELD, LINES_FIELD])
    object_records.create_collection_record(
        "invoices", {"id": "A", "lines": json.dumps([{"sku": "A-1"}])}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "invoices", {"id": "B", "lines": json.dumps([{"sku": "B-1"}])}, base_dir=data_dir, roots=[]
    )
    path = _records_path(data_dir, "invoices")
    full_text = path.read_text()
    assert full_text.endswith("\n")

    hostile_blob = json.dumps([{"sku": "C-1"}, {"sku": "C-2"}], indent=2)
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(["", "C", hostile_blob])
    full_row_text = buf.getvalue()
    first_nl = full_row_text.index("\n")
    second_nl = full_row_text.index("\n", first_nl + 1)
    torn_fragment = full_row_text[: second_nl + 1]

    # Simulate: the live file itself crashed at this exact unlucky
    # boundary (matches the live-side FINDING scenario), and a backup was
    # then taken of the runtime in that state.
    path.write_text(full_text + torn_fragment)
    assert path.read_text().endswith("\n")

    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()

    # preview_collection resolves the backup id under
    # backups_dir(data_dir) = <data_dir>/backups, and separately diffs
    # against the LIVE records.tsv under that SAME data_dir. To make the
    # finding legible in the diff output (rather than backup and "live"
    # both showing the identical torn bytes and folding identically), the
    # archive is placed under a distinct "server_data" dir whose own
    # collections/invoices does not exist -- so the diff's "added" list is
    # purely "what preview_collection believes the backup contains".
    server_data_dir = tmp_path / "server_data"
    backup_output = server_data_dir / "backups" / "torn.tar.gz"
    object_backup.create_runtime_backup(backup_output, objects_dir=objects_dir, data_dir=data_dir)

    result = object_backup_index.preview_collection(
        "torn.tar.gz", "invoices", data_dir=server_data_dir
    )

    print(f"\n[backup-index torn-tail FINDING, end-to-end] preview_collection "
          f"added={result['added']!r}")

    assert "A" in result["added"] and "B" in result["added"]
    assert "C" in result["added"], (
        "FINDING confirmed: preview_collection (the real public API atop "
        "object_backup_index._parse_tsv_by_id) reports a torn mid-multiline "
        f"row as a normal, present-in-backup record. Full diff: {result!r}"
    )

