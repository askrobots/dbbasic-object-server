"""CHARACTERIZATION tests: does the current TSV substrate (object_records.py)
safely hold an embedded-JSON array field (a document's "lines") through both
storage modes -- especially the append-mode, byte-offset-indexed single-record
read path (the id->offset sidecar, ".records.oidx")?

This file is pure characterization of EXISTING behavior. It does not modify
any production module. Where a case fails or corrupts data, the assertion is
written to state the correctness property that a "safe to hold embedded JSON"
substrate SHOULD have, and is left to fail -- that failure IS the finding, and
is documented in this file's docstrings and in the accompanying report. Do
not "fix" a failing assertion here by weakening it to match broken behavior.

Context: the design question being evaluated is whether order/invoice/journal
LINE ITEMS can be modeled as a single embedded JSON array on the parent
document (schemaless TSV + a `lines` field holding `json.dumps(...)`) rather
than a separate `*_lines` collection. Two risk axes, per collection storage
mode:

  1. Hostile content: does a `lines` value containing TAB, NEWLINE,
     DOUBLE-QUOTE, BACKSLASH, CRLF, or Unicode survive an exact round trip
     through the TSV/CSV layer, in both the "read everything and fold" path
     and the single-record-by-id path?
  2. Scale: many items per array, and many documents, in both correctness and
     wall-clock terms -- including the cost of MUTATING an item inside an
     already-large array (the write-amplification concern for this model).

One important nuance threaded through every "hostile content" test below:
`json.dumps(...)` (the realistic way a `lines` value would be produced)
ESCAPES every control character in a string VALUE -- a literal tab or
newline typed by a user becomes the two-character sequence "\\t"/"\\n" in the
JSON text, never a raw 0x09/0x0A byte. So a compact `json.dumps(...)` blob,
however hostile the *logical* content, never actually presents a raw
tab/newline byte to the TSV/CSV layer -- only literal double-quotes,
backslashes, and Unicode do. The one realistic way a `lines` blob DOES end up
carrying literal raw newline BYTES is pretty-printing (`json.dumps(...,
indent=2)`, or any other human-readable serialization choice) -- so tests
that need to exercise a genuinely multi-physical-line row use `indent=2`
deliberately, and say so.
"""

from __future__ import annotations

import csv
import io
import json
import time
from pathlib import Path

import pytest

import object_records


ID_FIELD = {"name": "id"}
LINES_FIELD = {"name": "lines", "type": "textarea"}


# --- setup helpers (mirror tests/test_object_records.py's conventions) -----


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
    """Force the NEXT read to go past the warm in-process caches: a cold
    _RECORDS_CACHE + _OIDX_CACHE is what makes get_collection_record on an
    append-mode file actually exercise the id->offset sidecar / _oidx_get_row
    (see _fast_record_lookup) instead of serving from the ordinary records
    cache -- which is what this whole file needs to test the right thing."""
    object_records._RECORDS_CACHE.clear()
    object_records._OIDX_CACHE.clear()


def _hostile_lines() -> list[dict]:
    return [
        {
            "sku": "SKU-1",
            "note": "café \t x\n y \"q\" \\z 日本",
            "crlf_field": "alpha\r\nbeta",
            "amount_cents": 1099,
        },
        {
            "sku": "SKU-2",
            "note": "plain ascii, no surprises",
            "amount_cents": -50,
        },
    ]


# =============================================================================
# 1. HOSTILE CONTENT, CLASSIC mode
# =============================================================================


def test_hostile_content_classic_mode_round_trip(tmp_path):
    """Classic-mode parsing (_parse_classic_records) uses csv.reader over the
    whole file handle, which is quote-aware ACROSS physical lines -- so a
    pretty-printed (literal-raw-newline-bearing) JSON blob should round-trip
    exactly, both via the fold-all read and the by-id read (classic mode
    never touches the id->offset sidecar at all)."""
    data_dir = tmp_path / "data"
    write_schema(data_dir, "invoices", [ID_FIELD, LINES_FIELD])
    original = _hostile_lines()
    # Pretty-printed deliberately: this is what forces literal raw "\n"
    # bytes into the stored TSV cell (see module docstring) -- the worst
    # case for a naive delimiter scan, and the case classic mode's
    # file-level csv.reader is expected to still get right.
    blob = json.dumps(original, indent=2)

    object_records.create_collection_record(
        "invoices", {"id": "inv-1", "lines": blob}, base_dir=data_dir, roots=[]
    )

    _clear_caches()
    all_records = object_records.read_collection_records("invoices", base_dir=data_dir, roots=[])
    assert len(all_records) == 1
    assert json.loads(all_records[0]["lines"]) == original

    _clear_caches()
    by_id = object_records.get_collection_record("invoices", "inv-1", base_dir=data_dir, roots=[])
    assert json.loads(by_id["lines"]) == original


