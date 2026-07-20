"""CHARACTERIZATION tests: does the TSV/CSV record storage substrate
(object_records.py) safely hold emoji / astral-plane (U+10000+, 4-byte
UTF-8) / grapheme-cluster content through both storage modes -- especially
append mode's byte-offset id->offset sidecar (_scan_append_tail /
_oidx_get_row), whose row-span and seek math is byte-offset-based while
emoji content is multi-byte and often multi-codepoint per user-visible
"character" (ZWJ sequences, skin-tone modifiers, regional-indicator flag
pairs, variation selectors, keycap sequences).

Pure characterization of EXISTING behavior. Does not modify any production
module. Where a case fails or corrupts data, the assertion states the
correctness property a safe substrate SHOULD have and is left to fail --
that failure IS the finding. Do not "fix" a failing assertion by weakening
it to match broken behavior.

Explicitly OUT OF SCOPE (per instructions): torn-tail behavior (known
deferred bug #2, see test_embedded_json_lines_characterization.py's
xfail). Nothing here writes a deliberately-truncated file.

Mirrors tests/test_embedded_json_lines_characterization.py's setup
helpers/conventions (write_schema / write_append_schema / _clear_caches).
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
TEXT_FIELD = {"name": "note", "type": "textarea"}
BLOB_FIELD = {"name": "blob", "type": "textarea"}


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
    """Force the NEXT read past the warm in-process caches: a cold
    _RECORDS_CACHE + _OIDX_CACHE is what makes get_collection_record on an
    append-mode file actually exercise the id->offset sidecar / _oidx_get_row
    instead of serving from the ordinary records cache."""
    object_records._RECORDS_CACHE.clear()
    object_records._OIDX_CACHE.clear()


# --- emoji / astral-plane / grapheme-cluster sample strings -----------------

SINGLE_EMOJI = "\U0001F600"  # 😀 U+1F600, 4-byte UTF-8, single codepoint
MANY_EMOJI = SINGLE_EMOJI * 500  # many 4-byte chars in one field

FAMILY_ZWJ = "\U0001F468‍\U0001F469‍\U0001F467‍\U0001F466"  # 👨‍👩‍👧‍👦
PROFESSION_ZWJ = "\U0001F469‍\U0001F4BB"  # 👩‍💻 (woman + ZWJ + laptop)
COUPLE_ZWJ = "\U0001F469‍❤️‍\U0001F468"  # 👩‍❤️‍👨 (woman-heart-man)

SKIN_TONE_THUMBSUP = "\U0001F44D\U0001F3FD"  # 👍🏽 base + Fitzpatrick modifier
SKIN_TONE_ZWJ_COMBO = (
    "\U0001F469\U0001F3FD‍\U0001F4BB"
)  # woman+medium-skin-tone ZWJ laptop (skin tone + ZWJ combined)

FLAG_US = "\U0001F1FA\U0001F1F8"  # 🇺🇸 two regional indicators
FLAG_JP = "\U0001F1EF\U0001F1F5"  # 🇯🇵
FLAGS_ADJACENT = FLAG_US + FLAG_JP + "\U0001F1EC\U0001F1E7"  # US JP GB adjacent

HEART_VARSEL = "❤️"  # ❤️ base heart + emoji presentation selector
TEXT_PRESENTATION = "❤︎"  # ❤︎ same base + TEXT presentation selector (VS15)

KEYCAP_1 = "1️⃣"  # 1️⃣ digit + VS16 + combining keycap
KEYCAP_SEQ = "1️⃣" + "2️⃣" + "3️⃣"  # 1️⃣2️⃣3️⃣

MIXED = (
    "café éèê "  # accented Latin
    "日本語 "  # CJK: 日本語
    + SINGLE_EMOJI
    + " "
    + FAMILY_ZWJ
    + " "
    + FLAG_US
    + " "
    + SKIN_TONE_THUMBSUP
    + " "
    + HEART_VARSEL
    + " "
    + KEYCAP_1
)

ALL_SAMPLES: dict[str, str] = {
    "single_emoji": SINGLE_EMOJI,
    "many_emoji": MANY_EMOJI,
    "family_zwj": FAMILY_ZWJ,
    "profession_zwj": PROFESSION_ZWJ,
    "couple_zwj": COUPLE_ZWJ,
    "skin_tone_thumbsup": SKIN_TONE_THUMBSUP,
    "skin_tone_zwj_combo": SKIN_TONE_ZWJ_COMBO,
    "flag_us": FLAG_US,
    "flags_adjacent": FLAGS_ADJACENT,
    "heart_varsel": HEART_VARSEL,
    "text_presentation": TEXT_PRESENTATION,
    "keycap_1": KEYCAP_1,
    "keycap_seq": KEYCAP_SEQ,
    "mixed": MIXED,
}


# =============================================================================
# 1. RAW FIELD round-trip -- classic mode, fold read AND by-id read
# =============================================================================


@pytest.mark.parametrize("name", list(ALL_SAMPLES.keys()))
def test_raw_field_classic_mode_round_trip(tmp_path, name):
    """A raw (non-JSON) field holding emoji/astral/grapheme-cluster content,
    classic storage: exact string round trip via fold-all read and by-id
    read (classic mode never touches the id->offset sidecar)."""
    value = ALL_SAMPLES[name]
    data_dir = tmp_path / "data"
    write_schema(data_dir, "items", [ID_FIELD, TEXT_FIELD])

    object_records.create_collection_record(
        "items", {"id": "r1", "note": value}, base_dir=data_dir, roots=[]
    )

    _clear_caches()
    all_records = object_records.read_collection_records("items", base_dir=data_dir, roots=[])
    assert len(all_records) == 1
    assert all_records[0]["note"] == value, (
        f"[{name}] classic fold-read: expected {value!r} ({len(value)} codepoints, "
        f"{len(value.encode('utf-8'))} bytes), got {all_records[0]['note']!r}"
    )

    _clear_caches()
    by_id = object_records.get_collection_record("items", "r1", base_dir=data_dir, roots=[])
    assert by_id["note"] == value, (
        f"[{name}] classic by-id read: expected {value!r}, got {by_id['note']!r}"
    )


# =============================================================================
# 2. RAW FIELD round-trip -- append mode, fold read AND cold by-id/sidecar read
# =============================================================================


@pytest.mark.parametrize("name", list(ALL_SAMPLES.keys()))
def test_raw_field_append_mode_round_trip(tmp_path, monkeypatch, name):
    """Same, append storage, with the by-id read forced through the cold
    id->offset sidecar path (DBBASIC_RECORDS_CACHE_MAX_ROWS=0), which is
    the byte-offset-math-sensitive path this file exists to probe."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    value = ALL_SAMPLES[name]
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "items", [ID_FIELD, TEXT_FIELD])

    object_records.create_collection_record(
        "items", {"id": "r1", "note": value}, base_dir=data_dir, roots=[]
    )

    _clear_caches()
    all_records = object_records.read_collection_records("items", base_dir=data_dir, roots=[])
    assert len(all_records) == 1
    assert all_records[0]["note"] == value, (
        f"[{name}] append fold-read: expected {value!r}, got {all_records[0]['note']!r}"
    )

    _clear_caches()
    by_id = object_records.get_collection_record("items", "r1", base_dir=data_dir, roots=[])
    assert by_id["note"] == value, (
        f"[{name}] append by-id (cold sidecar) read: expected {value!r} "
        f"({len(value)} codepoints, {len(value.encode('utf-8'))} bytes), "
        f"got {by_id['note']!r}"
    )
    assert by_id["note"].encode("utf-8") == value.encode("utf-8"), (
        f"[{name}] append by-id read: raw UTF-8 bytes diverged even though "
        f"decoded string comparison may have passed -- check for silently "
        f"split surrogate pairs or a re-encoding round trip"
    )


