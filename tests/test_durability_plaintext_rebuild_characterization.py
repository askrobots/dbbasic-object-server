"""CHARACTERIZATION tests proving (or falsifying) the GOVERNING PRINCIPLE the
plain-text-durability pitch rests on:

    The canonical ``records.tsv`` file is the SOLE source of truth. Every
    accelerator built on top of it -- the id->offset sidecar
    (``.records.oidx``), the in-process ``_RECORDS_CACHE`` / ``_OIDX_CACHE``,
    and (for derived collections) folded/aggregated views -- is DERIVED and
    REBUILDABLE from the TSV, never authoritative. Delete every accelerator
    and you lose *speed*, never a single byte of data. This is the "delete
    our indexes, your data is still all there in files you can read" proof:
    the contract-winning demo (see plan/database-test-strategy.md, gitignored).

This file is pure characterization of EXISTING behavior. It does not modify
any production module (object_records.py, object_stock.py, etc.) and does
not touch packages/. Where a probe reveals the principle does NOT hold for
some accelerator, the assertion is written to state the correctness property
the durability pitch REQUIRES, and is left to fail (or is reported via a
soft check with a printed finding) rather than "fixed" by weakening it.

Layout under test (read from object_records.py's own module docstring/
comments, not guessed):
  - ``data/collections/{collection}/records.tsv`` -- the canonical file.
    Header row 1 names every logical field (self-describing); for
    append-storage collections the physical header additionally carries a
    leading ``_op`` column (values ``""`` = upsert, ``"del"`` = tombstone).
  - ``data/collections/{collection}/.records.oidx`` -- the id->byte-offset
    sidecar (append mode only). A dotfile, invisible to every collection
    glob and to backup. Format: header line ``"oidx1\\t<data_ino>"`` then
    one ``"<row_start>\\t<row_end>\\t<op>\\t<id>"`` line per indexed physical
    row. 100% disposable per object_records.py's own comment: "any reader
    unable to make sense of it ... rebuilds it with one sequential scan of
    records.tsv rather than raising."
  - ``object_records._RECORDS_CACHE`` / ``._OIDX_CACHE`` -- in-process,
    module-level dicts. Not on disk at all; trivially disposable by
    definition, but exercised here anyway so the "cold reopen" path is
    genuinely cold, matching how a fresh process would see the world.

Four probes, each reported as PRINCIPLE HOLDS or PRINCIPLE VIOLATED:
  1. REBUILD-FROM-TSV-ALONE
  2. NO-HIDDEN-STATE
  3. READ-WITH-STANDARD-TOOLS
  4. ARCHIVAL round-trip (naive external reader, zero DBBASIC code)
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

import object_records

pytestmark = pytest.mark.conformance


ID_FIELD = {"name": "id"}
AMOUNT_FIELD = {"name": "amount_cents"}
NOTE_FIELD = {"name": "note", "type": "textarea"}
CRLF_FIELD = {"name": "crlf_field", "type": "textarea"}
BARE_CR_FIELD = {"name": "bare_cr_field", "type": "textarea"}
COMMAS_FIELD = {"name": "commas_field"}
EMPTY_FIELD = {"name": "empty_field"}

ALL_FIELDS = [
    ID_FIELD,
    AMOUNT_FIELD,
    NOTE_FIELD,
    CRLF_FIELD,
    BARE_CR_FIELD,
    COMMAS_FIELD,
    EMPTY_FIELD,
]
LOGICAL_FIELD_NAMES = [f["name"] for f in ALL_FIELDS]  # excludes "_op"


# --- setup helpers (mirror tests/test_embedded_json_lines_characterization.py) --


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
    """Cold every in-process accelerator: a fresh _RECORDS_CACHE +
    _OIDX_CACHE is what forces the NEXT read to actually go back to disk
    (records.tsv, and -- for append mode -- rebuild/reconsult the
    sidecar) instead of serving a warm hit. This is the in-memory half of
    "delete every accelerator"; the on-disk half is unlinking
    .records.oidx, done explicitly in each test below."""
    object_records._RECORDS_CACHE.clear()
    object_records._OIDX_CACHE.clear()


def _exotic_payload(suffix: str, amount: int) -> dict:
    """A record whose values are deliberately hostile to a naive delimiter
    scan: literal TAB, NEWLINE, double-quote, backslash, Unicode, CRLF, a
    BARE CR (no paired LF -- the specific case the task's own note flags:
    "Bare-CR rows are now quoted, so standard csv parsing handles them"),
    embedded commas (harmless under a TAB delimiter, but a nice archival
    sanity check), and a genuinely empty field."""
    return {
        "id": f"rec-{suffix}",
        "amount_cents": str(amount),
        "note": f"café \t x\n y \"q\" \\z 日本 #{suffix}",
        "crlf_field": f"alpha-{suffix}\r\nbeta-{suffix}",
        "bare_cr_field": f"before-{suffix}\rafter-{suffix}",
        "commas_field": f"a,b,c,{suffix}",
        "empty_field": "",
    }


def _records_tsv_path(data_dir: Path, collection: str) -> Path:
    return object_records.collection_records_file(collection, base_dir=data_dir)


def _oidx_path(data_dir: Path, collection: str) -> Path:
    return object_records._oidx_path(_records_tsv_path(data_dir, collection))


def _collection_dir_files(data_dir: Path, collection: str) -> list[Path]:
    coll_dir = _records_tsv_path(data_dir, collection).parent
    if not coll_dir.exists():
        return []
    return sorted(p for p in coll_dir.iterdir() if p.is_file())


# =============================================================================
# 1. REBUILD-FROM-TSV-ALONE
# =============================================================================


def test_rebuild_from_tsv_alone_classic_mode(tmp_path):
    """Classic-mode collection: no sidecar is ever built for classic mode
    (only append-storage collections use .records.oidx) -- so this probe's
    job here is narrower: prove that clearing the in-process caches alone
    (the only "accelerator" classic mode has) still yields byte-identical
    reads. Included primarily as the control case for the append-mode test
    right below it."""
    data_dir = tmp_path / "data"
    collection = "widgets_classic"
    write_schema(data_dir, collection, ALL_FIELDS)

    records = [_exotic_payload(str(i), 1000 + i) for i in range(8)]
    for rec in records:
        object_records.create_collection_record(collection, rec, base_dir=data_dir, roots=[])

    path = _records_tsv_path(data_dir, collection)
    tsv_bytes_before = path.read_bytes()

    all_before = object_records.read_collection_records(collection, base_dir=data_dir, roots=[])
    by_id_before = {
        rec["id"]: object_records.get_collection_record(
            collection, rec["id"], base_dir=data_dir, roots=[]
        )
        for rec in records[::2]
    }

    # Delete every accelerator this mode has (no on-disk sidecar exists for
    # classic mode, but assert that explicitly rather than assume it).
    sidecar = _oidx_path(data_dir, collection)
    assert not sidecar.exists(), "classic mode unexpectedly produced an oidx sidecar"
    _clear_caches()

    tsv_bytes_after = path.read_bytes()
    all_after = object_records.read_collection_records(collection, base_dir=data_dir, roots=[])
    by_id_after = {
        rec["id"]: object_records.get_collection_record(
            collection, rec["id"], base_dir=data_dir, roots=[]
        )
        for rec in records[::2]
    }

    assert tsv_bytes_after == tsv_bytes_before, "PRINCIPLE VIOLATED: records.tsv mutated by a mere read"
    assert all_after == all_before, "PRINCIPLE VIOLATED: cold reread diverges from original in classic mode"
    assert by_id_after == by_id_before, "PRINCIPLE VIOLATED: cold by-id reread diverges in classic mode"
    print("\n[REBUILD-FROM-TSV-ALONE / classic] PRINCIPLE HOLDS: cold reread byte-identical.")


def test_rebuild_from_tsv_alone_append_mode_with_updates_and_deletes(tmp_path, monkeypatch):
    """The real test of the sidecar's disposability: an append-mode
    collection with creates, an UPDATE (superseding physical row) and a
    DELETE (tombstone row) already on disk, plus exotic content throughout.
    DBBASIC_RECORDS_CACHE_MAX_ROWS=0 forces every read through the
    id->offset sidecar rather than the ordinary records cache (same trick
    the embedded-JSON characterization file uses), so this genuinely
    exercises the accelerator being proven disposable, not a shortcut
    around it.

    Sequence: capture the full golden dataset (fold + several by-id reads),
    THEN physically delete .records.oidx from disk and clear every
    in-process cache WITHOUT touching records.tsv, THEN reread and compare.
    """
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    collection = "widgets_append"
    write_append_schema(data_dir, collection, ALL_FIELDS)

    records = [_exotic_payload(str(i), 2000 + i) for i in range(10)]
    for rec in records:
        object_records.create_collection_record(collection, rec, base_dir=data_dir, roots=[])

    # An UPDATE (a second, superseding physical row for the same id) and a
    # DELETE (a tombstone row) -- both must fold correctly from a from-
    # scratch sidecar rebuild, not just from a freshly-built one.
    object_records.update_collection_record(
        collection, "rec-3", {"note": "UPDATED note, still hostile: \t\n\"\\ 日本"},
        base_dir=data_dir, roots=[],
    )
    object_records.delete_collection_record(collection, "rec-7", base_dir=data_dir, roots=[])

    path = _records_tsv_path(data_dir, collection)
    sidecar = _oidx_path(data_dir, collection)

    _clear_caches()
    tsv_bytes_before = path.read_bytes()
    all_before = object_records.read_collection_records(collection, base_dir=data_dir, roots=[])
    surviving_ids = [r["id"] for r in all_before]
    assert "rec-7" not in surviving_ids  # delete really took
    assert len(all_before) == 9

    _clear_caches()
    by_id_before = {
        rid: object_records.get_collection_record(collection, rid, base_dir=data_dir, roots=[])
        for rid in surviving_ids
    }
    assert "UPDATED note" in by_id_before["rec-3"]["note"]

    # --- delete EVERY accelerator: the on-disk sidecar AND every in-process
    # cache -- without touching records.tsv at all. ---
    assert sidecar.exists(), "expected the sidecar to have been built by the reads above"
    sidecar.unlink()
    _clear_caches()

    tsv_bytes_after_delete = path.read_bytes()
    assert tsv_bytes_after_delete == tsv_bytes_before, (
        "PRINCIPLE VIOLATED: deleting the sidecar + clearing caches touched records.tsv"
    )
    assert not sidecar.exists()

    # Reopen/reread with everything cold: the sidecar must be silently,
    # transparently rebuilt from records.tsv alone.
    all_after = object_records.read_collection_records(collection, base_dir=data_dir, roots=[])
    assert all_after == all_before, "PRINCIPLE VIOLATED: fold-all diverges after sidecar deletion"

    _clear_caches()
    by_id_after = {
        rid: object_records.get_collection_record(collection, rid, base_dir=data_dir, roots=[])
        for rid in surviving_ids
    }
    assert by_id_after == by_id_before, (
        "PRINCIPLE VIOLATED: by-id reads diverge after sidecar deletion -- the sidecar was load-bearing"
    )

    # records.tsv itself must still be byte-for-byte what it was -- the
    # rebuild is allowed to (and does) recreate .records.oidx as a
    # *side effect* of a read, but must never touch the canonical file.
    tsv_bytes_final = path.read_bytes()
    assert tsv_bytes_final == tsv_bytes_before, (
        "PRINCIPLE VIOLATED: records.tsv changed as a side effect of rebuilding the sidecar"
    )

    print(
        "\n[REBUILD-FROM-TSV-ALONE / append, with update+delete] PRINCIPLE HOLDS: "
        "after unlinking .records.oidx and clearing _RECORDS_CACHE/_OIDX_CACHE, "
        "read_collection_records and every by-id get_collection_record reproduced "
        "the pre-deletion dataset exactly, and records.tsv's bytes never changed."
    )
    if sidecar.exists():
        print("  (sidecar was transparently rebuilt on disk as a read side effect -- expected/fine.)")


# =============================================================================
# 2. NO-HIDDEN-STATE
# =============================================================================


def test_no_hidden_state_only_records_tsv_is_load_bearing(tmp_path, monkeypatch):
    """Enumerate every file that exists for a collection, then delete
    EVERYTHING except records.tsv (not just the sidecar -- whatever else
    is sitting in the collection directory) and confirm the full dataset
    is still completely intact. If any byte of truth turned out to live
    only in a non-.tsv file, this is where it would show up as a genuine
    data loss (missing/wrong records), not just a slow rebuild."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    collection = "no_hidden_state"
    write_append_schema(data_dir, collection, ALL_FIELDS)

    records = [_exotic_payload(str(i), 3000 + i) for i in range(6)]
    for rec in records:
        object_records.create_collection_record(collection, rec, base_dir=data_dir, roots=[])
    object_records.update_collection_record(
        collection, "rec-1", {"amount_cents": "999999"}, base_dir=data_dir, roots=[]
    )
    object_records.delete_collection_record(collection, "rec-4", base_dir=data_dir, roots=[])

    _clear_caches()
    golden = object_records.read_collection_records(collection, base_dir=data_dir, roots=[])
    golden_ids = sorted(r["id"] for r in golden)
    assert golden_ids == ["rec-0", "rec-1", "rec-2", "rec-3", "rec-5"]

    path = _records_tsv_path(data_dir, collection)
    files_before = _collection_dir_files(data_dir, collection)
    print(f"\n[NO-HIDDEN-STATE] files present for '{collection}' before cleanup: "
          f"{[p.name for p in files_before]}")
    assert path in files_before

    # Delete every file in the collection directory EXCEPT records.tsv.
    for f in files_before:
        if f != path:
            f.unlink()
    _clear_caches()

    files_remaining = _collection_dir_files(data_dir, collection)
    assert files_remaining == [path], (
        f"expected only records.tsv to remain, found {[p.name for p in files_remaining]}"
    )

    # The dataset must be fully intact reading from records.tsv alone.
    reread = object_records.read_collection_records(collection, base_dir=data_dir, roots=[])
    assert sorted(r["id"] for r in reread) == golden_ids
    assert {r["id"]: r for r in reread} == {r["id"]: r for r in golden}

    _clear_caches()
    for rid in golden_ids:
        by_id = object_records.get_collection_record(collection, rid, base_dir=data_dir, roots=[])
        expected = next(r for r in golden if r["id"] == rid)
        assert by_id == expected

    print(
        "[NO-HIDDEN-STATE] PRINCIPLE HOLDS: after deleting every file except "
        "records.tsv, the complete dataset (including the prior update and "
        "delete) is intact via every read path -- no byte of truth lives "
        "outside the .tsv file."
    )


