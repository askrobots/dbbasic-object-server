"""CHARACTERIZATION tests: does the current TSV substrate (object_records.py)
safely hold multibyte UTF-8 content through both storage modes, with special
attention to BYTE-OFFSET accounting?

This file is pure characterization of EXISTING behavior. It does not modify
any production module. Where a case fails or corrupts data, the assertion is
written to state the correctness property the substrate SHOULD have, and is
left to fail -- that failure IS the finding. Do not "fix" a failing assertion
here by weakening it to match broken behavior.

THE RISK THIS FILE TARGETS: object_records.py stores TSV via csv. In append
mode, a byte-offset sidecar (`.records.oidx`) is maintained; _scan_append_tail
derives each row's byte span by decoding text ONCE per scan and measuring
consumed CHAR-slices back to bytes
(`len(text[prev_char:cur_char].encode("utf-8"))`), and _oidx_get_row /
_fast_record_lookup then `seek()` to those byte offsets to read a single row.
Multibyte characters mean char-count != byte-count -- so any place that
conflates the two, or seeks to a byte offset landing mid-multibyte-sequence,
could corrupt a read. Separately, _check_field_sizes caps a field at
MAX_TSV_FIELD_BYTES measured via `len(str(value).encode("utf-8"))` -- this
must count BYTES, not characters, or a large-but-under-the-character-cap
multibyte value could be silently mis-admitted or mis-rejected.

Explicitly NOT probed here (already covered / deferred elsewhere):
  - field_size_limit / csv oversize-field handling in general (fixed, see
    tests/test_embedded_json_lines_characterization.py substrate fix #3).
  - _oidx_get_row's own csv-awareness for multi-physical-line rows (fixed,
    substrate fix #1, same file).
  - torn-tail mid-multiline-row resurrection (substrate bug #2, deferred,
    xfail in the same file) -- not touched here at all.

Two content shapes are probed throughout: a RAW field value, and a value
wrapped in a compact `json.dumps(...)` blob (a realistic way multibyte
content actually arrives in a JSON-in-a-cell field) -- json.dumps by default
does NOT escape non-ASCII (ensure_ascii=True is the default and WOULD
\\uXXXX-escape everything into pure ASCII, defeating the point of this file),
so this file passes ensure_ascii=False explicitly to keep raw multibyte UTF-8
bytes in the cell, matching the module docstring's precedent in the sibling
embedded-JSON characterization file for what "compact JSON blob" is meant to
stress.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pytest

import object_records

# Conformance tier: heavy charset characterization -- deselected from the
# per-commit run (see pyproject 'conformance' marker). Run: pytest -m conformance
pytestmark = pytest.mark.conformance


ID_FIELD = {"name": "id"}
VALUE_FIELD = {"name": "value", "type": "textarea"}


# --- setup helpers (mirrored from test_embedded_json_lines_characterization.py) --


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
    """Force the NEXT read past the warm in-process caches -- a cold
    _RECORDS_CACHE + _OIDX_CACHE is what makes get_collection_record on an
    append-mode file actually exercise the id->offset sidecar / _oidx_get_row
    (see _fast_record_lookup) instead of serving from the ordinary records
    cache, which is what this file needs to test the byte-offset path."""
    object_records._RECORDS_CACHE.clear()
    object_records._OIDX_CACHE.clear()


# --- multibyte sample strings --------------------------------------------

TWO_BYTE = "café résumé naïve Москва Ελληνικά שלום"  # accented Latin + Cyrillic + Greek + Hebrew
THREE_BYTE = "日本語中文 العربية देवनागरी"  # CJK + Arabic + Devanagari
FOUR_BYTE = "\U00020000\U00020001 astral CJK-B \U0001F600 emoji \U00010000"  # astral CJK ext B + emoji
MIXED = f"{TWO_BYTE} | {THREE_BYTE} | {FOUR_BYTE}"

NFC_STRING = unicodedata.normalize("NFC", "é café")  # precomposed é where possible
NFD_STRING = unicodedata.normalize("NFD", "é café")  # fully decomposed "e" + combining acute


def _roundtrip_raw(data_dir: Path, collection: str, record_id: str, value: str) -> None:
    """create -> fold read -> by-id read (cold), assert exact string equality
    at every hop, for a RAW (non-JSON) field value."""
    object_records.create_collection_record(
        collection, {"id": record_id, "value": value}, base_dir=data_dir, roots=[]
    )

    _clear_caches()
    all_records = object_records.read_collection_records(collection, base_dir=data_dir, roots=[])
    match = next(r for r in all_records if r["id"] == record_id)
    assert match["value"] == value, (
        f"{collection}/{record_id}: fold read did not round-trip raw value exactly. "
        f"Expected {value!r}, got {match['value']!r}"
    )

    _clear_caches()
    by_id = object_records.get_collection_record(collection, record_id, base_dir=data_dir, roots=[])
    assert by_id["value"] == value, (
        f"{collection}/{record_id}: by-id (cold sidecar) read did not round-trip raw "
        f"value exactly. Expected {value!r}, got {by_id['value']!r}"
    )


def _roundtrip_json(data_dir: Path, collection: str, record_id: str, payload) -> None:
    """create -> fold read -> by-id read (cold), for a value that is a
    compact json.dumps(..., ensure_ascii=False) blob containing multibyte
    content -- json.loads back and compare structurally."""
    blob = json.dumps(payload, ensure_ascii=False)
    object_records.create_collection_record(
        collection, {"id": record_id, "value": blob}, base_dir=data_dir, roots=[]
    )

    _clear_caches()
    all_records = object_records.read_collection_records(collection, base_dir=data_dir, roots=[])
    match = next(r for r in all_records if r["id"] == record_id)
    assert json.loads(match["value"]) == payload, (
        f"{collection}/{record_id}: fold read did not round-trip JSON blob. "
        f"Expected {payload!r}, got {match['value']!r}"
    )

    _clear_caches()
    by_id = object_records.get_collection_record(collection, record_id, base_dir=data_dir, roots=[])
    assert json.loads(by_id["value"]) == payload, (
        f"{collection}/{record_id}: by-id (cold sidecar) read did not round-trip "
        f"JSON blob. Expected {payload!r}, got {by_id['value']!r}"
    )


# =============================================================================
# 1. Basic multibyte width classes -- RAW field, classic AND append mode
# =============================================================================


@pytest.mark.parametrize(
    "label,value",
    [
        ("2byte", TWO_BYTE),
        ("3byte", THREE_BYTE),
        ("4byte_astral", FOUR_BYTE),
        ("mixed", MIXED),
    ],
)
def test_raw_field_multibyte_round_trip_classic_and_append(tmp_path, monkeypatch, label, value):
    """Each width class, RAW (non-JSON) field, both storage modes, cold-cache
    by-id read to force the byte-offset sidecar path in append mode."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_schema(data_dir, "classic_coll", [ID_FIELD, VALUE_FIELD])
    write_append_schema(data_dir, "append_coll", [ID_FIELD, VALUE_FIELD])

    _roundtrip_raw(data_dir, "classic_coll", f"rec-{label}", value)
    _roundtrip_raw(data_dir, "append_coll", f"rec-{label}", value)


