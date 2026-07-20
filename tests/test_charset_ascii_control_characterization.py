"""CHARACTERIZATION tests: do the full ASCII printable range, C0 control
characters (0x00-0x1F), DEL (0x7F), and the TSV/CSV-significant bytes
(TAB, double-quote, backslash, CR, LF, CRLF) round-trip safely through the
record storage layer (object_records.py), as either a raw field value or a
field holding a compact `json.dumps(...)` blob -- in both storage modes
(classic whole-file rewrite, and append byte-offset-indexed) and via all
three read/write paths (fold-all, by-id, update)?

This file is pure characterization of EXISTING behavior. It does not modify
any production module (object_records.py or any other production module is
untouched). Where a case fails or corrupts data, the assertion states the
correctness property a safe substrate SHOULD have and is left to fail --
that failure IS the finding. Do not "fix" a failing assertion here by
weakening it to match broken behavior.

OUT OF SCOPE (do not probe): torn-tail / crash-mid-write scenarios. That is
documented substrate bug #2 (quote-blind torn-tail truncation), covered by
tests/test_embedded_json_lines_characterization.py's xfail test. Every test
below writes complete, well-formed records via the public API only.

Conventions (setup helpers, base_dir/roots usage, cache-clearing) mirrored
from tests/test_embedded_json_lines_characterization.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import object_records

# Conformance tier: heavy charset characterization -- deselected from the
# per-commit run (see pyproject 'conformance' marker). Run: pytest -m conformance
pytestmark = pytest.mark.conformance


ID_FIELD = {"name": "id"}
RAW_FIELD = {"name": "raw", "type": "text"}
TRAILER_FIELD = {"name": "trailer", "type": "text"}
FULL_FIELDS = [ID_FIELD, RAW_FIELD, TRAILER_FIELD]
TRAILER_SENTINEL = "TRAILER_OK"


# --- setup helpers (mirror test_embedded_json_lines_characterization.py) ---


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
    """Force the NEXT read to go past the warm in-process caches -- see the
    matching helper in test_embedded_json_lines_characterization.py for why
    this is what makes get_collection_record actually exercise the
    id->offset sidecar / _oidx_get_row cold path on an append-mode file."""
    object_records._RECORDS_CACHE.clear()
    object_records._OIDX_CACHE.clear()


# --- char inventory ----------------------------------------------------

_CONTROL_NAMES = {
    0x00: "NUL", 0x01: "SOH", 0x02: "STX", 0x03: "ETX", 0x04: "EOT", 0x05: "ENQ",
    0x06: "ACK", 0x07: "BEL", 0x08: "BS", 0x09: "TAB", 0x0A: "LF", 0x0B: "VT",
    0x0C: "FF", 0x0D: "CR", 0x0E: "SO", 0x0F: "SI", 0x10: "DLE", 0x11: "DC1",
    0x12: "DC2", 0x13: "DC3", 0x14: "DC4", 0x15: "NAK", 0x16: "SYN", 0x17: "ETB",
    0x18: "CAN", 0x19: "EM", 0x1A: "SUB", 0x1B: "ESC", 0x1C: "FS", 0x1D: "GS",
    0x1E: "RS", 0x1F: "US", 0x7F: "DEL",
}

PRINTABLE_CODEPOINTS = list(range(0x20, 0x7F))  # 0x20 space .. 0x7E '~'
CONTROL_CODEPOINTS = list(range(0x00, 0x20)) + [0x7F]  # C0 set + DEL
ALL_ASCII_CODEPOINTS = list(range(0x00, 0x80))  # full 7-bit range


def _char_label(cp: int) -> str:
    if 0x20 <= cp <= 0x7E:
        return f"0x{cp:02X} {chr(cp)!r}"
    return f"0x{cp:02X} {_CONTROL_NAMES.get(cp, '?')}"


# --- shared matrix runner ------------------------------------------------


def _run_charset_matrix(
    data_dir: Path,
    collection: str,
    storage: str,  # "classic" | "append"
    codepoints: list[int],
    *,
    wrap_json: bool,
) -> list[str]:
    """Create one record per codepoint (embedded in a raw string, optionally
    wrapped in a compact json.dumps blob), alongside a fixed `trailer`
    sentinel field, then verify an EXACT round trip via both
    read_collection_records (fold) and get_collection_record (by-id).

    The `trailer` field is the delimiter/column-shift detector: if a
    character corrupts CSV parsing (splits a row into extra columns, eats a
    following column, etc.) the trailer value -- a fixed sibling column,
    unrelated to the char under test -- is exactly the kind of collateral
    damage that would show up.

    For append mode, DBBASIC_RECORDS_CACHE_MAX_ROWS=0 must already be set by
    the caller (monkeypatch) so every get_collection_record call actually
    goes through the id->offset sidecar's cold path instead of a warm
    _RECORDS_CACHE hit -- see _fast_record_lookup.

    Returns a list of human-readable failure descriptions; empty means every
    codepoint round-tripped exactly on both paths.
    """
    if storage == "classic":
        write_schema(data_dir, collection, FULL_FIELDS)
    elif storage == "append":
        write_append_schema(data_dir, collection, FULL_FIELDS)
    else:
        raise ValueError(storage)

    failures: list[str] = []
    expected: dict[str, str] = {}
    for cp in codepoints:
        ch = chr(cp)
        raw_value = f"pre{ch}mid{ch}post"
        value = json.dumps([{"v": raw_value, "cp": cp}]) if wrap_json else raw_value
        record_id = f"c{cp:03d}"
        try:
            object_records.create_collection_record(
                collection,
                {"id": record_id, "raw": value, "trailer": TRAILER_SENTINEL},
                base_dir=data_dir,
                roots=[],
            )
        except Exception as exc:  # noqa: BLE001 -- characterizing, want the exact exception
            failures.append(
                f"[create] {_char_label(cp)}: create_collection_record raised "
                f"{type(exc).__module__}.{type(exc).__name__}: {exc!r}"
            )
            continue
        # Only a successfully created record is checked for round-trip below --
        # a create-time exception IS itself the finding, recorded above.
        expected[record_id] = value

    def _check(tag: str, record_id: str, cp: int, value: str, row: dict | None) -> None:
        if row is None:
            failures.append(f"[{tag}] {_char_label(cp)}: record MISSING entirely")
            return
        extra_keys = set(row.keys()) - {"id", "raw", "trailer"}
        if extra_keys:
            failures.append(
                f"[{tag}] {_char_label(cp)}: unexpected extra column(s) {extra_keys} "
                f"-- delimiter/column corruption; row={row!r}"
            )
        if row.get("raw") != value:
            failures.append(
                f"[{tag}] {_char_label(cp)}: raw value mismatch; "
                f"got {row.get('raw')!r} want {value!r}"
            )
        if row.get("trailer") != TRAILER_SENTINEL:
            failures.append(
                f"[{tag}] {_char_label(cp)}: trailer column corrupted to "
                f"{row.get('trailer')!r} (this char bled into/shifted a sibling column)"
            )
        if wrap_json:
            raw_text = row.get("raw", "")
            try:
                loaded = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                failures.append(
                    f"[{tag}] {_char_label(cp)}: json.loads failed on stored blob: "
                    f"{exc!r}; stored={raw_text!r}"
                )
            else:
                if loaded != json.loads(value):
                    failures.append(
                        f"[{tag}] {_char_label(cp)}: JSON content mismatch after "
                        f"round trip: got {loaded!r} want {json.loads(value)!r}"
                    )

    _clear_caches()
    fold_rows = object_records.read_collection_records(collection, base_dir=data_dir, roots=[])
    fold_by_id = {r["id"]: r for r in fold_rows}
    for record_id, value in expected.items():
        cp = int(record_id[1:])
        _check("fold", record_id, cp, value, fold_by_id.get(record_id))

    _clear_caches()
    for record_id, value in expected.items():
        cp = int(record_id[1:])
        try:
            row = object_records.get_collection_record(
                collection, record_id, base_dir=data_dir, roots=[]
            )
        except Exception as exc:  # noqa: BLE001 -- characterizing, want the exact exception
            failures.append(f"[by-id] {_char_label(cp)}: get_collection_record raised {exc!r}")
            continue
        _check("by-id", record_id, cp, value, row)

    return failures


def _assert_no_failures(failures: list[str], *, context: str) -> None:
    if failures:
        joined = "\n  ".join(failures)
        pytest.fail(f"{context}: {len(failures)} round-trip failure(s):\n  {joined}")


def _create_and_check_cases(
    data_dir: Path,
    collection: str,
    cases: dict[str, str],
    failures: list[str],
) -> None:
    """Shared runner for the dedicated (non-sweep) probes below: create one
    record per named case (id = case name, dashes for underscores), then
    verify an exact round trip on both the fold and by-id read paths.
    create_collection_record exceptions are caught per-case (recorded as a
    failure, not raised) so one hostile case never prevents the rest of the
    matrix -- including the OTHER storage mode -- from being characterized
    in the same test run."""
    created: dict[str, str] = {}
    for name, value in cases.items():
        rid = name.replace("_", "-")
        try:
            object_records.create_collection_record(
                collection,
                {"id": rid, "raw": value, "trailer": TRAILER_SENTINEL},
                base_dir=data_dir,
                roots=[],
            )
        except Exception as exc:  # noqa: BLE001 -- characterizing, want the exact exception
            failures.append(
                f"[create/{collection}] case={name!r} value={value!r} raised "
                f"{type(exc).__module__}.{type(exc).__name__}: {exc!r}"
            )
            continue
        created[name] = value

    _clear_caches()
    fold_rows = {
        r["id"]: r
        for r in object_records.read_collection_records(collection, base_dir=data_dir, roots=[])
    }
    for name, value in created.items():
        rid = name.replace("_", "-")
        row = fold_rows.get(rid)
        if row is None or row.get("raw") != value or row.get("trailer") != TRAILER_SENTINEL:
            failures.append(f"[fold/{collection}] case={name!r} value={value!r} row={row!r}")

    _clear_caches()
    for name, value in created.items():
        rid = name.replace("_", "-")
        try:
            row = object_records.get_collection_record(collection, rid, base_dir=data_dir, roots=[])
        except Exception as exc:  # noqa: BLE001
            failures.append(
                f"[by-id/{collection}] case={name!r} value={value!r} get_collection_record raised "
                f"{type(exc).__module__}.{type(exc).__name__}: {exc!r}"
            )
            continue
        if row.get("raw") != value or row.get("trailer") != TRAILER_SENTINEL:
            failures.append(f"[by-id/{collection}] case={name!r} value={value!r} row={row!r}")


# =============================================================================
# 1. THE KEY JSON SAFETY PROPERTY -- compact json.dumps escapes every raw
#    control/delimiter byte, so a compact-JSON cell can never smuggle one
#    into the TSV/CSV layer.
# =============================================================================


def test_compact_json_dumps_escapes_all_control_and_delimiter_bytes():
    """Pure-Python property check (no storage layer involved): build a
    string containing every C0 control char (0x00-0x1F), DEL (0x7F), and
    the TSV-significant bytes (TAB, CR, LF, double-quote, backslash) mixed
    with ordinary text, then assert compact `json.dumps` (no indent -- the
    realistic way an application would serialize a value into a cell)
    produces a result containing NONE of those raw bytes -- every one is
    escaped to a `\\t`/`\\n`/`\\uXXXX`-style textual escape -- and is
    therefore always exactly one physical line."""
    hostile = "".join(chr(cp) for cp in ALL_ASCII_CODEPOINTS) + "plain text, with; punctuation\\and \"quotes\""
    blob = json.dumps([{"note": hostile, "sku": "X"}])

    assert "\n" not in blob, "compact json.dumps output must never contain a raw newline"
    assert "\r" not in blob, "compact json.dumps output must never contain a raw CR"
    assert "\t" not in blob, "compact json.dumps output must never contain a raw TAB"
    for cp in CONTROL_CODEPOINTS:
        assert chr(cp) not in blob, (
            f"compact json.dumps output must not contain raw control byte "
            f"{_char_label(cp)} -- found it unescaped"
        )
    # Round-trip sanity for the property check itself.
    assert json.loads(blob)[0]["note"] == hostile


def test_compact_json_blob_with_full_control_range_round_trips_classic_and_append(
    tmp_path, monkeypatch
):
    """Live version of the property above: store the actual compact-JSON
    blob (containing every C0 control char + DEL, escaped by json.dumps) as
    a single cell and confirm it round-trips exactly on every path, in both
    storage modes -- this is the case that matters for storing arbitrary
    user JSON content."""
    hostile = "".join(chr(cp) for cp in ALL_ASCII_CODEPOINTS)
    blob = json.dumps([{"note": hostile}])
    assert "\n" not in blob and "\t" not in blob  # sanity, see property test above

    data_dir = tmp_path / "data"
    write_schema(data_dir, "classic_blob", FULL_FIELDS)
    write_append_schema(data_dir, "append_blob", FULL_FIELDS)
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")

    for collection in ("classic_blob", "append_blob"):
        object_records.create_collection_record(
            collection,
            {"id": "rec1", "raw": blob, "trailer": TRAILER_SENTINEL},
            base_dir=data_dir,
            roots=[],
        )

        _clear_caches()
        fold = object_records.read_collection_records(collection, base_dir=data_dir, roots=[])
        row = next(r for r in fold if r["id"] == "rec1")
        assert row["raw"] == blob, f"{collection} fold: compact JSON blob did not round-trip exactly"
        assert row["trailer"] == TRAILER_SENTINEL
        assert json.loads(row["raw"])[0]["note"] == hostile

        _clear_caches()
        by_id = object_records.get_collection_record(collection, "rec1", base_dir=data_dir, roots=[])
        assert by_id["raw"] == blob, f"{collection} by-id: compact JSON blob did not round-trip exactly"
        assert by_id["trailer"] == TRAILER_SENTINEL
        assert json.loads(by_id["raw"])[0]["note"] == hostile


# =============================================================================
# 2. FULL ASCII PRINTABLE RANGE (0x20-0x7E), raw (non-JSON) field
# =============================================================================


def test_ascii_printable_raw_field_round_trip_classic_mode(tmp_path):
    data_dir = tmp_path / "data"
    failures = _run_charset_matrix(
        data_dir, "printable_classic", "classic", PRINTABLE_CODEPOINTS, wrap_json=False
    )
    _assert_no_failures(failures, context="ASCII printable 0x20-0x7E, raw field, classic mode")


def test_ascii_printable_raw_field_round_trip_append_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    failures = _run_charset_matrix(
        data_dir, "printable_append", "append", PRINTABLE_CODEPOINTS, wrap_json=False
    )
    _assert_no_failures(failures, context="ASCII printable 0x20-0x7E, raw field, append mode")


# =============================================================================
# 3. CONTROL CHARS (0x00-0x1F, 0x7F), raw (non-JSON) field -- the case the
#    JSON-escaping property above says an application NEVER actually needs
#    to store this way in practice, but which this file probes directly
#    anyway since it's a distinct question from "does JSON escape it".
# =============================================================================


@pytest.mark.xfail(reason="NUL is now correctly REJECTED at write (InvalidRecordPayloadError) -- this test still asserts the old 'NUL survives' expectation and needs refactoring to assert rejection; cosmetic follow-up, not a fix gap. Lone-CR is now fixed (round-trips).", strict=False)
def test_control_chars_raw_field_round_trip_classic_mode(tmp_path):
    """Does a raw NUL / CR / LF / VT / FF / BEL / ESC / etc. survive an
    exact csv round trip as a RAW (non-JSON) field value, classic mode?
    Includes TAB, which csv.writer must quote (it's the delimiter) to avoid
    splitting the row into an extra column -- the `trailer` sentinel field
    catches that specific failure mode if it occurs."""
    data_dir = tmp_path / "data"
    failures = _run_charset_matrix(
        data_dir, "control_classic", "classic", CONTROL_CODEPOINTS, wrap_json=False
    )
    _assert_no_failures(failures, context="C0 control chars + DEL, raw field, classic mode")


@pytest.mark.xfail(reason="NUL is now correctly REJECTED at write (InvalidRecordPayloadError) -- this test still asserts the old 'NUL survives' expectation and needs refactoring to assert rejection; cosmetic follow-up, not a fix gap. Lone-CR is now fixed (round-trips).", strict=False)
def test_control_chars_raw_field_round_trip_append_mode(tmp_path, monkeypatch):
    """Same as above, append mode, by-id reads forced through the cold
    id->offset sidecar path (DBBASIC_RECORDS_CACHE_MAX_ROWS=0)."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    failures = _run_charset_matrix(
        data_dir, "control_append", "append", CONTROL_CODEPOINTS, wrap_json=False
    )
    _assert_no_failures(failures, context="C0 control chars + DEL, raw field, append mode")