# =============================================================================
# 3. READ-WITH-STANDARD-TOOLS
# =============================================================================


def test_read_with_standard_tools_self_describing_and_csv_parseable(tmp_path):
    """The archivist-in-2125 test: open records.tsv with nothing but
    Python's stdlib `csv` module (delimiter='\\t'), independent of
    object_records, and confirm (a) the header row names every column
    (self-describing -- no external schema needed to know what a column
    IS, even though a schema file separately governs validation), and (b)
    every data row parses cleanly and reconstructs the exact field values,
    for both classic and append storage."""
    data_dir = tmp_path / "data"

    write_schema(data_dir, "std_classic", ALL_FIELDS)
    write_append_schema(data_dir, "std_append", ALL_FIELDS)

    records = [_exotic_payload(str(i), 4000 + i) for i in range(5)]
    for collection in ("std_classic", "std_append"):
        for rec in records:
            object_records.create_collection_record(collection, rec, base_dir=data_dir, roots=[])

    for collection, expect_op_column in (("std_classic", False), ("std_append", True)):
        path = _records_tsv_path(data_dir, collection)
        with path.open(newline="") as handle:
            reader = csv.reader(handle, delimiter="\t")
            rows = list(reader)

        header, data_rows = rows[0], rows[1:]
        print(f"\n[READ-WITH-STANDARD-TOOLS / {collection}] header: {header}")

        if expect_op_column:
            assert header[0] == "_op"
            logical_header = header[1:]
        else:
            logical_header = header

        # Self-describing: the header names exactly the fields we wrote
        # (order-independent check on set membership, order-sensitive on
        # "id" being present at all).
        assert set(logical_header) == set(LOGICAL_FIELD_NAMES), (
            f"{collection}: header does not name the columns we wrote: {logical_header!r}"
        )
        assert len(data_rows) == len(records), (
            f"{collection}: expected {len(records)} data rows, got {len(data_rows)}"
        )

        recovered_by_id = {}
        for row in data_rows:
            assert len(row) == len(header), (
                f"{collection}: row does not match header column count: {row!r} vs {header!r}"
            )
            as_dict = dict(zip(header, row))
            if expect_op_column:
                assert as_dict.pop("_op") == ""  # every row here is a plain upsert
            recovered_by_id[as_dict["id"]] = as_dict

        for rec in records:
            assert recovered_by_id[rec["id"]] == rec, (
                f"{collection}: naive csv.reader recovery of {rec['id']!r} does not match "
                f"what was written.\n  wrote:     {rec!r}\n  recovered: {recovered_by_id[rec['id']]!r}"
            )

    print(
        "\n[READ-WITH-STANDARD-TOOLS] PRINCIPLE HOLDS: both classic- and "
        "append-mode records.tsv files are well-formed tab-delimited CSV "
        "with a self-describing header, and every exotic-content field "
        "(tab/newline/quote/backslash/Unicode/CRLF/bare-CR/commas/empty) "
        "recovers exactly via a plain csv.reader -- no object_records code "
        "involved in this parse."
    )