# =============================================================================
# 2. HOSTILE CONTENT, APPEND mode
# =============================================================================


def test_hostile_content_append_mode_compact_json_round_trip(tmp_path, monkeypatch):
    """Compact json.dumps (no raw control bytes, but real literal
    double-quotes/backslashes/Unicode) through append mode, forcing the
    by-id read through the cold-cache id->offset sidecar path
    (DBBASIC_RECORDS_CACHE_MAX_ROWS=0, same trick
    tests/test_object_records.py's oidx tests use). Expected, and observed,
    to round-trip correctly: nothing about this content spans more than one
    physical line."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "invoices", [ID_FIELD, LINES_FIELD])
    original = _hostile_lines()
    blob = json.dumps(original)

    object_records.create_collection_record(
        "invoices", {"id": "inv-1", "lines": blob}, base_dir=data_dir, roots=[]
    )

    _clear_caches()
    all_records = object_records.read_collection_records("invoices", base_dir=data_dir, roots=[])
    assert json.loads(all_records[0]["lines"]) == original

    _clear_caches()
    by_id = object_records.get_collection_record("invoices", "inv-1", base_dir=data_dir, roots=[])
    assert json.loads(by_id["lines"]) == original


def test_hostile_content_append_mode_pretty_json_by_id_read_round_trips(
    tmp_path, monkeypatch
):
    """REGRESSION GUARD for substrate fix #1 (was a FINDING, now fixed):
    _oidx_get_row -- the function behind get_collection_record's
    byte-offset-indexed single-record read, once the ordinary records cache
    is cold -- used to read the row with a raw `handle.readline()` that
    stopped at the FIRST b'\\n' byte, silently TRUNCATING any value that spans
    multiple physical lines (e.g. this pretty-printed JSON blob) at its first
    embedded newline, and (because "lines" is the last column) returning the
    wrong value with no exception or fallback. The fix reads the full LOGICAL
    row via csv.reader from the row offset, exactly as the full-fold path
    (_parse_append_body) already did -- so the by-id read now consumes the
    quoted multi-line field correctly. This test asserts both paths agree.
    """
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "invoices", [ID_FIELD, LINES_FIELD])
    original = _hostile_lines()
    blob = json.dumps(original, indent=2)  # pretty-printed: literal raw "\n" bytes in the cell

    object_records.create_collection_record(
        "invoices", {"id": "inv-1", "lines": blob}, base_dir=data_dir, roots=[]
    )

    _clear_caches()
    all_records = object_records.read_collection_records("invoices", base_dir=data_dir, roots=[])
    assert json.loads(all_records[0]["lines"]) == original  # full fold: correct

    _clear_caches()
    by_id = object_records.get_collection_record("invoices", "inv-1", base_dir=data_dir, roots=[])
    try:
        loaded = json.loads(by_id["lines"])
    except json.JSONDecodeError as exc:
        pytest.fail(
            "FINDING confirmed: the offset-indexed by-id read returned "
            f"truncated/unparseable JSON for a row with an embedded literal "
            f"newline (json.loads raised {exc!r}). Raw value returned: "
            f"{by_id['lines']!r}"
        )
    assert loaded == original, (
        "FINDING confirmed: the offset-indexed by-id read returned wrong "
        f"data for a row with an embedded literal newline. Got: {loaded!r}"
    )


# =============================================================================
# 3. OFFSET-INDEX CRUX: does the sidecar delimit rows CSV-aware, or naively?
# =============================================================================


def test_offset_index_crux_reads_survive_an_embedded_newline_row_between_them(
    tmp_path, monkeypatch
):
    """THE central question this file exists to answer: when the id->offset
    sidecar is built/caught-up (_scan_append_tail, shared by
    _scan_append_rows_for_offsets and the record-cache's own tail-delta
    fold), does it delimit physical rows with real CSV parsing, or with a
    naive '\\n' scan? If naive, record B's embedded newlines (a
    pretty-printed JSON blob) would throw off every row_start/row_end AFTER
    it, corrupting C's and D's offsets even though neither of THEIR rows has
    anything hostile in it.

    ANSWER (read from the source, then verified here): _scan_append_tail
    parses via `csv.reader` over `io.StringIO(text)` and computes each row's
    byte span from the csv reader's OWN `stream.tell()` position (character
    offset, converted to a byte length by re-encoding only the newly
    consumed slice) -- genuinely CSV-aware, not a `.split("\\n")` or
    `.count("\\n")` scan. This test forces the sidecar cold on every point
    op (DBBASIC_RECORDS_CACHE_MAX_ROWS=0) and confirms C and D come back
    correct by id, both before and after B is updated (a second physical
    row, also multi-line) -- i.e. the scan's row-boundary tracking is
    correct across a multi-physical-line row. (This is a DIFFERENT question
    from "is B's OWN by-id read correct" -- it isn't, see the FINDING test
    above; that is a distinct bug in how a row is RE-READ from its offset,
    not in how offsets are COMPUTED during the scan.)
    """
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "invoices", [ID_FIELD, LINES_FIELD])

    a_lines = [{"sku": "A-1", "amount_cents": 100}]
    b_lines_v1 = _hostile_lines()
    c_lines = [{"sku": "C-1", "amount_cents": 300}, {"sku": "C-2", "amount_cents": 301}]
    d_lines = [{"sku": "D-1", "amount_cents": 400}]

    object_records.create_collection_record(
        "invoices", {"id": "A", "lines": json.dumps(a_lines)}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "invoices",
        {"id": "B", "lines": json.dumps(b_lines_v1, indent=2)},  # multi-physical-line row
        base_dir=data_dir,
        roots=[],
    )
    object_records.create_collection_record(
        "invoices", {"id": "C", "lines": json.dumps(c_lines)}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "invoices", {"id": "D", "lines": json.dumps(d_lines)}, base_dir=data_dir, roots=[]
    )

    _clear_caches()
    rec_c = object_records.get_collection_record("invoices", "C", base_dir=data_dir, roots=[])
    assert json.loads(rec_c["lines"]) == c_lines

    _clear_caches()
    rec_d = object_records.get_collection_record("invoices", "D", base_dir=data_dir, roots=[])
    assert json.loads(rec_d["lines"]) == d_lines

    # Update B (a SECOND multi-physical-line physical row, superseding the
    # first) and confirm C/D still resolve correctly afterwards -- the
    # sidecar's catch-up scan must correctly skip over BOTH of B's physical
    # rows to keep later offsets right.
    b_lines_v2 = _hostile_lines() + [{"sku": "B-extra", "amount_cents": 999}]
    object_records.update_collection_record(
        "invoices", "B", {"lines": json.dumps(b_lines_v2, indent=2)}, base_dir=data_dir, roots=[]
    )

    _clear_caches()
    rec_c2 = object_records.get_collection_record("invoices", "C", base_dir=data_dir, roots=[])
    assert json.loads(rec_c2["lines"]) == c_lines

    _clear_caches()
    rec_d2 = object_records.get_collection_record("invoices", "D", base_dir=data_dir, roots=[])
    assert json.loads(rec_d2["lines"]) == d_lines


# =============================================================================
# 4. SCALE -- many lines per doc
# =============================================================================


def _item_shape(i: int) -> dict:
    return {"sku": f"SKU-{i:05d}", "qty": (i % 7) + 1, "price_cents": 1099 + i, "tax_cents": 87}


def test_scale_many_lines_per_doc_round_trip(tmp_path):
    """Correctness (not timing -- see the many-docs test below) at
    increasing embedded-array size, both storage modes: 10/100/1000 line
    items -- all comfortably under Python's csv module's default
    field_size_limit (131072 bytes / 128 KiB; see the dedicated FINDING
    test right below this one for what happens once a `lines` blob crosses
    that ceiling, which this item shape does well before 5000 items).
    Reports the serialized blob's byte size at each size, since that --
    not the item count itself -- is what actually drives write/parse
    cost."""
    data_dir = tmp_path / "data"
    write_schema(data_dir, "orders_classic", [ID_FIELD, LINES_FIELD])
    write_append_schema(data_dir, "orders_append", [ID_FIELD, LINES_FIELD])

    sizes_report: dict[int, int] = {}
    for n in (10, 100, 1000):
        lines = [_item_shape(i) for i in range(n)]
        blob = json.dumps(lines)
        sizes_report[n] = len(blob.encode("utf-8"))
        record_id = f"ord-{n}"

        object_records.create_collection_record(
            "orders_classic", {"id": record_id, "lines": blob}, base_dir=data_dir, roots=[]
        )
        object_records.create_collection_record(
            "orders_append", {"id": record_id, "lines": blob}, base_dir=data_dir, roots=[]
        )

        for collection in ("orders_classic", "orders_append"):
            _clear_caches()
            all_recs = object_records.read_collection_records(
                collection, base_dir=data_dir, roots=[]
            )
            match = next(r for r in all_recs if r["id"] == record_id)
            assert json.loads(match["lines"]) == lines

            _clear_caches()
            by_id = object_records.get_collection_record(
                collection, record_id, base_dir=data_dir, roots=[]
            )
            assert json.loads(by_id["lines"]) == lines

    print("\n[scale] serialized 'lines' blob size (bytes) by item count:")
    for n, nbytes in sizes_report.items():
        print(f"  n={n:>5}  bytes={nbytes:>8}")


def test_scale_large_field_round_trips_and_oversize_rejected_on_write(tmp_path):
    """REGRESSION GUARD for substrate fix #3 (was a FINDING, now fixed):
    csv.field_size_limit is raised to MAX_TSV_FIELD_BYTES at module load and
    create/update reject any field over that cap -- so a large-but-under-cap
    cell round-trips and an over-cap write is refused, instead of silently
    corrupting reads. The original finding this guards against:

    FINDING (see report -- independent of, and arguably more
    fundamental than, the offset-index/torn-tail findings above): nothing
    in object_records.py ever raises Python's `csv` module's default
    `field_size_limit()` (131072 bytes / 128 KiB) above its stdlib
    default. A `lines` blob past that size -- 5000 items of this shape
    serializes to ~350 KB, well over it; the crossover for THIS item shape
    (~70 bytes/item) is roughly 1800-1900 items, printed below -- is
    written to disk just fine (csv.writer enforces no such limit), but
    becomes UNREADABLE the moment anything tries to read the collection
    back, and the failure mode is different, and worse, in append mode
    than in classic mode:

      - CLASSIC mode: `_parse_classic_records` has no try/except around
        row iteration at all, so `_csv.Error: field larger than field
        limit (131072)` propagates UNCAUGHT out of
        read_collection_records/get_collection_record. Loud, but total:
        the entire collection (every OTHER record in it too, not just the
        oversized one) becomes unreadable via any read path until the
        oversized value is removed or the process's field_size_limit is
        raised -- there is no per-record isolation.
      - APPEND mode: both `_parse_append_body` and `_scan_append_tail`
        catch `csv.Error` and treat it exactly like a torn tail (`break`,
        stop consuming, don't raise) -- which is the right instinct for a
        genuinely torn write, but wrong here: the oversized field is
        legitimately, fully committed. The practical effect: since the
        oversized row in this test is the FIRST physical row, parsing
        breaks immediately and the fold produces ZERO records -- the
        entire collection (every record, not just the oversized one)
        silently reads back as EMPTY on every path (list, and by-id, since
        an incoherent/empty sidecar rebuild falls through to the same
        empty fold) -- no exception at all. This is silent, total,
        collection-wide data loss on the READ side (the bytes are still on
        disk; nothing can see them) triggered by ONE oversized document.

    This is a hard, item-shape-independent ceiling any embedded-JSON-array
    design must budget for: a single richly-detailed invoice/order/journal
    (many line items, or line items with long descriptions) can trivially
    exceed 128 KB.
    """
    data_dir = tmp_path / "data"
    write_schema(data_dir, "orders_classic", [ID_FIELD, LINES_FIELD])
    write_append_schema(data_dir, "orders_append", [ID_FIELD, LINES_FIELD])

    # A large-but-under-cap value (~350 KB, far below the 16 MiB
    # MAX_TSV_FIELD_BYTES) now round-trips on EVERY path in BOTH modes. Before
    # fix #3 this same value silently emptied the whole append collection on
    # read; now the raised csv.field_size_limit lets it parse.
    n = 5000
    lines = [_item_shape(i) for i in range(n)]
    blob = json.dumps(lines)
    blob_bytes = len(blob.encode("utf-8"))
    assert blob_bytes > 131072  # over the old 128 KiB stdlib default...
    assert blob_bytes < object_records.MAX_TSV_FIELD_BYTES  # ...but under our cap
    print(f"\n[scale] blob={blob_bytes} bytes: over 128 KiB, under the "
          f"{object_records.MAX_TSV_FIELD_BYTES}-byte cap -- must round-trip")

    for coll in ("orders_classic", "orders_append"):
        object_records.create_collection_record(
            coll, {"id": "big-1", "lines": blob}, base_dir=data_dir, roots=[]
        )
        object_records.create_collection_record(
            coll, {"id": "small-2", "lines": json.dumps([{"sku": "small"}])},
            base_dir=data_dir, roots=[],
        )
        _clear_caches()
        all_rows = object_records.read_collection_records(coll, base_dir=data_dir, roots=[])
        assert {r["id"] for r in all_rows} == {"big-1", "small-2"}, (
            f"{coll}: an oversize-but-under-cap field must not break the read "
            f"(nor drop its siblings); got {all_rows!r}"
        )
        _clear_caches()
        got = object_records.get_collection_record(coll, "big-1", base_dir=data_dir, roots=[])
        assert json.loads(got["lines"]) == lines, (
            f"{coll}: by-id read of the large field must round-trip exactly"
        )

    # Over the cap: create REJECTS the write with a structured error rather
    # than persist a row that cannot be read back.
    over = "x" * (object_records.MAX_TSV_FIELD_BYTES + 1)
    for coll in ("orders_classic", "orders_append"):
        with pytest.raises(object_records.InvalidRecordPayloadError):
            object_records.create_collection_record(
                coll, {"id": "too-big", "lines": over}, base_dir=data_dir, roots=[]
            )


# =============================================================================
# 5. SCALE -- many docs (timing)
# =============================================================================


def test_scale_many_docs_ecommerce_shape_append_mode_timing(tmp_path):
    """1000 docs x 5-line arrays (the ecommerce shape: many small documents),
    then 200 docs x 500-line arrays, append mode. Wall-clock characterization
    only (time.perf_counter) -- the only hard assertions are that a sampled
    read from each population is correct."""
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "orders_small", [ID_FIELD, LINES_FIELD])
    write_append_schema(data_dir, "orders_big", [ID_FIELD, LINES_FIELD])

    def make_lines(n: int, seed: int) -> list[dict]:
        return [
            {"sku": f"SKU-{seed}-{i:04d}", "qty": (i % 5) + 1, "price_cents": 500 + i}
            for i in range(n)
        ]

    docs_small = 1000
    lines_per_small_doc = 5
    t0 = time.perf_counter()
    for i in range(docs_small):
        blob = json.dumps(make_lines(lines_per_small_doc, i))
        object_records.create_collection_record(
            "orders_small", {"id": f"small-{i:05d}", "lines": blob}, base_dir=data_dir, roots=[]
        )
    write_elapsed_small = time.perf_counter() - t0

    _clear_caches()
    t0 = time.perf_counter()
    sample_small = object_records.get_collection_record(
        "orders_small", "small-00500", base_dir=data_dir, roots=[]
    )
    read_elapsed_small = time.perf_counter() - t0
    assert json.loads(sample_small["lines"]) == make_lines(lines_per_small_doc, 500)

    docs_big = 200
    lines_per_big_doc = 500
    t0 = time.perf_counter()
    for i in range(docs_big):
        blob = json.dumps(make_lines(lines_per_big_doc, i))
        object_records.create_collection_record(
            "orders_big", {"id": f"big-{i:04d}", "lines": blob}, base_dir=data_dir, roots=[]
        )
    write_elapsed_big = time.perf_counter() - t0

    _clear_caches()
    t0 = time.perf_counter()
    sample_big = object_records.get_collection_record(
        "orders_big", "big-0100", base_dir=data_dir, roots=[]
    )
    read_elapsed_big = time.perf_counter() - t0
    assert json.loads(sample_big["lines"]) == make_lines(lines_per_big_doc, 100)

    print("\n[scale] many-docs append-mode timing:")
    print(
        f"  {docs_small} docs x {lines_per_small_doc} lines: "
        f"total write={write_elapsed_small:.4f}s "
        f"({write_elapsed_small / docs_small * 1000:.3f}ms/doc), "
        f"single by-id read={read_elapsed_small * 1000:.3f}ms"
    )
    print(
        f"  {docs_big} docs x {lines_per_big_doc} lines: "
        f"total write={write_elapsed_big:.4f}s "
        f"({write_elapsed_big / docs_big * 1000:.3f}ms/doc), "
        f"single by-id read={read_elapsed_big * 1000:.3f}ms"
    )


# =============================================================================
# 6. TORN TAIL, APPEND mode -- with an embedded newline in the partial row
# =============================================================================


def test_append_mode_torn_tail_ordinary_case_still_self_heals_with_lines_field(tmp_path):
    """Baseline (expected-safe) case, mirroring
    test_append_mode_torn_tail_is_ignored_and_self_heals in
    tests/test_object_records.py exactly, just with a `lines` field/compact
    JSON payload in the mix: a crash that lands in the middle of a SINGLE
    physical line (no embedded newline in the torn fragment itself) is
    correctly detected and dropped, and self-heals on the next write."""
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "invoices", [ID_FIELD, LINES_FIELD])
    object_records.create_collection_record(
        "invoices",
        {"id": "A", "lines": json.dumps([{"sku": "A-1"}])},
        base_dir=data_dir,
        roots=[],
    )
    object_records.create_collection_record(
        "invoices",
        {"id": "B", "lines": json.dumps([{"sku": "B-1"}])},
        base_dir=data_dir,
        roots=[],
    )
    path = data_dir / "collections" / "invoices" / "records.tsv"
    full_text = path.read_text()

    # Chop the last few bytes off (no embedded newline involved -- ordinary
    # single-physical-line torn tail).
    path.write_text(full_text[:-4])
    assert not path.read_text().endswith("\n")

    _clear_caches()
    before_stat = path.stat()
    listing = object_records.list_collection_records("invoices", base_dir=data_dir, roots=[])[
        "records"
    ]
    after_stat = path.stat()
    assert listing == [{"id": "A", "lines": json.dumps([{"sku": "A-1"}])}]
    assert before_stat.st_mtime_ns == after_stat.st_mtime_ns
    assert before_stat.st_size == after_stat.st_size

    object_records.create_collection_record(
        "invoices",
        {"id": "C", "lines": json.dumps([{"sku": "C-1"}])},
        base_dir=data_dir,
        roots=[],
    )
    _clear_caches()
    listing_after = object_records.list_collection_records("invoices", base_dir=data_dir, roots=[])[
        "records"
    ]
    assert listing_after == [
        {"id": "A", "lines": json.dumps([{"sku": "A-1"}])},
        {"id": "C", "lines": json.dumps([{"sku": "C-1"}])},
    ]


@pytest.mark.xfail(
    strict=True,
    reason="substrate bug #2 (torn-tail is quote-blind) PENDING FIX -- see "
    "plan/parity-completion-plan.md Stage 1a. Fix: _repair_torn_tail must "
    "truncate to _scan_append_tail's csv-aware covered_bytes from the "
    "sidecar's last known-good offset, not rfind(b'\\n'). Deferred as a "
    "careful crash-safety change; strict=True flips this to XPASS when fixed "
    "so the test gets rewritten into a real regression guard.",
)
def test_append_mode_torn_tail_mid_multiline_row_is_silently_resurrected_and_cascades_FINDING(
    tmp_path,
):
    """FINDING (see report -- this is the most serious one): the torn-tail
    safety net (_drop_torn_tail / _repair_torn_tail) decides "is the file's
    tail committed?" with exactly one check: does the file end with a
    literal b'\\n' byte. That check is unsound for any row whose value spans
    multiple physical lines (e.g. a pretty-printed embedded-JSON `lines`
    blob): a real crash can flush and stop at ANY byte boundary, including
    one that lands right after an INTERNAL newline inside the still-open
    quoted field -- at which point the file "ends with \\n" even though the
    row (and the write) never actually completed.

    Reproduced here: a row for id "C" is deliberately cut after its SECOND
    physical line (mid-quote, but the cut point itself is right after a
    newline, so the file's raw bytes end in \\n). Observed effects, via the
    real public API only (create_collection_record / list_collection_records
    -- no internal function called directly):

      1. A plain read (no write at all) immediately reports C as a normal,
         fully-formed committed record -- with a truncated/garbled `lines`
         value -- even though C's write never completed. This is silent
         data resurrection from a torn write, not "torn tail correctly
         dropped."
      2. Because C's quoted field was never actually closed on disk, EVERY
         row appended after it (self-heal does not trigger -- the file
         already "ends with \\n" per the same broken check, so
         _repair_torn_tail sees nothing to truncate) gets silently absorbed
         AS TEXT into C's still-open "lines" value. A subsequent normal
         create of record "D" does not produce a new, distinct record D at
         all -- D's entire physical row becomes part of C's corrupted
         value, and D is permanently missing from every future read.

    This is worse than the offset-index truncation FINDING above: it is not
    limited to the (already cold-cache-gated) by-id read path, it requires
    no cache-warmth conditions to observe, and it is not self-limiting to
    one row -- it corrupts and swallows every subsequent write until the
    collection is compacted (compaction rewrites from the in-memory FOLDED
    view, which already contains C's corrupted value and never recovers D).
    """
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "invoices", [ID_FIELD, LINES_FIELD])
    object_records.create_collection_record(
        "invoices",
        {"id": "A", "lines": json.dumps([{"sku": "A-1"}])},
        base_dir=data_dir,
        roots=[],
    )
    object_records.create_collection_record(
        "invoices",
        {"id": "B", "lines": json.dumps([{"sku": "B-1"}])},
        base_dir=data_dir,
        roots=[],
    )
    path = data_dir / "collections" / "invoices" / "records.tsv"
    full_text = path.read_text()
    assert full_text.endswith("\n")

    # Build what row C's bytes WOULD have been (a full, well-formed physical
    # row for a pretty-printed, multi-line "lines" value), via the exact
    # same csv dialect object_records itself writes with -- then keep only a
    # PREFIX that cuts right after the row's second physical line, so the
    # leftover fragment (a) is genuinely incomplete (the quote never
    # closes) and (b) itself ends with "\n" -- simulating a crash that
    # landed at that unlucky byte boundary.
    hostile_blob = json.dumps(_hostile_lines(), indent=2)
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(["", "C", hostile_blob])
    full_row_text = buf.getvalue()
    assert full_row_text.count("\n") > 3  # sanity: genuinely spans several physical lines

    first_nl = full_row_text.index("\n")
    second_nl = full_row_text.index("\n", first_nl + 1)
    torn_fragment = full_row_text[: second_nl + 1]
    assert torn_fragment.endswith("\n")  # the crux: looks "committed" by the naive check

    path.write_text(full_text + torn_fragment)
    assert path.read_text().endswith("\n")  # file passes the "torn tail" check despite being torn

    # Effect 1: a plain read resurrects the incomplete row C as if genuine.
    # (Report this via a soft check rather than a hard `assert`+stop: effect
    # 2, below, is the more important half of the finding and must still
    # run and be reported even if effect 1's exact shape ever changes.)
    _clear_caches()
    listing = object_records.list_collection_records("invoices", base_dir=data_dir, roots=[])[
        "records"
    ]
    ids = [r["id"] for r in listing]
    effect_1_confirmed = ids == ["A", "B", "C"]
    print(f"\n[torn-tail FINDING] effect 1 -- ids after a bare read of the torn file: {ids!r}")
    if not effect_1_confirmed:
        print(f"  (effect 1 did NOT reproduce as expected -- got {ids!r}, expected ['A', 'B', 'C'])")

    # Effect 2: the next NORMAL write does not self-heal -- it cascades,
    # silently absorbing D's entire row into C's still-open field.
    object_records.create_collection_record(
        "invoices",
        {"id": "D", "lines": json.dumps([{"sku": "D-1"}])},
        base_dir=data_dir,
        roots=[],
    )
    _clear_caches()
    listing_after = object_records.list_collection_records(
        "invoices", base_dir=data_dir, roots=[]
    )["records"]
    ids_after = [r["id"] for r in listing_after]
    print(f"[torn-tail FINDING] effect 2 -- ids after one more normal create('D'): {ids_after!r}")

    assert effect_1_confirmed, (
        "FINDING confirmed: a torn write that crashed mid-multiline-row was "
        f"NOT dropped -- it was resurrected as a committed record. Got ids: {ids!r}"
    )
    assert ids_after == ["A", "B", "C", "D"], (
        "FINDING confirmed: record D's write was silently absorbed into "
        f"C's still-open corrupted field instead of landing as its own "
        f"record. Got ids: {ids_after!r} / records: {listing_after!r}"
    )


# =============================================================================
# 7. MUTATION PERFORMANCE -- editing one item inside an already-stored array
# =============================================================================


def test_mutation_edit_one_item_performance_classic_vs_append(tmp_path):
    """Characterization: cost of editing ONE item inside an N-item embedded
    JSON array and re-writing the record, classic vs append storage, at
    N = 5/50/500/1000. Each collection also carries a fixed backdrop of
    SIBLING_COUNT small sibling documents, so the thing this comparison is
    actually about -- classic mode's full-FILE rewrite touching every OTHER
    document too, vs append mode's O(1) delta row -- has something to show
    up against. (Without any sibling backdrop, a single-document collection
    makes classic and append cost about the same: both are then dominated
    purely by the edited blob's own size, since there's nothing else to
    amplify.)

    Not a hard-assertion timing test -- see the printed table in the pytest
    -s output -- but round-trip correctness of the edited record IS
    asserted, in both modes, at every N."""
    SIBLING_COUNT = 150
    ITEM_SIZES = (5, 50, 500, 1000)
    results: dict[tuple[str, int], float] = {}

    for storage in ("classic", "append"):
        for n in ITEM_SIZES:
            collection = f"mut1_{storage}_{n}"
            data_dir = tmp_path / f"data_{storage}_{n}"
            if storage == "classic":
                write_schema(data_dir, collection, [ID_FIELD, LINES_FIELD])
            else:
                write_append_schema(data_dir, collection, [ID_FIELD, LINES_FIELD])

            for i in range(SIBLING_COUNT):
                sib_lines = [{"sku": f"sib-{i}-{j}", "price_cents": 100 + j} for j in range(3)]
                object_records.create_collection_record(
                    collection,
                    {"id": f"sib-{i:04d}", "lines": json.dumps(sib_lines)},
                    base_dir=data_dir,
                    roots=[],
                )

            target_lines = [
                {"sku": f"item-{i:04d}", "qty": 1, "price_cents": 1000 + i} for i in range(n)
            ]
            object_records.create_collection_record(
                collection,
                {"id": "target", "lines": json.dumps(target_lines)},
                base_dir=data_dir,
                roots=[],
            )

            # The edit: bump item[0]'s price by one cent, then re-serialize
            # the WHOLE array -- there is no partial-field update for an
            # embedded array; the entire blob must be rewritten even though
            # only one of its N items changed.
            current = object_records.get_collection_record(
                collection, "target", base_dir=data_dir, roots=[]
            )
            items = json.loads(current["lines"])
            original_first_price = items[0]["price_cents"]
            items[0]["price_cents"] += 1
            new_blob = json.dumps(items)

            t0 = time.perf_counter()
            object_records.update_collection_record(
                collection, "target", {"lines": new_blob}, base_dir=data_dir, roots=[]
            )
            elapsed = time.perf_counter() - t0
            results[(storage, n)] = elapsed

            after = object_records.get_collection_record(
                collection, "target", base_dir=data_dir, roots=[]
            )
            after_items = json.loads(after["lines"])
            assert after_items[0]["price_cents"] == original_first_price + 1
            assert after_items[1:] == items[1:]

    print(
        f"\n[mutation] edit-one-item latency, classic vs append "
        f"({SIBLING_COUNT} sibling docs backdrop):"
    )
    print(f"  {'N items':>8} {'classic (ms)':>14} {'append (ms)':>14}")
    for n in ITEM_SIZES:
        c = results[("classic", n)] * 1000
        a = results[("append", n)] * 1000
        print(f"  {n:>8} {c:>14.3f} {a:>14.3f}")


def test_mutation_repeated_edits_compaction_effect_on_read_latency(tmp_path, monkeypatch):
    """K=50 repeated edits of the SAME big (N=500-item) embedded-array
    document, append mode: each edit re-writes the whole 500-item blob as
    one new physical row (append mode's write amplification per the
    previous test), so 50 edits leaves 50 superseded physical rows plus the
    original sitting in records.tsv before compaction runs. A backdrop of
    BACKDROP_COUNT small sibling rows is included so the id->offset
    sidecar's on-disk body (one line per PHYSICAL row of the WHOLE
    collection, not just this one document) is big enough for a cold
    reload's cost to be plausibly measurable.
    DBBASIC_RECORDS_CACHE_MAX_ROWS=0 forces every point op through the
    sidecar rather than the ordinary records cache, matching how a
    collection past that threshold behaves in production; caches are then
    explicitly cleared (_clear_caches) immediately before each of the two
    timed reads so both measure a genuinely cold reload, not a warm hit."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    collection = "mut2"
    write_append_schema(data_dir, collection, [ID_FIELD, LINES_FIELD])

    BACKDROP_COUNT = 1500
    N = 500
    K = 50

    for i in range(BACKDROP_COUNT):
        sib_lines = [{"sku": f"sib-{i}-{j}"} for j in range(3)]
        object_records.create_collection_record(
            collection,
            {"id": f"sib-{i:05d}", "lines": json.dumps(sib_lines)},
            base_dir=data_dir,
            roots=[],
        )

    big_lines = [{"sku": f"item-{i:04d}", "price_cents": 1000 + i} for i in range(N)]
    object_records.create_collection_record(
        collection, {"id": "big", "lines": json.dumps(big_lines)}, base_dir=data_dir, roots=[]
    )

    for k in range(K):
        current = object_records.get_collection_record(collection, "big", base_dir=data_dir, roots=[])
        items = json.loads(current["lines"])
        items[k % N]["price_cents"] += 1
        object_records.update_collection_record(
            collection, "big", {"lines": json.dumps(items)}, base_dir=data_dir, roots=[]
        )

    stats_before = object_records.append_collection_stats(collection, base_dir=data_dir, roots=[])

    _clear_caches()
    t0 = time.perf_counter()
    before_read = object_records.get_collection_record(collection, "big", base_dir=data_dir, roots=[])
    read_before = time.perf_counter() - t0
    before_items = json.loads(before_read["lines"])
    assert len(before_items) == N  # correctness held through 50 edits

    compact_result = object_records.compact_collection(collection, base_dir=data_dir, roots=[])
    stats_after = object_records.append_collection_stats(collection, base_dir=data_dir, roots=[])

    _clear_caches()
    t0 = time.perf_counter()
    after_read = object_records.get_collection_record(collection, "big", base_dir=data_dir, roots=[])
    read_after = time.perf_counter() - t0
    after_items = json.loads(after_read["lines"])

    assert after_items == before_items  # compaction must not change live content
    assert compact_result["rows_after"] < compact_result["rows_before"]

    print(
        f"\n[mutation] {K} repeated edits of one {N}-item document "
        f"(+{BACKDROP_COUNT} sibling docs backdrop), append mode:"
    )
    print(
        f"  physical_rows before compaction: {stats_before['physical_rows']} "
        f"(live_rows={stats_before['live_rows']}, bloat_ratio={stats_before['bloat_ratio']})"
    )
    print(
        f"  physical_rows after compaction:  {stats_after['physical_rows']} "
        f"(live_rows={stats_after['live_rows']}, bloat_ratio={stats_after['bloat_ratio']})"
    )
    print(f"  compact_collection result: {compact_result}")
    print(f"  cold single-record-by-id read BEFORE compaction: {read_before * 1000:.3f}ms")
    print(f"  cold single-record-by-id read AFTER  compaction: {read_after * 1000:.3f}ms")