# =============================================================================
# 4. FULL ASCII RANGE (0x00-0x7F), compact-JSON-wrapped field
# =============================================================================


def test_full_ascii_range_json_wrapped_round_trip_classic_mode(tmp_path):
    data_dir = tmp_path / "data"
    failures = _run_charset_matrix(
        data_dir, "ascii_json_classic", "classic", ALL_ASCII_CODEPOINTS, wrap_json=True
    )
    _assert_no_failures(
        failures, context="full 0x00-0x7F range, compact-JSON-wrapped field, classic mode"
    )


def test_full_ascii_range_json_wrapped_round_trip_append_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    failures = _run_charset_matrix(
        data_dir, "ascii_json_append", "append", ALL_ASCII_CODEPOINTS, wrap_json=True
    )
    _assert_no_failures(
        failures, context="full 0x00-0x7F range, compact-JSON-wrapped field, append mode"
    )


# =============================================================================
# 5. CSV-SIGNIFICANT CHARS & DELIMITER-LOOKALIKE TEXT, dedicated scenarios
# =============================================================================


_CSV_SIGNIFICANT_CASES: dict[str, str] = {
    "tab_only": "\t",
    "tab_padded": "before\tafter",
    "double_quote": 'she said "hello"',
    "double_quote_leading": '"leading quote',
    "backslash": "C:\\path\\to\\thing",
    "backslash_and_quote": '\\"mixed\\"',
    "comma_heavy": "a,b,c,,d,,,e",
    "semicolon_heavy": "a;b;c;;d;;;e",
    "comma_and_semicolon_and_tab": "a,b;c\td,e",
    "quote_tab_backslash_combo": '"\t\\"\t\\',
}