# =============================================================================
# 4. ARCHIVAL round-trip -- fully naive, zero DBBASIC code on the READ side
# =============================================================================


def _naive_read_classic_tsv(path: Path) -> list[dict]:
    """A hand-rolled reader that owes NOTHING to object_records: stdlib
    `csv` only, using the header row itself to know the field names (the
    only "schema" this function is given). This is what an archivist with
    no access to this codebase, in some future year, would write in an
    afternoon to recover the data."""
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        return list(reader)


def _naive_read_append_tsv_with_manual_fold(path: Path) -> dict[str, dict]:
    """Same as above, but additionally applies the append-only op
    semantics documented in plain English in object_records.py's own
    comments -- op "" upserts, op "del" tombstones, last physical row for
    an id wins -- reimplemented here with no reference to any
    object_records internals, to show even append-mode data does not
    require DBBASIC's own code to recover, only reading the two
    human-documented op values in order."""
    live: dict[str, dict] = {}
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for row in reader:
            op = row.pop("_op")
            rid = row["id"]
            if op == "del":
                live.pop(rid, None)
            else:
                live[rid] = row
    return live


def test_archival_round_trip_naive_reader_classic_mode(tmp_path):
    """Write via object_records (realistic production write path), then
    read back with ONLY the naive external reader above -- zero calls to
    object_records for the read side."""
    data_dir = tmp_path / "data"
    collection = "archival_classic"
    write_schema(data_dir, collection, ALL_FIELDS)

    records = [_exotic_payload(str(i), 5000 + i) for i in range(7)]
    for rec in records:
        object_records.create_collection_record(collection, rec, base_dir=data_dir, roots=[])

    path = _records_tsv_path(data_dir, collection)
    recovered = _naive_read_classic_tsv(path)
    recovered_by_id = {r["id"]: r for r in recovered}

    assert len(recovered) == len(records)
    for rec in records:
        assert recovered_by_id[rec["id"]] == rec, (
            f"naive external round-trip failed for {rec['id']!r}: "
            f"wrote {rec!r}, naive-recovered {recovered_by_id[rec['id']]!r}"
        )

    print(
        "\n[ARCHIVAL round-trip / classic] PRINCIPLE HOLDS: a csv.DictReader "
        "with zero DBBASIC code recovers every record exactly, including "
        "every hostile-content field."
    )