@pytest.mark.parametrize(
    "label,value",
    [
        ("2byte", TWO_BYTE),
        ("3byte", THREE_BYTE),
        ("4byte_astral", FOUR_BYTE),
        ("mixed", MIXED),
    ],
)
def test_json_blob_multibyte_round_trip_classic_and_append(tmp_path, monkeypatch, label, value):
    """Same width classes, but the value is embedded inside a compact
    json.dumps(..., ensure_ascii=False) blob -- the realistic shape for a
    JSON-in-a-cell field carrying multibyte user content."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_schema(data_dir, "classic_coll", [ID_FIELD, VALUE_FIELD])
    write_append_schema(data_dir, "append_coll", [ID_FIELD, VALUE_FIELD])

    payload = {"sku": f"SKU-{label}", "note": value, "nested": [value, value]}
    _roundtrip_json(data_dir, "classic_coll", f"rec-{label}", payload)
    _roundtrip_json(data_dir, "append_coll", f"rec-{label}", payload)


def test_update_preserves_multibyte_raw_and_json(tmp_path, monkeypatch):
    """update_collection_record specifically: create with ASCII, then update
    to multibyte content, both raw and JSON-wrapped, append mode, cold-cache
    by-id read after the update."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "append_coll", [ID_FIELD, VALUE_FIELD])

    object_records.create_collection_record(
        "append_coll", {"id": "u1", "value": "plain ascii"}, base_dir=data_dir, roots=[]
    )
    object_records.update_collection_record(
        "append_coll", "u1", {"value": MIXED}, base_dir=data_dir, roots=[]
    )
    _clear_caches()
    got = object_records.get_collection_record("append_coll", "u1", base_dir=data_dir, roots=[])
    assert got["value"] == MIXED, (
        f"update -> cold by-id read did not preserve raw multibyte value exactly. "
        f"Expected {MIXED!r}, got {got['value']!r}"
    )

    payload = {"note": MIXED}
    blob = json.dumps(payload, ensure_ascii=False)
    object_records.update_collection_record(
        "append_coll", "u1", {"value": blob}, base_dir=data_dir, roots=[]
    )
    _clear_caches()
    got2 = object_records.get_collection_record("append_coll", "u1", base_dir=data_dir, roots=[])
    assert json.loads(got2["value"]) == payload, (
        f"update -> cold by-id read did not preserve JSON-wrapped multibyte value. "
        f"Expected {payload!r}, got {got2['value']!r}"
    )