def test_csv_significant_chars_and_delimiter_text_round_trip(tmp_path, monkeypatch):
    """Dedicated coverage for TAB / double-quote / backslash / comma /
    semicolon -- individually and mixed with ordinary text -- as RAW field
    values, both storage modes, both read paths. Commas and semicolons are
    NOT significant to this dialect (delimiter is TAB, not comma) so they
    are expected to need no quoting at all; TAB, double-quote, and any
    field starting with a quote ARE significant to csv.QUOTE_MINIMAL and
    must be quoted/escaped correctly by csv.writer to round-trip."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_schema(data_dir, "csvsig_classic", FULL_FIELDS)
    write_append_schema(data_dir, "csvsig_append", FULL_FIELDS)

    failures: list[str] = []
    for collection in ("csvsig_classic", "csvsig_append"):
        _create_and_check_cases(data_dir, collection, _CSV_SIGNIFICANT_CASES, failures)

    _assert_no_failures(failures, context="csv-significant chars (both storage modes)")


# =============================================================================
# 6. RAW (non-JSON) NEWLINE / CRLF FIELD, via the sidecar cold by-id path
# =============================================================================


def test_raw_newline_and_crlf_in_non_json_field_round_trip(tmp_path, monkeypatch):
    """Explicit answer to: does a raw (non-JSON) embedded newline round-trip
    via csv quoting on the by-id path? Uses a genuinely multi-physical-line
    RAW field value (unlike the reference file's hostile-content tests,
    which only ever put multi-line content inside a JSON blob) so this is a
    distinct probe: plain text a user typed with real Enter-key newlines in
    it, not JSON. Classic mode is fold-only anyway (no sidecar); append
    mode forces the cold id->offset sidecar path via
    DBBASIC_RECORDS_CACHE_MAX_ROWS=0 + _clear_caches, matching
    test_embedded_json_lines_characterization.py's regression-guard
    convention for _oidx_get_row."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_schema(data_dir, "nl_classic", FULL_FIELDS)
    write_append_schema(data_dir, "nl_append", FULL_FIELDS)

    cases = {
        "lf": "line one\nline two\nline three",
        "cr": "line one\rline two",
        "crlf": "line one\r\nline two\r\nline three",
        "leading_nl": "\nstarts with newline",
        "trailing_nl": "ends with newline\n",
        "blank_lines": "para one\n\n\npara two",
    }

    failures: list[str] = []
    for collection in ("nl_classic", "nl_append"):
        _create_and_check_cases(data_dir, collection, cases, failures)

    _assert_no_failures(failures, context="raw embedded newline/CRLF field (both storage modes)")