# =============================================================================
# 3. Inside a compact json.dumps blob -- classic AND append, fold + by-id
# =============================================================================


@pytest.mark.parametrize("name", list(ALL_SAMPLES.keys()))
def test_json_blob_classic_and_append_round_trip(tmp_path, monkeypatch, name):
    """The same content, wrapped as a value inside a compact json.dumps(...)
    blob (the realistic way structured emoji-bearing content is stored),
    both storage modes. json.dumps with default ensure_ascii=True escapes
    non-ASCII as \\uXXXX (surrogate-pair escapes for astral codepoints) --
    that escaping happens BEFORE the TSV/CSV layer ever sees the string, so
    this is a genuinely different code path than the raw-field tests above
    (the literal bytes handed to csv.writer are pure ASCII backslash-u
    escapes, not raw UTF-8 emoji bytes) and must be probed separately."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    value = ALL_SAMPLES[name]
    original = {"sku": f"SKU-{name}", "label": value, "n": 42}
    blob = json.dumps(original)

    for mode, writer in (("classic", write_schema), ("append", write_append_schema)):
        data_dir = tmp_path / f"data_{mode}"
        writer(data_dir, "items", [ID_FIELD, BLOB_FIELD])

        object_records.create_collection_record(
            "items", {"id": "r1", "blob": blob}, base_dir=data_dir, roots=[]
        )

        _clear_caches()
        all_records = object_records.read_collection_records("items", base_dir=data_dir, roots=[])
        assert len(all_records) == 1
        fold_loaded = json.loads(all_records[0]["blob"])
        assert fold_loaded == original, (
            f"[{name}/{mode}] json-blob fold-read: expected {original!r}, got {fold_loaded!r}"
        )

        _clear_caches()
        by_id = object_records.get_collection_record("items", "r1", base_dir=data_dir, roots=[])
        by_id_loaded = json.loads(by_id["blob"])
        assert by_id_loaded == original, (
            f"[{name}/{mode}] json-blob by-id read: expected {original!r}, got {by_id_loaded!r}"
        )
        assert by_id_loaded["label"] == value
        assert len(by_id_loaded["label"]) == len(value)  # codepoint count preserved


def test_json_blob_ensure_ascii_false_classic_and_append_round_trip(tmp_path, monkeypatch):
    """Same as above but with ensure_ascii=False, so the JSON text itself
    carries raw multi-byte UTF-8 emoji bytes (not \\uXXXX escapes) straight
    into the TSV cell -- a different, arguably more hostile, byte pattern
    for the CSV layer to quote/carry."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    original = {"sku": "SKU-mixed", "label": MIXED, "flags": [FLAG_US, FLAG_JP], "n": 7}
    blob = json.dumps(original, ensure_ascii=False)

    for mode, writer in (("classic", write_schema), ("append", write_append_schema)):
        data_dir = tmp_path / f"data_{mode}"
        writer(data_dir, "items", [ID_FIELD, BLOB_FIELD])

        object_records.create_collection_record(
            "items", {"id": "r1", "blob": blob}, base_dir=data_dir, roots=[]
        )

        _clear_caches()
        all_records = object_records.read_collection_records("items", base_dir=data_dir, roots=[])
        assert json.loads(all_records[0]["blob"]) == original, (
            f"[{mode}] ensure_ascii=False fold-read mismatch"
        )

        _clear_caches()
        by_id = object_records.get_collection_record("items", "r1", base_dir=data_dir, roots=[])
        assert json.loads(by_id["blob"]) == original, (
            f"[{mode}] ensure_ascii=False by-id read mismatch"
        )