# =============================================================================
# 2. CRITICAL: append byte-offset sidecar correctness when EARLIER rows are
#    heavily multibyte (char-offset != byte-offset for every row after them)
# =============================================================================


def test_offset_sidecar_survives_heavy_multibyte_content_in_earlier_rows(tmp_path, monkeypatch):
    """THE central offset-accounting question this file exists to answer:
    _scan_append_tail computes each row's byte span from a CHARACTER-offset
    delta (`stream.tell()`, a csv.reader-over-io.StringIO character
    position) re-encoded to bytes. If an EARLY record's field is packed with
    multibyte characters, every row AFTER it starts at a byte offset that is
    numerically far from its character offset. If the byte-offset sidecar
    (or _oidx_get_row's seek) ever conflated the two, or mis-tracked the
    cumulative byte cursor, later records' by-id reads (forced through the
    cold sidecar path) would come back garbled, truncated, or attributed to
    the wrong row.

    Layout: A (heavy 3-byte CJK, ~2000 chars => ~6000 bytes), B (heavy 4-byte
    astral, plenty of surrogate-pair-relevant code points), C/D/E (small,
    ordinary ASCII-ish records) -- then C, D, E are each read BY ID with the
    cache forced cold, and must come back byte-exact.
    """
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "offsets", [ID_FIELD, VALUE_FIELD])

    heavy_cjk = "日本語" * 700  # 2100 chars, 3 bytes/char => 6300 bytes
    heavy_astral = "\U00020000\U0001F600" * 500  # 1000 chars, 4 bytes/char => 4000 bytes

    c_value = "record C, plain and short"
    d_value = f"record D with some multibyte: {TWO_BYTE}"
    e_value = f"record E with more: {THREE_BYTE} {FOUR_BYTE}"

    object_records.create_collection_record(
        "offsets", {"id": "A", "value": heavy_cjk}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "offsets", {"id": "B", "value": heavy_astral}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "offsets", {"id": "C", "value": c_value}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "offsets", {"id": "D", "value": d_value}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "offsets", {"id": "E", "value": e_value}, base_dir=data_dir, roots=[]
    )

    for record_id, expected in (("C", c_value), ("D", d_value), ("E", e_value)):
        _clear_caches()
        got = object_records.get_collection_record("offsets", record_id, base_dir=data_dir, roots=[])
        assert got["value"] == expected, (
            f"FINDING: cold by-id read of '{record_id}' (after multibyte-heavy "
            f"earlier rows A/B) did not round-trip exactly -- the byte-offset "
            f"sidecar's accounting broke under multibyte content in preceding "
            f"rows. Expected {expected!r}, got {got['value']!r}"
        )

    # Also confirm A and B (the heavy rows themselves) round-trip.
    for record_id, expected in (("A", heavy_cjk), ("B", heavy_astral)):
        _clear_caches()
        got = object_records.get_collection_record("offsets", record_id, base_dir=data_dir, roots=[])
        assert got["value"] == expected, (
            f"FINDING: cold by-id read of the heavy-multibyte record '{record_id}' "
            f"itself did not round-trip exactly."
        )

    # Full fold read too, for good measure.
    _clear_caches()
    all_records = {r["id"]: r["value"] for r in object_records.read_collection_records(
        "offsets", base_dir=data_dir, roots=[]
    )}
    assert all_records == {
        "A": heavy_cjk, "B": heavy_astral, "C": c_value, "D": d_value, "E": e_value,
    }