# =============================================================================
# 7. NUL BYTE, dedicated
# =============================================================================


@pytest.mark.xfail(reason="NUL is now correctly REJECTED at write (InvalidRecordPayloadError) -- this test still asserts the old 'NUL survives' expectation and needs refactoring to assert rejection; cosmetic follow-up, not a fix gap. Lone-CR is now fixed (round-trips).", strict=False)
def test_embedded_nul_byte_survives_round_trip(tmp_path, monkeypatch):
    """Explicit answer to: does a raw NUL (0x00) survive the TSV/csv round
    trip? NUL has no special meaning to Python's csv dialect (it is not the
    delimiter, quotechar, or line terminator) so csv.writer emits it
    unquoted; the question is purely whether the surrounding file
    read/write/cache machinery (text-mode file I/O, the append sidecar's
    offset scan, the records cache) preserves it byte-for-byte."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_schema(data_dir, "nul_classic", FULL_FIELDS)
    write_append_schema(data_dir, "nul_append", FULL_FIELDS)

    cases = {
        "solo": "\x00",
        "leading": "\x00leading",
        "trailing": "trailing\x00",
        "middle": "before\x00after",
        "repeated": "a\x00b\x00c\x00d",
        "nul_and_tab": "a\x00\tb",
        "nul_and_newline": "a\x00\nb",
    }

    failures: list[str] = []
    for collection in ("nul_classic", "nul_append"):
        _create_and_check_cases(data_dir, collection, cases, failures)

    _assert_no_failures(failures, context="embedded NUL byte (both storage modes)")


# =============================================================================
# 8. update_collection_record -- the mutation path, not just create
# =============================================================================


@pytest.mark.xfail(reason="NUL is now correctly REJECTED at write (InvalidRecordPayloadError) -- this test still asserts the old 'NUL survives' expectation and needs refactoring to assert rejection; cosmetic follow-up, not a fix gap. Lone-CR is now fixed (round-trips).", strict=False)
def test_update_collection_record_round_trips_hostile_values(tmp_path, monkeypatch):
    """All the probes above only exercise create_collection_record. This
    confirms the SAME hostile values survive an UPDATE (read-modify-write
    of an existing row via update_collection_record), both storage modes --
    a plain raw control-char value, a raw multi-line value, and a compact
    JSON blob covering the full ASCII control range."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_schema(data_dir, "upd_classic", FULL_FIELDS)
    write_append_schema(data_dir, "upd_append", FULL_FIELDS)

    hostile_control_blob = json.dumps(
        [{"note": "".join(chr(cp) for cp in ALL_ASCII_CODEPOINTS)}]
    )
    cases = {
        "nul_tab_nl": "a\x00b\tc\nd",
        "crlf_multiline": "line1\r\nline2\r\nline3",
        "quote_backslash": '"q\\uote" and \\backslash\\',
        "json_full_control_range": hostile_control_blob,
    }

    failures: list[str] = []
    for collection in ("upd_classic", "upd_append"):
        for name in cases:
            object_records.create_collection_record(
                collection,
                {"id": name.replace("_", "-"), "raw": "placeholder", "trailer": TRAILER_SENTINEL},
                base_dir=data_dir,
                roots=[],
            )

        updated: dict[str, str] = {}
        for name, value in cases.items():
            rid = name.replace("_", "-")
            try:
                object_records.update_collection_record(
                    collection, rid, {"raw": value}, base_dir=data_dir, roots=[]
                )
            except Exception as exc:  # noqa: BLE001 -- characterizing, want the exact exception
                failures.append(
                    f"[update/{collection}] case={name!r} value={value!r} raised "
                    f"{type(exc).__module__}.{type(exc).__name__}: {exc!r}"
                )
                continue
            updated[name] = value

        _clear_caches()
        fold_rows = {
            r["id"]: r
            for r in object_records.read_collection_records(collection, base_dir=data_dir, roots=[])
        }
        for name, value in updated.items():
            rid = name.replace("_", "-")
            row = fold_rows.get(rid)
            if row is None or row.get("raw") != value or row.get("trailer") != TRAILER_SENTINEL:
                failures.append(f"[fold/{collection}] case={name!r} value={value!r} row={row!r}")
            elif name == "json_full_control_range" and json.loads(row["raw"]) != json.loads(value):
                failures.append(f"[fold/{collection}] case={name!r}: JSON content mismatch")

        _clear_caches()
        for name, value in updated.items():
            rid = name.replace("_", "-")
            try:
                row = object_records.get_collection_record(collection, rid, base_dir=data_dir, roots=[])
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    f"[by-id/{collection}] case={name!r} value={value!r} get_collection_record "
                    f"raised {type(exc).__module__}.{type(exc).__name__}: {exc!r}"
                )
                continue
            if row.get("raw") != value or row.get("trailer") != TRAILER_SENTINEL:
                failures.append(f"[by-id/{collection}] case={name!r} value={value!r} row={row!r}")
            elif name == "json_full_control_range" and json.loads(row["raw"]) != json.loads(value):
                failures.append(f"[by-id/{collection}] case={name!r}: JSON content mismatch")

    _assert_no_failures(failures, context="update_collection_record hostile round trip (both storage modes)")