# =============================================================================
# 4. UPDATE path -- overwrite a record's emoji field, re-read
# =============================================================================


@pytest.mark.parametrize("name", ["family_zwj", "flags_adjacent", "skin_tone_zwj_combo", "many_emoji"])
def test_update_record_emoji_field_round_trip(tmp_path, monkeypatch, name):
    """update_collection_record on a field that already holds -- and is
    being replaced with different -- emoji/grapheme-cluster content, both
    modes, by-id read forced cold afterwards."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    value = ALL_SAMPLES[name]
    updated_value = value + " updated " + SINGLE_EMOJI

    for mode, writer in (("classic", write_schema), ("append", write_append_schema)):
        data_dir = tmp_path / f"data_{mode}"
        writer(data_dir, "items", [ID_FIELD, TEXT_FIELD])

        object_records.create_collection_record(
            "items", {"id": "r1", "note": "placeholder"}, base_dir=data_dir, roots=[]
        )
        object_records.update_collection_record(
            "items", "r1", {"note": value}, base_dir=data_dir, roots=[]
        )
        object_records.update_collection_record(
            "items", "r1", {"note": updated_value}, base_dir=data_dir, roots=[]
        )

        _clear_caches()
        by_id = object_records.get_collection_record("items", "r1", base_dir=data_dir, roots=[])
        assert by_id["note"] == updated_value, (
            f"[{name}/{mode}] update round-trip: expected {updated_value!r}, "
            f"got {by_id['note']!r}"
        )


# =============================================================================
# 5. OFFSET PROBE: append-mode sidecar byte math, emoji-heavy EARLY rows,
#    later rows read BY ID with a cold cache to force the sidecar path.
# =============================================================================


def test_offset_probe_emoji_heavy_early_rows_do_not_corrupt_later_by_id_reads(tmp_path, monkeypatch):
    """THE central question this file exists to answer for append mode:
    _scan_append_tail computes each physical row's byte span by re-encoding
    consumed TEXT to UTF-8 bytes (see object_records.py's docstring on that
    function), not by assuming 1 char == 1 byte -- so a run of early rows
    stuffed with 4-byte astral emoji (which inflate byte length far more
    than char length) should not throw off the byte offsets computed for
    rows written AFTER them. This test forces the sidecar cold on every
    point op and reads LATER records by id, asserting exact round trip,
    after EARLIER records were written with large emoji payloads.
    """
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "items", [ID_FIELD, TEXT_FIELD])

    # Early records: heavy emoji content (4-byte-per-char inflation), some
    # with ZWJ sequences / flags / skin tones mixed in, to shift byte
    # offsets substantially relative to char offsets.
    early = {
        "A": MANY_EMOJI,  # 500 x 4-byte emoji = 2000 bytes in one field
        "B": FAMILY_ZWJ * 100,
        "C": FLAGS_ADJACENT * 100,
        "D": SKIN_TONE_ZWJ_COMBO * 100,
    }
    for rec_id, value in early.items():
        object_records.create_collection_record(
            "items", {"id": rec_id, "note": value}, base_dir=data_dir, roots=[]
        )

    # Later records: plain, distinguishable content -- correctness here is
    # a direct probe of whether the sidecar's row offsets (computed while
    # scanning past A-D's multi-byte content) landed in the right place.
    later = {
        "E": "later-record-E " + SINGLE_EMOJI,
        "F": "later-record-F " + FLAG_JP,
        "G": "later-record-G plain ascii",
        "H": "later-record-H " + HEART_VARSEL + KEYCAP_1,
    }
    for rec_id, value in later.items():
        object_records.create_collection_record(
            "items", {"id": rec_id, "note": value}, base_dir=data_dir, roots=[]
        )

    for rec_id, value in later.items():
        _clear_caches()  # force cold cache -> id->offset sidecar path
        got = object_records.get_collection_record("items", rec_id, base_dir=data_dir, roots=[])
        assert got["note"] == value, (
            f"OFFSET-PROBE FINDING: by-id read of '{rec_id}' (written AFTER "
            f"emoji-heavy early rows A-D) returned wrong content -- the "
            f"append sidecar's byte-offset math may be corrupted by 4-byte "
            f"UTF-8 content in earlier rows. Expected {value!r}, got "
            f"{got['note']!r}"
        )

    # Also confirm the early (emoji-heavy) records themselves still resolve
    # correctly by id via the same cold sidecar path.
    for rec_id, value in early.items():
        _clear_caches()
        got = object_records.get_collection_record("items", rec_id, base_dir=data_dir, roots=[])
        assert got["note"] == value, (
            f"OFFSET-PROBE FINDING: by-id read of emoji-heavy early record "
            f"'{rec_id}' itself is wrong. Expected {value!r}, got {got['note']!r}"
        )

    # And a fold-all read agrees with every by-id read.
    _clear_caches()
    all_records = {r["id"]: r["note"] for r in object_records.read_collection_records(
        "items", base_dir=data_dir, roots=[]
    )}
    for rec_id, value in {**early, **later}.items():
        assert all_records[rec_id] == value, (
            f"OFFSET-PROBE FINDING: fold-all read disagrees with by-id read for '{rec_id}'"
        )


def test_offset_probe_emoji_heavy_row_update_then_later_rows_still_resolve(tmp_path, monkeypatch):
    """Same offset-probe idea but adding an UPDATE of an early emoji-heavy
    record (a second physical row superseding the first, shifting the tail
    of the file again) before re-checking later records by id -- mirrors
    the update-then-recheck shape of the offset-index crux test in
    test_embedded_json_lines_characterization.py, but with emoji payloads
    instead of embedded-newline JSON."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "items", [ID_FIELD, TEXT_FIELD])

    object_records.create_collection_record(
        "items", {"id": "A", "note": "a-v1"}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "items", {"id": "B", "note": MANY_EMOJI}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "items", {"id": "C", "note": "c-v1 " + FLAGS_ADJACENT}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "items", {"id": "D", "note": "d-v1 " + FAMILY_ZWJ}, base_dir=data_dir, roots=[]
    )

    _clear_caches()
    rec_c = object_records.get_collection_record("items", "C", base_dir=data_dir, roots=[])
    assert rec_c["note"] == "c-v1 " + FLAGS_ADJACENT

    _clear_caches()
    rec_d = object_records.get_collection_record("items", "D", base_dir=data_dir, roots=[])
    assert rec_d["note"] == "d-v1 " + FAMILY_ZWJ

    # Update B (emoji-heavy) -- a second physical row, superseding the
    # first, shifting every subsequent byte offset again.
    b_v2 = MANY_EMOJI + SKIN_TONE_ZWJ_COMBO * 50
    object_records.update_collection_record(
        "items", "B", {"note": b_v2}, base_dir=data_dir, roots=[]
    )

    _clear_caches()
    rec_c2 = object_records.get_collection_record("items", "C", base_dir=data_dir, roots=[])
    assert rec_c2["note"] == "c-v1 " + FLAGS_ADJACENT, (
        "OFFSET-PROBE FINDING: record C's by-id read is wrong after "
        "updating emoji-heavy record B (second physical row)."
    )

    _clear_caches()
    rec_d2 = object_records.get_collection_record("items", "D", base_dir=data_dir, roots=[])
    assert rec_d2["note"] == "d-v1 " + FAMILY_ZWJ, (
        "OFFSET-PROBE FINDING: record D's by-id read is wrong after "
        "updating emoji-heavy record B (second physical row)."
    )

    _clear_caches()
    rec_b = object_records.get_collection_record("items", "B", base_dir=data_dir, roots=[])
    assert rec_b["note"] == b_v2, (
        "OFFSET-PROBE FINDING: record B's own by-id read after its update is wrong."
    )