def test_offset_sidecar_survives_after_update_of_early_multibyte_row(tmp_path, monkeypatch):
    """Same crux as above, but the multibyte-heavy row is UPDATED (superseded
    by a second, different-length physical row) after later records already
    exist -- confirming the sidecar's catch-up scan correctly advances past
    the new physical row's byte span, not the old one's, before it reaches
    later rows' offsets again."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "offsets2", [ID_FIELD, VALUE_FIELD])

    a_value_v1 = "short ascii to start"
    c_value = "record C"
    d_value = "record D"

    object_records.create_collection_record(
        "offsets2", {"id": "A", "value": a_value_v1}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "offsets2", {"id": "C", "value": c_value}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "offsets2", {"id": "D", "value": d_value}, base_dir=data_dir, roots=[]
    )

    # Now append a NEW physical row for A, much larger and heavily multibyte,
    # AFTER C and D's rows already exist on disk.
    a_value_v2 = "日本語中文" * 900 + FOUR_BYTE  # large, multibyte-heavy
    object_records.update_collection_record(
        "offsets2", "A", {"value": a_value_v2}, base_dir=data_dir, roots=[]
    )

    for record_id, expected in (("C", c_value), ("D", d_value), ("A", a_value_v2)):
        _clear_caches()
        got = object_records.get_collection_record("offsets2", record_id, base_dir=data_dir, roots=[])
        assert got["value"] == expected, (
            f"FINDING: cold by-id read of '{record_id}' after updating A to a "
            f"large multibyte value did not round-trip. Expected {expected!r}, "
            f"got {got['value']!r}"
        )


# =============================================================================
# 3. LARGE multibyte field -- _check_field_sizes must count BYTES not chars
# =============================================================================


def test_large_multibyte_field_counts_bytes_not_chars(tmp_path):
    """100,000 CJK characters (3 bytes each => ~300 KB) is comfortably under
    MAX_TSV_FIELD_BYTES (16 MiB) whether measured in chars or bytes, so this
    alone can't distinguish char-counting from byte-counting bugs in
    _check_field_sizes. The real probe: a value whose CHARACTER count is
    small enough that a char-counting bug would wrongly ADMIT it past the
    byte cap, but whose BYTE count is actually over MAX_TSV_FIELD_BYTES --
    if _check_field_sizes measures bytes correctly (as its source says:
    `len(str(value).encode("utf-8"))`), this must be REJECTED.
    """
    data_dir = tmp_path / "data"
    write_schema(data_dir, "classic_coll", [ID_FIELD, VALUE_FIELD])
    write_append_schema(data_dir, "append_coll", [ID_FIELD, VALUE_FIELD])

    # Comfortably-under-cap large multibyte value: round-trip check.
    big_cjk = "日本語" * 33334  # 100,002 chars, 3 bytes/char = 300,006 bytes
    big_bytes = len(big_cjk.encode("utf-8"))
    assert big_bytes < object_records.MAX_TSV_FIELD_BYTES
    for collection in ("classic_coll", "append_coll"):
        object_records.create_collection_record(
            collection, {"id": "big-cjk", "value": big_cjk}, base_dir=data_dir, roots=[]
        )
        _clear_caches()
        got = object_records.get_collection_record(collection, "big-cjk", base_dir=data_dir, roots=[])
        assert got["value"] == big_cjk, (
            f"{collection}: large (~{big_bytes} byte) multibyte field did not round-trip"
        )

    # Char count UNDER MAX_TSV_FIELD_BYTES, but byte count OVER it: 4-byte
    # astral characters, ~4.1M of them => ~4.1M chars (well under the
    # 16,777,216 char/byte cap numerically) but ~16.4M bytes (over the cap).
    # If _check_field_sizes ever counted `len(value)` (chars) instead of
    # `len(value.encode("utf-8"))` (bytes), this value's admit/reject
    # decision would flip.
    astral_char = "\U00020000"  # 4 bytes in UTF-8, 1 Python char
    n_chars = (object_records.MAX_TSV_FIELD_BYTES // 4) + 100  # chars: just over cap/4
    over_by_bytes_under_by_chars = astral_char * n_chars
    char_len = len(over_by_bytes_under_by_chars)
    byte_len = len(over_by_bytes_under_by_chars.encode("utf-8"))
    assert char_len < object_records.MAX_TSV_FIELD_BYTES, "sanity: char count must be under the cap"
    assert byte_len > object_records.MAX_TSV_FIELD_BYTES, "sanity: byte count must be over the cap"

    for collection in ("classic_coll", "append_coll"):
        with pytest.raises(object_records.InvalidRecordPayloadError):
            object_records.create_collection_record(
                collection,
                {"id": "byte-over-char-under", "value": over_by_bytes_under_by_chars},
                base_dir=data_dir,
                roots=[],
            )


# =============================================================================
# 4. BOM (U+FEFF)
# =============================================================================


@pytest.mark.parametrize(
    "label,value",
    [
        ("start", "﻿hello world"),
        ("middle", "hello ﻿ world"),
        ("end", "hello world﻿"),
        ("only", "﻿"),
    ],
)
def test_bom_survives_raw_and_json_append_and_classic(tmp_path, monkeypatch, label, value):
    """U+FEFF (BOM / zero-width no-break space) at start, middle, end of a
    value, and a value that IS only a BOM -- must not be stripped, moved, or
    misinterpreted as a stream-level encoding marker by the csv/text layer
    (object_records opens files with explicit encoding="utf-8", not
    "utf-8-sig", so a leading U+FEFF in a CELL VALUE should be indistinguishable
    from any other codepoint to this layer)."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_schema(data_dir, "classic_coll", [ID_FIELD, VALUE_FIELD])
    write_append_schema(data_dir, "append_coll", [ID_FIELD, VALUE_FIELD])

    _roundtrip_raw(data_dir, "classic_coll", f"bom-{label}", value)
    _roundtrip_raw(data_dir, "append_coll", f"bom-{label}", value)

    payload = {"note": value}
    _roundtrip_json(data_dir, "classic_coll", f"bom-json-{label}", payload)
    _roundtrip_json(data_dir, "append_coll", f"bom-json-{label}", payload)


