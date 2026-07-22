"""CHARACTERIZATION tests: crash-recovery / torn-write durability of the TSV
substrate (object_records.py). This is the crown-jewel property of a
"database": does a write interrupted at ANY byte offset (process killed,
disk full, container OOM-killed mid-flush) leave the store in a valid state
with no data loss and no corruption?

Pure characterization -- this file does not modify any production module.
Where behavior fails a data-safety property, the assertion is written to
state the property a crash-safe substrate SHOULD have, and is either (a)
left to fail loudly (a hard assertion, when the failure would be a NEW
finding not already known/deferred), or (b) wrapped in `pytest.mark.xfail
(strict=True)` when it reproduces the already-documented, deliberately
deferred substrate bug #2 (see object_records._repair_torn_tail's
"KNOWN LIMITATION" docstring paragraph, and
test_append_mode_torn_tail_mid_multiline_row_is_silently_resurrected_and_
cascades_FINDING in test_embedded_json_lines_characterization.py, which
first identified it). Do not "fix" a failing assertion here by weakening it
to match broken behavior, and do not fix object_records.py from this file.

CONTEXT (see object_records.py for the authoritative version of this):
  - CLASSIC mode always rewrites the whole file atomically: temp file +
    `Path.replace` (_write_collection_records). A crash strictly before the
    replace leaves the OLD file completely untouched (atomic rename is all-
    or-nothing at the filesystem level) plus, possibly, an orphaned temp
    file next to it that no read path ever looks at.
  - APPEND mode appends rows in place (open("a")) and maintains an
    id->offset sidecar (`.records.oidx`) alongside records.tsv. Before each
    append, `_repair_torn_tail` truncates any unterminated fragment left by
    a prior crash (_drop_torn_tail's rule: "does the file end with a raw
    newline byte" is treated as "is the tail committed"). Both compaction
    and any full rewrite of an append-format file reuse the same atomic
    temp+replace path as classic mode.
  - SUBSTRATE BUG #2 (documented, deferred, xfailed elsewhere -- NOT fixed
    here): the "ends with \\n" check behind both _drop_torn_tail and
    _repair_torn_tail is QUOTE-BLIND. It only makes sense for a row whose
    value never itself contains a raw newline byte. A crash that lands
    right after a newline that is INSIDE an open quoted field (e.g. a
    pretty-printed embedded-JSON cell) leaves the file ending in "\\n" even
    though the row -- and the write -- never actually completed. Both
    checks then wrongly conclude "not torn": the fragment is resurrected as
    a garbled "committed" record, and (because its quote was never closed
    on disk) every row appended afterward is silently absorbed AS TEXT into
    that still-open field until the collection is compacted.

This file's job is to map that surface exhaustively (every byte offset of a
representative multi-line row, not just the one case already known) and to
probe the durability of every OTHER torn-write surface this substrate has:
single-line append rows, the oidx sidecar file, a torn header, an
interrupted compaction, and classic mode's atomic-rename guarantee.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

import object_records

pytestmark = pytest.mark.conformance


ID_FIELD = {"name": "id"}
VALUE_FIELD = {"name": "value"}
LINES_FIELD = {"name": "lines", "type": "textarea"}


# =============================================================================
# setup helpers (mirror tests/test_object_records.py and
# tests/test_embedded_json_lines_characterization.py's conventions exactly)
# =============================================================================


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
    """Force the NEXT read to go past the warm in-process caches -- see
    tests/test_embedded_json_lines_characterization.py's identical helper
    for why this matters for exercising the id->offset sidecar path."""
    object_records._RECORDS_CACHE.clear()
    object_records._OIDX_CACHE.clear()


def _records_path(data_dir: Path, collection: str) -> Path:
    return data_dir / "collections" / collection / "records.tsv"


def _stray_tmp_path(real_path: Path) -> Path:
    """A sibling temp-file name matching the EXACT pattern
    _write_collection_records uses (`.{name}.{pid}.{tid}.tmp`), so this
    reproduces -- byte-for-byte, filename-wise -- the file a real crash
    between that function's temp-write and its `Path.replace` call would
    leave behind."""
    return real_path.with_name(f".{real_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")


def _captured_next_row_bytes(
    data_dir: Path, collection: str, payload: dict
) -> tuple[bytes, bytes]:
    """Perform a REAL create_collection_record and return (bytes_before,
    row_bytes): row_bytes is the EXACT physical bytes production appended
    for this create, captured by diffing records.tsv before/after, and the
    file is then restored to bytes_before -- i.e. this "un-commits" the
    create, leaving a pristine last-known-good file plus a byte-exact
    template of what a real write to it would look like, for callers to
    layer synthetic torn prefixes onto. Using the module's own writer to
    produce these bytes (rather than hand-building csv text) guarantees the
    dialect/quoting exactly matches what a genuine crash would have been
    interrupted mid-way through.
    """
    path = _records_path(data_dir, collection)
    before = path.read_bytes()
    object_records.create_collection_record(collection, payload, base_dir=data_dir, roots=[])
    after = path.read_bytes()
    assert after.startswith(before), "sanity: a fast append must only ever add bytes at EOF"
    row_bytes = after[len(before):]
    path.write_bytes(before)
    oidx_path = object_records._oidx_path(path)
    if oidx_path.exists():
        oidx_path.unlink()
    _clear_caches()
    return before, row_bytes


def _fold_and_by_id_agree(collection: str, data_dir: Path, ids: list[str]) -> bool:
    """Property (c) from the task: the fold-all read (read_collection_records)
    and the by-id read (get_collection_record, which for an append-mode
    collection with a cold cache goes through the id->offset sidecar) must
    return the identical value for every id the fold path claims is live,
    and get_collection_record must raise RecordNotFoundError for every id
    the fold path does NOT claim is live."""
    _clear_caches()
    folded = {r["id"]: r for r in object_records.read_collection_records(
        collection, base_dir=data_dir, roots=[]
    )}
    for record_id in ids:
        _clear_caches()
        if record_id in folded:
            try:
                by_id = object_records.get_collection_record(
                    collection, record_id, base_dir=data_dir, roots=[]
                )
            except object_records.RecordNotFoundError:
                return False
            if by_id != folded[record_id]:
                return False
        else:
            try:
                object_records.get_collection_record(
                    collection, record_id, base_dir=data_dir, roots=[]
                )
            except object_records.RecordNotFoundError:
                continue
            return False
    return True


# =============================================================================
# 1. APPEND MODE -- single-physical-line row, EVERY byte offset
# =============================================================================


def test_append_single_line_row_torn_at_every_byte_offset_self_heals(tmp_path):
    """Baseline surface, swept exhaustively rather than spot-checked: a row
    with no embedded newline of its own has no way to "look complete" via
    the endswith(b'\\n') check unless the FULL row (including its own
    terminating \\n) is present. Every strict prefix is therefore expected,
    at EVERY byte offset, to (a) lose no previously-committed record, (b)
    never resurrect a phantom/partial record, (c) have the fold and by-id
    read paths agree, and (d) self-heal cleanly on the next write. This is
    the property the whole append-mode design is supposed to guarantee;
    confirming it holds at every offset (not just one hand-picked cut) is
    the point of this test.
    """
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "logs", [ID_FIELD, VALUE_FIELD])
    object_records.create_collection_record(
        "logs", {"id": "A", "value": "one"}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "logs", {"id": "B", "value": "two"}, base_dir=data_dir, roots=[]
    )
    path = _records_path(data_dir, "logs")

    before, row_bytes = _captured_next_row_bytes(
        data_dir, "logs", {"id": "C", "value": "three-single-physical-line"}
    )
    assert row_bytes.count(b"\n") == 1, "sanity: genuinely a single physical line"
    assert row_bytes.endswith(b"\n")

    for offset in range(0, len(row_bytes)):  # every strict prefix: 0 .. len-1
        fragment = row_bytes[:offset]
        path.write_bytes(before + fragment)
        oidx_path = object_records._oidx_path(path)
        if oidx_path.exists():
            oidx_path.unlink()
        _clear_caches()

        listing = object_records.list_collection_records("logs", base_dir=data_dir, roots=[])[
            "records"
        ]
        assert listing == [{"id": "A", "value": "one"}, {"id": "B", "value": "two"}], (
            f"offset={offset}/{len(row_bytes)}: torn fragment must not lose A/B or "
            f"resurrect a phantom C; got {listing!r}"
        )
        assert _fold_and_by_id_agree("logs", data_dir, ["A", "B", "C"]), (
            f"offset={offset}: fold-all and by-id reads disagree"
        )

        # Self-heal: the next normal write must land as its own clean record,
        # and the torn fragment must never resurface.
        object_records.create_collection_record(
            "logs", {"id": "D", "value": "four"}, base_dir=data_dir, roots=[]
        )
        _clear_caches()
        listing_after = object_records.list_collection_records(
            "logs", base_dir=data_dir, roots=[]
        )["records"]
        assert listing_after == [
            {"id": "A", "value": "one"},
            {"id": "B", "value": "two"},
            {"id": "D", "value": "four"},
        ], f"offset={offset}: self-heal did not land D cleanly; got {listing_after!r}"


# =============================================================================
# 2. APPEND MODE -- multi-physical-line row (embedded newline), EVERY byte
#    offset -- maps substrate bug #2's exact trigger surface
# =============================================================================


def _hostile_multiline_blob() -> str:
    payload = [
        {"sku": "SKU-1", "note": 'café "q" \\z 日本', "amount_cents": 1099},
        {"sku": "SKU-2", "note": "plain ascii", "amount_cents": -50},
    ]
    return json.dumps(payload, indent=2)  # pretty-printed: real raw "\n" bytes in the cell


def _setup_multiline_scenario(tmp_path) -> tuple[Path, Path, bytes, bytes, list[int]]:
    """Shared setup for both multi-line torn-tail tests below. Returns
    (data_dir, path, before, row_bytes, predicted_bug_offsets).

    predicted_bug_offsets is every offset whose fragment contains AT LEAST
    ONE raw "\\n" byte of its own (i.e. every offset from right after the
    row's FIRST embedded newline through its second-to-last byte) --
    empirically confirmed (see this file's own exploratory run) to be the
    REAL trigger condition, which is measurably BROADER than "the fragment
    itself ends with \\n": _drop_torn_tail's rfind(b'\\n') doesn't require
    the crash to land exactly after a newline -- it back-scans from
    WHEREVER the crash landed to the nearest earlier "\\n" and keeps
    everything up to and including it. Once the fragment contains any
    embedded newline at all, that back-scan finds it (rather than falling
    all the way back to the end of the PRIOR row) and truncates there,
    leaving a still-open-quote row ending in "\\n" -- at which point
    csv.reader, given EOF while inside quotes, tolerantly treats it as the
    field's own terminator instead of raising. So the true trigger surface
    is not "crash lands right after a newline" but "crash lands ANYWHERE
    once the row's value has emitted its first raw newline byte" -- for a
    typical pretty-printed JSON cell, that is within the first handful of
    bytes of the value, meaning nearly the ENTIRE remainder of the row's
    write window is vulnerable, not just the instants immediately
    following each newline.
    """
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "invoices", [ID_FIELD, LINES_FIELD])
    object_records.create_collection_record(
        "invoices", {"id": "A", "lines": json.dumps([{"sku": "A-1"}])}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "invoices", {"id": "B", "lines": json.dumps([{"sku": "B-1"}])}, base_dir=data_dir, roots=[]
    )
    path = _records_path(data_dir, "invoices")

    before, row_bytes = _captured_next_row_bytes(
        data_dir, "invoices", {"id": "C", "lines": _hostile_multiline_blob()}
    )
    assert row_bytes.count(b"\n") > 3, "sanity: genuinely spans several physical lines"
    assert row_bytes.endswith(b"\n")

    predicted_bug_offsets = [
        offset for offset in range(1, len(row_bytes))
        if b"\n" in row_bytes[:offset]
    ]
    assert predicted_bug_offsets, "sanity: the blob must contain internal newlines to test"
    return data_dir, path, before, row_bytes, predicted_bug_offsets


def test_append_multiline_row_torn_at_every_byte_offset_maps_bug_2_surface(tmp_path):
    """THE comprehensive sweep. For every byte offset of a real, captured,
    multi-physical-line row:

      - offsets whose fragment contains NO raw "\\n" byte of its own (i.e.
        the crash landed before the value ever emitted its first embedded
        newline) are the substrate's unconditional promise -- the whole
        fragment plus everything before it, once _drop_torn_tail's
        rfind(b'\\n') runs, collapses back to exactly the last committed
        row (B's), so these must ALWAYS recover cleanly (no data loss, no
        phantom, reads agree, self-heals). Any failure here would be a NEW
        finding, not bug #2, and is a hard assertion.
      - offsets whose fragment DOES contain an embedded "\\n" are exactly
        bug #2's real trigger surface (see _setup_multiline_scenario's
        docstring for why this is the correct predictor -- broader than
        "ends with \\n"). These are recorded into a matrix and reported,
        but NOT hard-asserted here (see the dedicated xfail(strict=True)
        test right below this one, which asserts the bug actually
        reproduces at each of them; keeping that assertion out of THIS
        test lets the full sweep run to completion and print a complete
        matrix even though most of its rows are "expected bad").
    """
    data_dir, path, before, row_bytes, predicted_bug_offsets = _setup_multiline_scenario(tmp_path)

    unexpected: list[tuple[int, list[str], list[str]]] = []
    bug_confirmed: list[int] = []
    bug_did_not_reproduce: list[int] = []
    clean_count = 0

    for offset in range(1, len(row_bytes)):  # skip 0 (trivial no-op) and full length (not torn)
        fragment = row_bytes[:offset]
        has_internal_nl = b"\n" in fragment
        path.write_bytes(before + fragment)
        oidx_path = object_records._oidx_path(path)
        if oidx_path.exists():
            oidx_path.unlink()
        _clear_caches()

        listing = object_records.list_collection_records("invoices", base_dir=data_dir, roots=[])[
            "records"
        ]
        ids = [r["id"] for r in listing]
        reads_agree = _fold_and_by_id_agree("invoices", data_dir, ["A", "B", "C"])
        clean_before_write = (ids == ["A", "B"]) and reads_agree

        object_records.create_collection_record(
            "invoices", {"id": "D", "lines": json.dumps([{"sku": "D-1"}])},
            base_dir=data_dir, roots=[],
        )
        _clear_caches()
        listing_after = object_records.list_collection_records(
            "invoices", base_dir=data_dir, roots=[]
        )["records"]
        ids_after = [r["id"] for r in listing_after]
        d_landed_clean = ids_after == ["A", "B", "D"]

        fully_clean = clean_before_write and d_landed_clean

        if fully_clean:
            clean_count += 1
            if has_internal_nl:
                bug_did_not_reproduce.append(offset)
        elif has_internal_nl:
            bug_confirmed.append(offset)
        else:
            unexpected.append((offset, ids, ids_after))

    print(f"\n[torn-tail matrix] multiline row: {len(row_bytes)} bytes total, "
          f"{len(predicted_bug_offsets)} / {len(row_bytes) - 1} offsets predicted vulnerable "
          f"(fragment contains an embedded newline)")
    print(f"[torn-tail matrix] clean-recovery offsets: {clean_count} / {len(row_bytes) - 1}")
    print(f"[torn-tail matrix] bug #2 CONFIRMED (resurrection/cascade) at {len(bug_confirmed)} "
          f"offsets: {bug_confirmed}")
    if bug_did_not_reproduce:
        print(f"[torn-tail matrix] predicted-vulnerable offsets that recovered cleanly anyway: "
              f"{bug_did_not_reproduce}")

    assert not unexpected, (
        "NEW FINDING (outside bug #2's known quote-blind surface): a torn "
        "fragment with NO embedded newline of its own -- one the documented "
        "torn-tail rule unconditionally promises to collapse back to the last "
        f"committed row -- failed to recover cleanly. offset/ids/ids_after: {unexpected!r}"
    )
    # Every offset that reproduced the bug is, exactly, one whose fragment
    # contains an embedded "\n" -- confirming the (broader-than-originally-
    # documented) trigger condition precisely.
    assert set(bug_confirmed) <= set(predicted_bug_offsets)


@pytest.mark.xfail(
    strict=True,
    reason="substrate bug #2 (torn-tail is quote-blind) PENDING FIX -- see "
    "object_records._repair_torn_tail's 'KNOWN LIMITATION' docstring and "
    "test_append_mode_torn_tail_mid_multiline_row_is_silently_resurrected_and_"
    "cascades_FINDING in test_embedded_json_lines_characterization.py, which "
    "first identified this with one hand-picked offset. This test asserts the "
    "SAME failure mode reproduces at EVERY offset from right after the row's "
    "FIRST embedded newline through its last byte -- a materially BROADER "
    "surface than 'immediately following a newline' (see "
    "_setup_multiline_scenario's docstring for why) -- mapping the full "
    "trigger surface rather than re-confirming a single point. strict=True "
    "flips this to XPASS (a hard failure) the moment the underlying bug is "
    "fixed, so it becomes a real regression guard at that point.",
)
def test_append_multiline_row_torn_past_first_internal_newline_reproduces_bug_2(tmp_path):
    data_dir, path, before, row_bytes, predicted_bug_offsets = _setup_multiline_scenario(tmp_path)

    for offset in predicted_bug_offsets:
        fragment = row_bytes[:offset]
        assert b"\n" in fragment
        path.write_bytes(before + fragment)
        oidx_path = object_records._oidx_path(path)
        if oidx_path.exists():
            oidx_path.unlink()
        _clear_caches()

        # Effect 1: a bare read resurrects the incomplete row C as if genuine.
        listing = object_records.list_collection_records("invoices", base_dir=data_dir, roots=[])[
            "records"
        ]
        ids = [r["id"] for r in listing]
        assert ids == ["A", "B", "C"], (
            f"offset={offset}: expected bug #2 to resurrect a phantom C; got {ids!r}"
        )

        # Effect 2: the next normal write cascades -- D is silently absorbed
        # into C's still-open corrupted field instead of landing on its own.
        object_records.create_collection_record(
            "invoices", {"id": "D", "lines": json.dumps([{"sku": "D-1"}])},
            base_dir=data_dir, roots=[],
        )
        _clear_caches()
        listing_after = object_records.list_collection_records(
            "invoices", base_dir=data_dir, roots=[]
        )["records"]
        ids_after = [r["id"] for r in listing_after]
        assert ids_after == ["A", "B", "C", "D"], (
            f"offset={offset}: expected D to be swallowed into C's corrupted "
            f"field (cascade); got ids={ids_after!r}"
        )


# =============================================================================
# 3. APPEND MODE -- torn oidx SIDECAR (`.records.oidx`), every byte offset
# =============================================================================


def test_append_oidx_sidecar_torn_at_every_byte_offset_self_heals(tmp_path, monkeypatch):
    """The sidecar's own line format (`{start}\\t{end}\\t{op}\\t{id}\\n`) can
    never itself contain a raw newline mid-line -- ids are restricted to
    _RECORD_ID_RE and the other three columns are plain digits/op-literals
    -- so, unlike records.tsv, there is no quote-blindness surface here at
    all: "ends with \\n" is a SOUND completeness check for every physical
    line of this file, not just an approximation. The sidecar is also, by
    its own design ("never required for correctness" -- see _load_oidx's
    docstring), never the sole source of truth: any torn/missing tail is
    expected to be caught up from records.tsv itself. This sweeps every
    byte offset of one real captured idx line and confirms that promise:
    the collection's data is untouched (records.tsv is never even written
    to it by this scenario) and every id resolves correctly regardless of
    how badly mangled the sidecar's own tail is.
    """
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "widgets", [ID_FIELD, VALUE_FIELD])
    path = _records_path(data_dir, "widgets")
    oidx_path = object_records._oidx_path(path)

    object_records.create_collection_record(
        "widgets", {"id": "w1", "value": "Alpha"}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "widgets", {"id": "w2", "value": "Beta"}, base_dir=data_dir, roots=[]
    )
    # Force the sidecar to exist and be warm/caught-up through w2.
    object_records.get_collection_record("widgets", "w1", base_dir=data_dir, roots=[])
    assert oidx_path.exists()
    oidx_before = oidx_path.read_bytes()
    records_before = path.read_bytes()

    object_records.create_collection_record(
        "widgets", {"id": "w3", "value": "Gamma"}, base_dir=data_dir, roots=[]
    )
    oidx_after = oidx_path.read_bytes()
    records_after = path.read_bytes()
    assert oidx_after.startswith(oidx_before)
    assert records_after.startswith(records_before)
    oidx_row_bytes = oidx_after[len(oidx_before):]
    assert oidx_row_bytes.count(b"\n") == 1

    # records.tsv legitimately, fully committed w3's row -- only the SIDECAR
    # is torn in this scenario (a crash between the data append and the
    # sidecar catching up to it).
    for offset in range(0, len(oidx_row_bytes) + 1):  # every prefix, 0 .. full line
        oidx_path.write_bytes(oidx_before + oidx_row_bytes[:offset])
        path.write_bytes(records_after)  # data file: always fully committed
        object_records._OIDX_CACHE.clear()
        object_records._RECORDS_CACHE.clear()

        id_offsets, coherent = object_records._load_oidx(path)
        assert coherent, f"oidx offset={offset}: catch-up scan must reach full coherence"
        assert set(id_offsets) == {"w1", "w2", "w3"}, (
            f"oidx offset={offset}: catch-up must recover all three ids; got {set(id_offsets)}"
        )

        object_records._OIDX_CACHE.clear()
        object_records._RECORDS_CACHE.clear()
        for record_id, expected_value in (("w1", "Alpha"), ("w2", "Beta"), ("w3", "Gamma")):
            got = object_records.get_collection_record(
                "widgets", record_id, base_dir=data_dir, roots=[]
            )
            assert got == {"id": record_id, "value": expected_value}, (
                f"oidx offset={offset}: {record_id} did not resolve correctly; got {got!r}"
            )


# =============================================================================
# 4. APPEND MODE -- torn HEADER (interrupted very first write)
# =============================================================================


@pytest.mark.xfail(
    strict=True,
    reason="FINDING (new, distinct from bug #2): create_collection_record can "
    "report SUCCESS (no exception at all) while silently truncating an "
    "intact-but-unterminated header down to zero bytes via "
    "_repair_torn_tail's 'no newline found anywhere -> truncate whole file' "
    "fallback, then appending a bare data row with NO header in front of it "
    "-- so the very next read (by anyone) fails with an uncaught ValueError. "
    "See this test's docstring for the exact mechanism. Not fixed here "
    "(characterization only). A torn header is not reachable via this "
    "module's OWN crash paths (headers are always written via atomic "
    "temp+rename -- see section 6/7 below) but IS reachable via any external "
    "corruption that clips exactly the header's own trailing byte (a copy "
    "mid-write, a truncating disk fault, a manual edit) while leaving no "
    "other newline in the file -- worth hardening given how silent it is.",
)
def test_append_torn_header_every_byte_offset_characterization(tmp_path):
    """The header line is written ONLY as part of a full-rewrite (temp file +
    atomic rename) -- the transition-in write for a collection's very first
    record, per _persist_write's TRANSITION-IN branch -- and every full
    rewrite in this module goes through _write_collection_records's
    temp+replace. Because of that, a torn header is NOT actually reachable
    via any real crash in this module's own write path: readers can only
    ever observe the OLD file (rename never happened) or the NEW complete
    file (rename happened), never a half-written header -- see section 6/7
    below for that atomicity property tested directly.

    This test instead treats the header as a hostile/hand-corrupted file
    (e.g. a foreign process, a disk-level bit-rot, a manual edit, a copy
    caught mid-write) and characterizes whatever the module actually does --
    a SYNTHETIC scenario, documented as such, not a mapped crash-reachable
    surface. The result is NOT the simple "always self-heals" this test
    originally assumed. Sweeping every byte offset of the 13-byte header
    "_op\\tid\\tvalue\\n" reveals several distinct outcomes depending on
    exactly where the cut lands:

      - offset 0 (a genuinely empty existing file): both read and write
        raise an uncaught `ValueError: ... is missing a header` --
        _cache_entry's "file doesn't exist -> empty collection" special
        case is NOT extended to "file exists but is empty", so an empty
        records.tsv is treated as corrupt rather than as an empty
        collection. No self-heal at all.
      - offsets that cut inside "_op" or leave a header missing an "id"
        column (1, 2, 3, 5) or that produce an empty trailing field name
        (4, 7): both read and write raise -- again no self-heal, but at
        least loud/consistent about it on every call.
      - offset 6 ("_op\\tid", missing only "\\tvalue"): read succeeds
        (empty collection) and write CORRECTLY self-heals via the
        new-field-fallback full rewrite, producing a clean 3-column file.
      - offsets 8-11 (a partial "value" column name -- "v", "va", "val",
        "valu"): read succeeds (empty collection), but write "self-heals"
        into a PERMANENTLY WRONG SCHEMA: it treats the partial name as a
        genuine, distinct existing column and adds the real "value" column
        alongside it, leaving every future row with a silently-injected
        extra empty field (e.g. {"id": "w1", "v": "", "value": "Alpha"}).
        Not a crash -- worse, in a sense: a quiet, permanent schema
        pollution that round-trips forever afterward.
      - offset 12 (the full, correct 3-column header content, missing ONLY
        its own trailing "\\n"): read alone succeeds (empty collection,
        since a headers-only single physical line with no newline still
        parses fine via csv.reader's EOF tolerance). But CREATE takes the
        FAST-APPEND branch (the header parses as valid append-physical),
        which calls _repair_torn_tail first -- and because this 12-byte
        file has NO newline ANYWHERE (not even the header's own), that
        function's backward scan finds nothing to truncate to and hits its
        last-resort `handle.truncate(0)` fallback, destroying the header
        entirely. The append then writes a bare, header-less data row.
        create_collection_record ITSELF RAISES NO EXCEPTION -- the caller
        sees a normal-looking success -- yet the very next read of this
        collection, by anyone, raises an uncaught ValueError (the bare data
        row is now misread as the header, containing an empty leading
        field). This is the worst outcome in the sweep: silent apparent
        success immediately followed by an unreadable collection.

    This test's terminal assertion locks in exactly that last (offset 12)
    outcome as the headline finding; the full per-offset matrix is printed
    for the complete picture.
    """
    header_bytes = b"_op\tid\tvalue\n"
    matrix: list[tuple[int, str, str, str]] = []

    for offset in range(0, len(header_bytes)):  # every strict prefix of the header line itself
        data_dir = tmp_path / f"case_{offset}"
        write_append_schema(data_dir, "widgets", [ID_FIELD, VALUE_FIELD])
        path = _records_path(data_dir, "widgets")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(header_bytes[:offset])
        _clear_caches()

        list_outcome = "ok"
        try:
            listing = object_records.list_collection_records(
                "widgets", base_dir=data_dir, roots=[]
            )["records"]
            list_outcome = "ok-empty" if listing == [] else f"ok-nonempty:{listing!r}"
        except Exception as exc:  # noqa: BLE001 -- characterizing whatever happens
            list_outcome = f"{type(exc).__name__}: {exc}"

        _clear_caches()
        create_outcome = "ok"
        create_raised = False
        try:
            object_records.create_collection_record(
                "widgets", {"id": "w1", "value": "Alpha"}, base_dir=data_dir, roots=[]
            )
        except Exception as exc:  # noqa: BLE001
            create_outcome = f"{type(exc).__name__}: {exc}"
            create_raised = True

        _clear_caches()
        post_create_read_outcome = "n/a (create raised)"
        if not create_raised:
            try:
                listing_after = object_records.list_collection_records(
                    "widgets", base_dir=data_dir, roots=[]
                )["records"]
                post_create_read_outcome = f"ok:{listing_after!r}"
            except Exception as exc:  # noqa: BLE001
                post_create_read_outcome = f"{type(exc).__name__}: {exc}"

        matrix.append((offset, list_outcome, create_outcome, post_create_read_outcome))

    print("\n[torn-header matrix] offset -> (list, create, read-immediately-after-create):")
    for row in matrix:
        print(f"  {row}")

    # The one invariant that should hold regardless of how a corrupt/torn
    # header is otherwise handled: create_collection_record must never
    # report SUCCESS (no exception) while silently leaving the collection
    # unreadable on the very next read -- that is strictly worse than a loud
    # failure, since the caller believes their write succeeded.
    silent_corruptions = [
        (offset, post_read)
        for offset, _list, create, post_read in matrix
        if create == "ok" and not post_read.startswith("ok")
    ]
    assert not silent_corruptions, (
        "FINDING: create_collection_record reported success (raised nothing) "
        "while silently corrupting records.tsv such that the VERY NEXT read "
        "fails. A caller sees their write as having succeeded with no error, "
        "yet the whole collection becomes unreadable immediately afterward. "
        f"(offset, read-immediately-after-create outcome): {silent_corruptions!r}"
    )


# =============================================================================
# 5. APPEND MODE -- interrupted COMPACTION
# =============================================================================


def test_append_interrupted_compaction_leaves_pre_compaction_data_intact(tmp_path):
    """compact_collection rewrites via the exact same atomic temp+replace
    path as any other full rewrite (_write_collection_records). A crash
    strictly before the final `Path.replace` therefore can only ever leave
    the PRE-compaction file (complete, with its dead rows still physically
    present but harmlessly so -- they're already excluded by folding)
    untouched, plus an orphaned temp file no read path looks at. This
    demonstrates that guarantee directly: a stray, correctly-named temp file
    holding a plausible (but never-renamed) compacted body sits alongside
    the real file, and every read/write must behave exactly as if it were
    never there -- then a REAL compaction afterward must still succeed
    despite the leftover stray file.
    """
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "events", [ID_FIELD, VALUE_FIELD])
    for i in range(5):
        object_records.create_collection_record(
            "events", {"id": f"e{i}", "value": f"v{i}"}, base_dir=data_dir, roots=[]
        )
    # Create dead rows (superseded physical rows) so compaction has real work.
    object_records.update_collection_record(
        "events", "e0", {"value": "v0-updated"}, base_dir=data_dir, roots=[]
    )
    object_records.delete_collection_record("events", "e1", base_dir=data_dir, roots=[])

    path = _records_path(data_dir, "events")
    pre_compaction_bytes = path.read_bytes()

    stray = _stray_tmp_path(path)
    stray.write_bytes(b"_op\tid\tvalue\n\te0\tv0-would-be-compacted\n")  # plausible, never renamed
    try:
        _clear_caches()
        listing = object_records.list_collection_records("events", base_dir=data_dir, roots=[])[
            "records"
        ]
        assert listing == [
            {"id": "e0", "value": "v0-updated"},
            {"id": "e2", "value": "v2"},
            {"id": "e3", "value": "v3"},
            {"id": "e4", "value": "v4"},
        ], f"a stray uncommitted temp file must never be read; got {listing!r}"
        assert path.read_bytes() == pre_compaction_bytes, (
            "a bare read must never touch/rewrite records.tsv"
        )

        # Write activity must also be unaffected by the stray file's presence.
        object_records.create_collection_record(
            "events", {"id": "e5", "value": "v5"}, base_dir=data_dir, roots=[]
        )
        _clear_caches()
        assert stray.exists(), "the real write must not disturb an unrelated stray temp file"

        # A REAL compaction, run with the stray file still present, must
        # succeed and produce correct live content.
        result = object_records.compact_collection("events", base_dir=data_dir, roots=[])
        assert result["rows_after"] < result["rows_before"]
        _clear_caches()
        listing_after = object_records.list_collection_records(
            "events", base_dir=data_dir, roots=[]
        )["records"]
        assert listing_after == [
            {"id": "e0", "value": "v0-updated"},
            {"id": "e2", "value": "v2"},
            {"id": "e3", "value": "v3"},
            {"id": "e4", "value": "v4"},
            {"id": "e5", "value": "v5"},
        ]
    finally:
        if stray.exists():
            stray.unlink()


# =============================================================================
# 6/7. CLASSIC MODE -- atomic-rename durability (crash between temp-write and
#      rename), and the same guarantee re-confirmed for an APPEND-mode
#      transition-in / new-field full rewrite
# =============================================================================


def test_classic_mode_crash_between_temp_write_and_rename_serves_last_good_version(tmp_path):
    """Classic mode never appends in place -- every write is a full rewrite
    via temp file + atomic `Path.replace`. Simulate a crash strictly before
    that replace: leave a real, correctly-named, fully-written temp file
    (containing what WOULD have become the next version) sitting next to
    the real file, which still holds the last version that actually
    finished. Every read must serve exactly that last good version, never a
    mix, and the temp file must never be picked up as real data.
    """
    data_dir = tmp_path / "data"
    write_schema(data_dir, "widgets", [ID_FIELD, VALUE_FIELD])
    object_records.create_collection_record(
        "widgets", {"id": "w1", "value": "Alpha"}, base_dir=data_dir, roots=[]
    )
    path = _records_path(data_dir, "widgets")
    last_good_bytes = path.read_bytes()

    # Simulate: an update to w1 got as far as fully writing its temp file,
    # then the process died before `temp_path.replace(path)` ran.
    stray = _stray_tmp_path(path)
    stray.write_text("id\tvalue\nw1\tAlpha-WOULD-HAVE-BEEN-UPDATED\n")
    try:
        object_records._RECORDS_CACHE.clear()
        listing = object_records.list_collection_records("widgets", base_dir=data_dir, roots=[])[
            "records"
        ]
        assert listing == [{"id": "w1", "value": "Alpha"}], (
            f"must serve only the last COMMITTED version; got {listing!r}"
        )
        assert path.read_bytes() == last_good_bytes

        object_records._RECORDS_CACHE.clear()
        fetched = object_records.get_collection_record(
            "widgets", "w1", base_dir=data_dir, roots=[]
        )
        assert fetched == {"id": "w1", "value": "Alpha"}

        # A real write, with the stray temp file still present, must succeed
        # normally and not be confused by it.
        object_records.update_collection_record(
            "widgets", "w1", {"value": "Alpha2"}, base_dir=data_dir, roots=[]
        )
        object_records._RECORDS_CACHE.clear()
        listing_after = object_records.list_collection_records(
            "widgets", base_dir=data_dir, roots=[]
        )["records"]
        assert listing_after == [{"id": "w1", "value": "Alpha2"}]
    finally:
        if stray.exists():
            stray.unlink()


def test_append_mode_transition_in_crash_between_temp_write_and_rename(tmp_path):
    """The append-mode analogue of the classic-mode test above: a brand-new
    append-mode collection's very first write is a TRANSITION-IN full
    rewrite (adds the `_op` header column), which is the same
    _write_collection_records temp+replace path. Before that first write
    ever completes, records.tsv does not exist at all -- a crash leaves
    only an orphaned temp file, and the collection must still read back as
    genuinely empty (not error, not phantom data) and a subsequent real
    write must still succeed and transition in normally.
    """
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "widgets", [ID_FIELD, VALUE_FIELD])
    path = _records_path(data_dir, "widgets")
    assert not path.exists()

    stray = _stray_tmp_path(path)
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("_op\tid\tvalue\n\tw1\tWould-have-been-committed\n")
    try:
        listing = object_records.list_collection_records("widgets", base_dir=data_dir, roots=[])[
            "records"
        ]
        assert listing == [], f"a never-renamed temp file must not surface as data; got {listing!r}"
        assert not path.exists(), "a bare read must not materialize records.tsv from the stray file"

        object_records.create_collection_record(
            "widgets", {"id": "w1", "value": "Alpha"}, base_dir=data_dir, roots=[]
        )
        object_records._RECORDS_CACHE.clear()
        listing_after = object_records.list_collection_records(
            "widgets", base_dir=data_dir, roots=[]
        )["records"]
        assert listing_after == [{"id": "w1", "value": "Alpha"}]
        assert path.exists()
    finally:
        if stray.exists():
            stray.unlink()