# =============================================================================
# 6. Byte-exact / codepoint-exact assertions, explicit
# =============================================================================


@pytest.mark.parametrize("name", list(ALL_SAMPLES.keys()))
def test_byte_exact_and_codepoint_exact_no_surrogate_split_no_dropped_joiner(
    tmp_path, monkeypatch, name
):
    """Explicit assertions beyond simple string equality: codepoint COUNT
    preserved (nothing collapsed/expanded), raw UTF-8 BYTES preserved
    (nothing silently re-encoded/mangled), and -- for the ZWJ/modifier/flag
    samples specifically -- every individual codepoint in the original
    sequence appears, in the SAME ORDER, in the read-back value (so a U+200D
    ZWJ joiner or a U+1F3FD skin-tone modifier can't have been silently
    dropped or reordered while the surrounding text still happens to look
    right under loose comparison)."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    value = ALL_SAMPLES[name]
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "items", [ID_FIELD, TEXT_FIELD])

    object_records.create_collection_record(
        "items", {"id": "r1", "note": value}, base_dir=data_dir, roots=[]
    )

    _clear_caches()
    by_id = object_records.get_collection_record("items", "r1", base_dir=data_dir, roots=[])
    got = by_id["note"]

    assert len(got) == len(value), (
        f"[{name}] codepoint count changed: expected {len(value)}, got {len(got)} "
        f"-- original codepoints={[hex(ord(c)) for c in value]!r}, "
        f"got codepoints={[hex(ord(c)) for c in got]!r}"
    )
    assert got.encode("utf-8") == value.encode("utf-8"), (
        f"[{name}] raw UTF-8 bytes diverged: "
        f"expected {value.encode('utf-8')!r}, got {got.encode('utf-8')!r}"
    )
    assert [ord(c) for c in got] == [ord(c) for c in value], (
        f"[{name}] codepoint sequence diverged (reorder/drop/insert): "
        f"expected {[hex(ord(c)) for c in value]!r}, got {[hex(ord(c)) for c in got]!r}"
    )
    # No lone surrogates: a well-formed decode of valid UTF-8 never produces
    # one, but assert explicitly since a torn 4-byte sequence is exactly
    # the kind of corruption a byte-offset bug could produce.
    assert not any(0xD800 <= ord(c) <= 0xDFFF for c in got), (
        f"[{name}] read-back value contains a lone surrogate codepoint -- "
        f"evidence of a split 4-byte UTF-8 sequence: {got!r}"
    )