# =============================================================================
# 5. Combining sequences -- NFC vs NFD must round-trip byte-exact (no
#    silent normalization by the store)
# =============================================================================


def test_nfc_vs_nfd_are_distinct_and_survive_byte_exact(tmp_path, monkeypatch):
    """"e" + U+0301 (combining acute accent, NFD) and precomposed "é"
    (U+00E9, NFC) are canonically equivalent under Unicode normalization but
    are DIFFERENT byte sequences. A storage layer must never silently
    normalize one to the other -- this test asserts the two forms remain
    distinguishable (their raw bytes differ) AND each survives its own
    round trip unchanged, both raw and JSON-wrapped, both storage modes,
    cold by-id read."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_schema(data_dir, "classic_coll", [ID_FIELD, VALUE_FIELD])
    write_append_schema(data_dir, "append_coll", [ID_FIELD, VALUE_FIELD])

    # Sanity: these two Python strings really are different byte sequences,
    # not accidentally identical (confirms this test is exercising what it
    # claims to).
    assert NFC_STRING != NFD_STRING
    assert NFC_STRING.encode("utf-8") != NFD_STRING.encode("utf-8")
    assert unicodedata.normalize("NFC", NFD_STRING) == NFC_STRING  # canonically equivalent

    for collection in ("classic_coll", "append_coll"):
        _roundtrip_raw(data_dir, collection, f"nfc-{collection}", NFC_STRING)
        _roundtrip_raw(data_dir, collection, f"nfd-{collection}", NFD_STRING)

        # The store must not have coalesced the two into the same bytes.
        _clear_caches()
        nfc_got = object_records.get_collection_record(
            collection, f"nfc-{collection}", base_dir=data_dir, roots=[]
        )
        _clear_caches()
        nfd_got = object_records.get_collection_record(
            collection, f"nfd-{collection}", base_dir=data_dir, roots=[]
        )
        assert nfc_got["value"] != nfd_got["value"] or NFC_STRING == NFD_STRING, (
            f"{collection}: NFC and NFD forms were silently normalized to the "
            f"same stored value -- store must preserve the exact form given. "
            f"nfc={nfc_got['value']!r} nfd={nfd_got['value']!r}"
        )
        assert nfc_got["value"] == NFC_STRING
        assert nfd_got["value"] == NFD_STRING

        payload_nfc = {"note": NFC_STRING}
        payload_nfd = {"note": NFD_STRING}
        _roundtrip_json(data_dir, collection, f"nfc-json-{collection}", payload_nfc)
        _roundtrip_json(data_dir, collection, f"nfd-json-{collection}", payload_nfd)


# =============================================================================
# 6. A multibyte character's UTF-8 bytes straddling a naive row-boundary split
# =============================================================================


def test_multibyte_char_straddling_row_boundary_bytes(tmp_path, monkeypatch):
    """Construct rows so that a multibyte character's byte span sits right
    where a naive fixed-width or byte-count-based row splitter (as opposed
    to the real csv/text-based row delimiting object_records.py actually
    uses) would be tempted to cut -- e.g. a value engineered so the
    cumulative byte offset right before/after it lands on suspicious
    boundaries (mod 2, mod 3, mod 4 relative to the row start), stressing
    that ROW delimiting is happening on '\\n'/csv structure and never on a
    raw byte count that could bisect a multibyte sequence.

    Layout: several small rows, each ending in a different multibyte
    character (2/3/4-byte), each row's total byte length deliberately
    NOT a clean multiple of the multibyte char's own width, so the trailing
    character's bytes don't line up neatly with the row's total byte count
    -- if row-splitting ever degraded to a byte-oriented (not char/csv-
    oriented) scan, misalignment here would be likely to surface as
    corruption on the NEXT row rather than silently working by accident."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "straddle", [ID_FIELD, VALUE_FIELD])

    rows = {
        "R1": "x" * 1 + "café",       # ends in 2-byte char (é), odd prefix length
        "R2": "xx" * 1 + "日本語",     # ends in 3-byte chars, even prefix length
        "R3": "x" * 3 + "\U00020000",  # ends in 4-byte astral char, odd prefix length
        "R4": "xxxx" + "Ω" + "x",      # 2-byte char in the MIDDLE, ascii after
        "R5": "plain ascii tail",
    }
    for record_id, value in rows.items():
        object_records.create_collection_record(
            "straddle", {"id": record_id, "value": value}, base_dir=data_dir, roots=[]
        )

    for record_id, expected in rows.items():
        _clear_caches()
        got = object_records.get_collection_record("straddle", record_id, base_dir=data_dir, roots=[])
        assert got["value"] == expected, (
            f"FINDING: '{record_id}' did not round-trip byte-exact -- possible "
            f"row-boundary/byte-offset misalignment around a multibyte "
            f"character. Expected {expected!r}, got {got['value']!r}"
        )

    _clear_caches()
    all_records = {r["id"]: r["value"] for r in object_records.read_collection_records(
        "straddle", base_dir=data_dir, roots=[]
    )}
    assert all_records == rows