def test_archival_round_trip_naive_reader_append_mode_with_update_and_delete(tmp_path):
    """The stronger version: append-mode file, with a real update
    (superseding row) and a real delete (tombstone) baked in, recovered
    end-to-end by a hand-rolled fold that uses nothing but the two
    documented op values -- proving the append-only format itself needs no
    proprietary reader, just the (short, human-readable) semantics
    documented in object_records.py's own comments."""
    data_dir = tmp_path / "data"
    collection = "archival_append"
    write_append_schema(data_dir, collection, ALL_FIELDS)

    records = [_exotic_payload(str(i), 6000 + i) for i in range(6)]
    for rec in records:
        object_records.create_collection_record(collection, rec, base_dir=data_dir, roots=[])

    updated_note = "ARCHIVAL-UPDATED: café \t\n\"\\ 日本"
    object_records.update_collection_record(
        collection, "rec-2", {"note": updated_note}, base_dir=data_dir, roots=[]
    )
    object_records.delete_collection_record(collection, "rec-5", base_dir=data_dir, roots=[])

    path = _records_tsv_path(data_dir, collection)
    recovered = _naive_read_append_tsv_with_manual_fold(path)

    assert "rec-5" not in recovered, "naive fold failed to honor the 'del' tombstone"
    assert set(recovered) == {"rec-0", "rec-1", "rec-2", "rec-3", "rec-4"}
    assert recovered["rec-2"]["note"] == updated_note, (
        "naive fold failed to let the later physical row for rec-2 win"
    )
    for rid, rec in recovered.items():
        if rid == "rec-2":
            continue
        expected = next(r for r in records if r["id"] == rid)
        assert rec == expected, f"naive fold mismatch for {rid!r}: {rec!r} vs {expected!r}"

    print(
        "\n[ARCHIVAL round-trip / append, update+delete] PRINCIPLE HOLDS: "
        "a hand-rolled fold using only the two documented op values (\"\" "
        "upsert / \"del\" tombstone) and a csv.DictReader recovers the "
        "correct live dataset -- update and delete included -- with zero "
        "object_records code involved."
    )
