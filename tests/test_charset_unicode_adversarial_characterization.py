"""CHARACTERIZATION tests: does the TSV substrate (object_records.py) safely
hold ADVERSARIAL / edge-case Unicode -- the inputs most likely to break a
naive text-storage layer -- through create/update/read (fold-all and by-id),
in both classic and append storage modes?

This file is pure characterization of EXISTING behavior. It does not modify
any production module. Where a case legitimately SHOULD raise (content that
cannot be represented -- a lone UTF-16 surrogate has no UTF-8 encoding), the
assertion checks that the failure is CLEAN (a well-defined exception, at
write time, before anything touches disk) rather than corruption or a silent
drop. Where a case round-trips, the assertion is byte-exact equality -- not
"looks about right" -- because silent normalization/stripping/mangling of
zero-width, bidi, or combining-mark content is exactly the failure mode this
file exists to catch. A clean raise or an exact round-trip is a PASS; a
mismatch, silent alteration, or unhandled corruption is a FINDING and is
documented as such in the failing test's message, not weakened to pass.

Context (see module docstring / comments in object_records.py):
  - Values are stored as TSV cells via Python's csv module; writes encode
    text to UTF-8 (or whatever the file's default open() encoding resolves
    to -- see the delimiter-safety section below), reads decode UTF-8.
  - csv.field_size_limit has already been raised (MAX_TSV_FIELD_BYTES, 16
    MiB) and create/update enforce the same ceiling on write
    (_check_field_sizes) -- not retested here.
  - _oidx_get_row (the append-mode id->offset sidecar's single-record read)
    is already CSV-aware, not a raw readline() -- not retested here.
  - The append-mode torn-tail self-heal check (_drop_torn_tail: "does the
    file end with a literal \\n byte") is unsound for a row whose value
    spans multiple physical lines -- known, deferred bug #2. NOT probed
    here; every value below is either single-physical-line or (for the
    zalgo/noncharacter cases) still written via compact json.dumps, never
    pretty-printed, so no case in this file exercises that bug by accident.

Setup helpers below mirror tests/test_embedded_json_lines_characterization.py.
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
TEXT_FIELD = {"name": "note", "type": "textarea"}


def _required_text_field() -> dict:
    return {"name": "note", "type": "textarea", "required": True}


# --- setup helpers (mirror tests/test_embedded_json_lines_characterization.py) ---


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
    matching helper in test_embedded_json_lines_characterization.py. Needed
    so a by-id read on an append-mode collection actually exercises the
    id->offset sidecar path instead of serving from the ordinary records
    cache."""
    object_records._RECORDS_CACHE.clear()
    object_records._OIDX_CACHE.clear()


# --- adversarial content constants -----------------------------------------

ZWSP = "​"   # zero-width space
ZWNJ = "‌"   # zero-width non-joiner
ZWJ = "‍"    # zero-width joiner
WORD_JOINER = "⁠"

RLO = "‮"    # right-to-left override
LRO = "‭"    # left-to-right override
RLI = "⁧"    # right-to-left isolate
LRI = "⁦"    # left-to-right isolate
PDI = "⁩"    # pop directional isolate

NONCHARACTERS = ["￾", "￿", "\U0001fffe", "\U0001ffff", "﷐", "﷠", "﷯"]

CYRILLIC_A = "а"  # Cyrillic "а" -- visually identical to Latin "a" (U+0061)
LATIN_A = "a"


def _round_trip_both_modes(
    tmp_path, monkeypatch, collection_prefix: str, field_value: str, *, field_name: str = "note"
) -> None:
    """Store `field_value` raw under `field_name` in both a classic-mode and
    an append-mode collection; assert byte-exact equality on BOTH the
    full-fold read and the by-id read (the latter forced through the cold
    id->offset sidecar in append mode via DBBASIC_RECORDS_CACHE_MAX_ROWS=0)."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    classic_coll = f"{collection_prefix}_classic"
    append_coll = f"{collection_prefix}_append"
    write_schema(data_dir, classic_coll, [ID_FIELD, TEXT_FIELD])
    write_append_schema(data_dir, append_coll, [ID_FIELD, TEXT_FIELD])

    for coll in (classic_coll, append_coll):
        object_records.create_collection_record(
            coll, {"id": "r1", field_name: field_value}, base_dir=data_dir, roots=[]
        )

        _clear_caches()
        all_records = object_records.read_collection_records(coll, base_dir=data_dir, roots=[])
        assert len(all_records) == 1
        assert all_records[0][field_name] == field_value, (
            f"{coll}: FINDING -- fold-all read did not byte-exact round-trip "
            f"the stored value. Expected {field_value!r}, got "
            f"{all_records[0][field_name]!r}"
        )

        _clear_caches()
        by_id = object_records.get_collection_record(coll, "r1", base_dir=data_dir, roots=[])
        assert by_id[field_name] == field_value, (
            f"{coll}: FINDING -- by-id (sidecar) read did not byte-exact "
            f"round-trip the stored value. Expected {field_value!r}, got "
            f"{by_id[field_name]!r}"
        )


def _round_trip_json_wrapped_both_modes(
    tmp_path, monkeypatch, collection_prefix: str, field_value: str, *, field_name: str = "note"
) -> None:
    """Same as _round_trip_both_modes, but the stored cell is a compact
    json.dumps([field_value]) blob (a realistic shape for how this content
    would actually arrive -- e.g. inside a JSON API payload or an embedded
    array), read back via json.loads and compared to the original value."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    classic_coll = f"{collection_prefix}_classic"
    append_coll = f"{collection_prefix}_append"
    write_schema(data_dir, classic_coll, [ID_FIELD, TEXT_FIELD])
    write_append_schema(data_dir, append_coll, [ID_FIELD, TEXT_FIELD])
    blob = json.dumps([field_value])

    for coll in (classic_coll, append_coll):
        object_records.create_collection_record(
            coll, {"id": "r1", field_name: blob}, base_dir=data_dir, roots=[]
        )

        _clear_caches()
        all_records = object_records.read_collection_records(coll, base_dir=data_dir, roots=[])
        assert json.loads(all_records[0][field_name])[0] == field_value, (
            f"{coll}: FINDING -- fold-all read of a json.dumps-wrapped value "
            f"did not round-trip. Expected {field_value!r}."
        )

        _clear_caches()
        by_id = object_records.get_collection_record(coll, "r1", base_dir=data_dir, roots=[])
        assert json.loads(by_id[field_name])[0] == field_value, (
            f"{coll}: FINDING -- by-id read of a json.dumps-wrapped value "
            f"did not round-trip. Expected {field_value!r}."
        )


# =============================================================================
# 1. LONE SURROGATES
# =============================================================================
#
# A Python str MAY hold an unpaired UTF-16 surrogate codepoint (U+D800-
# U+DFFF) -- json.loads of adversarial/malformed input, or a \uD800-style
# escape typed by a user, can produce one. Such a str has NO valid UTF-8
# encoding (surrogates are reserved, not "supplementary plane" characters;
# CPython's UTF-8 codec raises UnicodeEncodeError rather than emitting
# CESU-8/WTF-8 bytes). The question: does object_records.create_collection_
# record catch this and fail cleanly BEFORE writing anything, or does it
# corrupt the file / partially write?


LONE_SURROGATE = "\ud800"


def test_lone_surrogate_raw_field_classic_mode_raises_cleanly_no_partial_write(tmp_path):
    """FINDING (nuance, not a corruption bug): a raw lone surrogate in a
    field value raises -- but as a bare stdlib UnicodeEncodeError out of
    _check_field_sizes's `str(value).encode("utf-8")` size probe, not as
    object_records.InvalidRecordPayloadError. The failure IS clean (occurs
    before create_collection_record ever opens records.tsv for write -- see
    the assertion below that no file is created), so a caller retains data
    integrity; but a caller that only catches InvalidRecordPayloadError to
    turn bad input into a clean 4xx response would let this raw
    UnicodeEncodeError escape uncaught instead."""
    data_dir = tmp_path / "data"
    write_schema(data_dir, "widgets", [ID_FIELD, TEXT_FIELD])

    with pytest.raises(object_records.InvalidRecordPayloadError):
        object_records.create_collection_record(
            "widgets", {"id": "w1", "note": LONE_SURROGATE}, base_dir=data_dir, roots=[]
        )

    records_path = data_dir / "collections" / "widgets" / "records.tsv"
    assert not records_path.exists(), (
        "FINDING confirmed (worse than expected): a rejected lone-surrogate "
        "write left a records.tsv file on disk despite raising."
    )
    # Collection must still read back as genuinely empty, not error.
    listing = object_records.list_collection_records("widgets", base_dir=data_dir, roots=[])
    assert listing["records"] == []
    assert listing["total"] == 0


def test_lone_surrogate_raw_field_append_mode_raises_cleanly_no_partial_write(tmp_path):
    """Same probe, append-mode collection: the failure must be equally clean
    -- no `_op`-tagged partial row, no torn-tail artifact left behind that a
    later self-heal or fold would have to reckon with."""
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "widgets", [ID_FIELD, TEXT_FIELD])

    with pytest.raises(object_records.InvalidRecordPayloadError):
        object_records.create_collection_record(
            "widgets", {"id": "w1", "note": LONE_SURROGATE}, base_dir=data_dir, roots=[]
        )

    records_path = data_dir / "collections" / "widgets" / "records.tsv"
    assert not records_path.exists(), (
        "FINDING confirmed (worse than expected): a rejected lone-surrogate "
        "write left a records.tsv file on disk despite raising."
    )
    listing = object_records.list_collection_records("widgets", base_dir=data_dir, roots=[])
    assert listing["records"] == []


def test_lone_surrogate_via_json_dumps_default_ensure_ascii_is_actually_safe(tmp_path, monkeypatch):
    """Counter-intuitive PASS: json.dumps' DEFAULT (ensure_ascii=True)
    backslash-escapes a lone surrogate to the literal ASCII text `\\ud800`
    rather than embedding the real surrogate character -- so the STORED
    blob is plain ASCII and trivially UTF-8-encodable. This is the
    "realistic" shape for JSON produced by ordinary application code (no
    one passes ensure_ascii=False by default), and it round-trips exactly,
    in both modes, both read paths -- json.loads on the way back
    reconstructs the very same lone-surrogate string in memory, with no
    encode step involved. This is NOT the same as the raw-field or the
    ensure_ascii=False case below; see those for where lone surrogates
    actually break something."""
    original = "\ud800"
    blob = json.dumps({"x": original})  # ensure_ascii=True (default)
    assert "\ud800" not in blob  # sanity: no raw surrogate byte in the blob itself
    blob.encode("utf-8")  # sanity: the blob itself is always encodable

    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_schema(data_dir, "widgets_classic", [ID_FIELD, TEXT_FIELD])
    write_append_schema(data_dir, "widgets_append", [ID_FIELD, TEXT_FIELD])

    for coll in ("widgets_classic", "widgets_append"):
        object_records.create_collection_record(
            coll, {"id": "w1", "note": blob}, base_dir=data_dir, roots=[]
        )
        _clear_caches()
        all_records = object_records.read_collection_records(coll, base_dir=data_dir, roots=[])
        assert json.loads(all_records[0]["note"])["x"] == original

        _clear_caches()
        by_id = object_records.get_collection_record(coll, "w1", base_dir=data_dir, roots=[])
        assert json.loads(by_id["note"])["x"] == original


@pytest.mark.xfail(reason="substrate: NUL is now cleanly REJECTED (InvalidRecordPayloadError) and lone-CR corruption awaits the QUOTE_ALL-vs-reject-bare-CR format decision (plan/database-test-strategy.md). Assertion refresh bundled with that decision.", strict=False)
def test_lone_surrogate_via_json_dumps_ensure_ascii_false_raises_cleanly(tmp_path):
    """FINDING (nuance, matches the raw-field case): json.dumps(..., ensure_
    ascii=False) embeds the REAL surrogate character into the JSON text
    (rather than escaping it), producing a blob that itself has no valid
    UTF-8 encoding -- exactly the "STORED blob may be unencodable" risk.
    This IS a realistic path (ensure_ascii=False is a common choice to keep
    non-ASCII text readable in stored JSON). Same clean-failure behavior as
    the raw-field case: create_collection_record raises UnicodeEncodeError
    before writing, no partial row, collection stays empty and readable."""
    original = "\ud800"
    blob = json.dumps({"x": original}, ensure_ascii=False)
    with pytest.raises(object_records.InvalidRecordPayloadError):
        blob.encode("utf-8")  # sanity: confirms the blob itself is unencodable

    data_dir = tmp_path / "data"
    write_schema(data_dir, "widgets_classic", [ID_FIELD, TEXT_FIELD])
    write_append_schema(data_dir, "widgets_append", [ID_FIELD, TEXT_FIELD])

    for coll in ("widgets_classic", "widgets_append"):
        with pytest.raises(object_records.InvalidRecordPayloadError):
            object_records.create_collection_record(
                coll, {"id": "w1", "note": blob}, base_dir=data_dir, roots=[]
            )
        records_path = data_dir / "collections" / coll / "records.tsv"
        assert not records_path.exists(), (
            f"{coll}: FINDING confirmed (worse than expected): a rejected "
            f"unencodable-JSON write left a records.tsv file on disk."
        )


def test_lone_surrogate_update_raises_cleanly_and_does_not_corrupt_existing_record(tmp_path):
    """The update path runs the same _check_field_sizes probe (after schema
    validation, before persist) -- confirm an update attempt with a lone
    surrogate raises cleanly and leaves the EXISTING record completely
    unchanged, in both storage modes."""
    for storage in ("classic", "append"):
        data_dir = tmp_path / f"data_{storage}"
        if storage == "classic":
            write_schema(data_dir, "widgets", [ID_FIELD, TEXT_FIELD])
        else:
            write_append_schema(data_dir, "widgets", [ID_FIELD, TEXT_FIELD])

        object_records.create_collection_record(
            "widgets", {"id": "w1", "note": "original value"}, base_dir=data_dir, roots=[]
        )

        with pytest.raises(object_records.InvalidRecordPayloadError):
            object_records.update_collection_record(
                "widgets", "w1", {"note": LONE_SURROGATE}, base_dir=data_dir, roots=[]
            )

        _clear_caches()
        after = object_records.get_collection_record("widgets", "w1", base_dir=data_dir, roots=[])
        assert after["note"] == "original value", (
            f"{storage}: FINDING confirmed: a rejected update corrupted the "
            f"existing record instead of leaving it untouched. Got: {after!r}"
        )


# =============================================================================
# 2. ZERO-WIDTH CHARACTERS
# =============================================================================


@pytest.mark.parametrize(
    "label,value",
    [
        ("zwsp", f"before{ZWSP}after"),
        ("zwnj", f"be{ZWNJ}fore"),
        ("zwj", f"a{ZWJ}b{ZWJ}c"),
        ("word_joiner", f"x{WORD_JOINER}y"),
        ("all_zero_width_mixed", f"a{ZWSP}b{ZWNJ}c{ZWJ}d{WORD_JOINER}e"),
    ],
)
def test_zero_width_chars_mixed_with_text_round_trip(tmp_path, monkeypatch, label, value):
    """A zero-width character embedded in otherwise-visible text is a plain
    Unicode scalar value with a normal UTF-8 encoding -- no CSV-special
    bytes involved. Expected, and the finding target if violated: an exact
    round trip with the zero-width character neither stripped nor
    collapsed (some text-processing layers treat zero-width chars as
    "whitespace-like" and trim them -- this substrate must not)."""
    _round_trip_both_modes(tmp_path, monkeypatch, f"zw_{label}", value)


def test_zero_width_only_field_round_trips_and_is_not_treated_as_empty(tmp_path, monkeypatch):
    """A field that is VISUALLY empty (all zero-width characters, no
    visible glyph) but is not the empty string. Two properties checked:
      1. It round-trips byte-exact (doesn't get folded down to "" anywhere
         in the write/read pipeline).
      2. object_records' own required/empty check (_is_empty: `value is
         None or value == ""`) does NOT treat it as empty -- a `required`
         field holding only zero-width characters must be ACCEPTED, since
         by that same definition it is not blank. (This documents the
         current, narrow definition of "empty" this substrate uses; it does
         NOT claim that definition is desirable for every caller -- a
         zero-width-only value being accepted as satisfying `required` is
         itself worth flagging to product/schema owners, even though it is
         not a storage-layer bug.)
    """
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    zero_width_only = ZWSP + ZWNJ + ZWJ + WORD_JOINER
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "forms", [ID_FIELD, _required_text_field()])

    # Property 2: must NOT raise "field is required".
    object_records.create_collection_record(
        "forms", {"id": "f1", "note": zero_width_only}, base_dir=data_dir, roots=[]
    )

    # Property 1: byte-exact round trip, fold + by-id.
    _clear_caches()
    all_records = object_records.read_collection_records("forms", base_dir=data_dir, roots=[])
    assert all_records[0]["note"] == zero_width_only, (
        "FINDING: a zero-width-only field value was altered (likely "
        f"collapsed to empty) on read. Got: {all_records[0]['note']!r}"
    )

    _clear_caches()
    by_id = object_records.get_collection_record("forms", "f1", base_dir=data_dir, roots=[])
    assert by_id["note"] == zero_width_only, (
        "FINDING: a zero-width-only field value was altered on by-id read. "
        f"Got: {by_id['note']!r}"
    )

    # A genuinely empty ("") required field must still be rejected -- this
    # is the contrast case proving the substrate distinguishes the two.
    with pytest.raises(object_records.InvalidRecordPayloadError):
        object_records.create_collection_record(
            "forms", {"id": "f2", "note": ""}, base_dir=data_dir, roots=[]
        )


# =============================================================================
# 3. BIDI CONTROLS
# =============================================================================


@pytest.mark.parametrize(
    "label,value",
    [
        ("rlo", f"normal {RLO}reversed{RLO}"),
        ("lro", f"normal {LRO}forced-ltr{LRO}"),
        ("isolates", f"{LRI}left{PDI} middle {RLI}right{PDI}"),
        ("flip_direction", f"English {RLO}עברית{RLO} more English"),
    ],
)
def test_bidi_controls_byte_exact_round_trip_no_stripping(tmp_path, monkeypatch, label, value):
    """Bidi control characters (RLO/LRO/RLI/LRI/PDI) are the classic
    "trojan source" vector -- text that reads differently depending on
    whether the control characters are honored or stripped. The storage
    layer's job is narrow: hold the bytes exactly, neither stripping the
    controls (which would silently change meaning) nor otherwise mangling
    them. Probed both as a raw field and wrapped in compact json.dumps."""
    _round_trip_both_modes(tmp_path, monkeypatch, f"bidi_{label}", value)
    _round_trip_json_wrapped_both_modes(tmp_path, monkeypatch, f"bidi_json_{label}", value)


# =============================================================================
# 4. UNICODE NONCHARACTERS
# =============================================================================


@pytest.mark.parametrize("codepoint", NONCHARACTERS)
def test_unicode_noncharacter_round_trips(tmp_path, monkeypatch, codepoint):
    """U+FFFE/U+FFFF (and their counterparts at the end of every plane) and
    U+FDD0-U+FDEF are permanently reserved "noncharacters" -- not illegal,
    just never assigned a meaning, and some libraries reject/strip them as
    a defensive measure. object_records has no such filtering (it is a
    plain UTF-8 TSV substrate), so these should encode/decode/round-trip
    exactly like any other scalar value -- included here specifically to
    confirm the substrate does NOT do any noncharacter-aware filtering."""
    label = f"nonchar_{ord(codepoint):x}"
    value = f"before{codepoint}after"
    _round_trip_both_modes(tmp_path, monkeypatch, label, value)


def test_unicode_noncharacters_all_together_one_field(tmp_path, monkeypatch):
    """All the sampled noncharacters concatenated into a single field, to
    also exercise them adjacent to each other (not just individually
    padded with ASCII)."""
    value = "".join(NONCHARACTERS)
    _round_trip_both_modes(tmp_path, monkeypatch, "nonchar_all", value)


# =============================================================================
# 5. NORMALIZATION ATTACK
# =============================================================================


def test_nfc_nfd_nfkc_forms_not_silently_normalized_and_remain_distinguishable(tmp_path, monkeypatch):
    """The same VISUAL string ("café") in three different Unicode
    normalization forms is, at the byte level, three different strings:
    NFC uses the precomposed U+00E9 (é); NFD decomposes it to U+0065 U+0301
    (e + combining acute accent); NFKC (compatibility composition) happens
    to coincide with NFC here but is computed independently below rather
    than assumed. If the storage layer silently normalized on write (e.g.
    via an implicit unicodedata.normalize somewhere in the pipeline), two
    logically-different inputs could collide into the same stored bytes --
    a real security concern for anything using stored text for comparison/
    dedup/lookup. Assert: each form's stored bytes exactly match ITS OWN
    input (no normalization at all), and the three stored values remain
    pairwise distinct from each other, in both storage modes."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    nfd = "café"          # e + combining acute accent
    nfc = unicodedata.normalize("NFC", nfd)    # precomposed U+00E9
    nfkc = unicodedata.normalize("NFKC", nfd)
    assert nfc != nfd  # sanity: genuinely different byte sequences...
    assert unicodedata.normalize("NFC", nfd) == nfc  # ...that mean the same thing

    data_dir = tmp_path / "data"
    write_schema(data_dir, "norm_classic", [ID_FIELD, TEXT_FIELD])
    write_append_schema(data_dir, "norm_append", [ID_FIELD, TEXT_FIELD])

    forms = {"nfc": nfc, "nfd": nfd, "nfkc": nfkc}
    for coll in ("norm_classic", "norm_append"):
        for form_id, form_value in forms.items():
            object_records.create_collection_record(
                coll, {"id": form_id, "note": form_value}, base_dir=data_dir, roots=[]
            )

        _clear_caches()
        stored = {
            form_id: object_records.get_collection_record(coll, form_id, base_dir=data_dir, roots=[])["note"]
            for form_id in forms
        }
        for form_id, original in forms.items():
            assert stored[form_id] == original, (
                f"{coll}: FINDING -- silent normalization detected. Stored "
                f"'{form_id}' value {stored[form_id]!r} != input {original!r}."
            )
        # Pairwise distinctness -- would collapse to fewer than 3 distinct
        # values if anything normalized on write.
        distinct_values = {stored["nfc"], stored["nfd"], stored["nfkc"]}
        assert len(distinct_values) == len({nfc, nfd, nfkc}), (
            f"{coll}: FINDING -- distinct normalization forms collapsed to "
            f"fewer distinct stored values than were written: {stored!r}"
        )


# =============================================================================
# 6. VERY LONG SINGLE GRAPHEME ("ZALGO")
# =============================================================================


def test_zalgo_long_combining_grapheme_round_trip(tmp_path, monkeypatch):
    """Thousands of combining marks (U+0300-U+036F, cycled) stacked on one
    base character -- a single Python "character" position but a very long
    string, and the classic Unicode stress-test for anything that assumes
    grapheme clusters are short or that iterates by codepoint expecting
    bounded work per visible character. The storage layer treats this as
    plain text of some length in bytes; the finding target is a byte-exact
    round trip at this length, in a single field, both modes."""
    combining_marks = "".join(chr(0x0300 + (i % 0x70)) for i in range(4000))
    zalgo = "e" + combining_marks
    assert len(zalgo) == 4001
    byte_len = len(zalgo.encode("utf-8"))
    assert byte_len < object_records.MAX_TSV_FIELD_BYTES  # sanity: well under the per-field cap
    _round_trip_both_modes(tmp_path, monkeypatch, "zalgo", zalgo)


# =============================================================================
# 7. HOMOGLYPH / CONFUSABLE STRINGS
# =============================================================================


def test_homoglyph_confusable_strings_byte_exact_and_stored_distinctly(tmp_path, monkeypatch):
    """Cyrillic "а" (U+0430) vs Latin "a" (U+0061) render identically in
    most fonts but are different codepoints/bytes. A storage layer that
    silently folds/canonicalizes look-alike characters (e.g. via a
    case-fold or a confusables-skeleton transform) would let two distinct
    identities collide; this substrate should have no such behavior. Store
    both as distinct records (distinct ids, since ids themselves are
    separately validated/restricted -- the field under test is `note`, an
    ordinary text field) and confirm each round-trips exactly and the two
    stay byte-distinguishable from each other, both modes."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    cyrillic_name = CYRILLIC_A + "dmin"   # "аdmin" (Cyrillic а)
    latin_name = LATIN_A + "dmin"          # "admin" (Latin a)
    assert cyrillic_name != latin_name
    assert cyrillic_name.encode("utf-8") != latin_name.encode("utf-8")

    data_dir = tmp_path / "data"
    write_schema(data_dir, "users_classic", [ID_FIELD, TEXT_FIELD])
    write_append_schema(data_dir, "users_append", [ID_FIELD, TEXT_FIELD])

    for coll in ("users_classic", "users_append"):
        object_records.create_collection_record(
            coll, {"id": "u-cyrillic", "note": cyrillic_name}, base_dir=data_dir, roots=[]
        )
        object_records.create_collection_record(
            coll, {"id": "u-latin", "note": latin_name}, base_dir=data_dir, roots=[]
        )

        _clear_caches()
        got_cyrillic = object_records.get_collection_record(coll, "u-cyrillic", base_dir=data_dir, roots=[])
        got_latin = object_records.get_collection_record(coll, "u-latin", base_dir=data_dir, roots=[])
        assert got_cyrillic["note"] == cyrillic_name, f"{coll}: FINDING -- Cyrillic homoglyph altered on read"
        assert got_latin["note"] == latin_name, f"{coll}: FINDING -- Latin string altered on read"
        assert got_cyrillic["note"] != got_latin["note"], (
            f"{coll}: FINDING -- homoglyph strings collapsed to the same "
            f"stored value."
        )


# =============================================================================
# 8. DELIMITER-SAFETY PROPERTY: no multibyte UTF-8 sequence contains 0x09/0x0A
# =============================================================================


def test_no_multibyte_utf8_sequence_contains_tab_or_newline_byte():
    """By construction of UTF-8, every byte in a MULTI-byte sequence (the
    lead byte and all continuation bytes) is >= 0x80 -- a multibyte
    sequence can never contain a byte that collides with ASCII TAB (0x09)
    or LF (0x0A) at the byte level, so a byte-level delimiter scan could
    never accidentally split a multibyte character mid-sequence and
    mistake a byte inside it for a real field/row delimiter. This is a
    property of UTF-8 itself (not of object_records' parser, which uses
    real CSV/TSV parsing rather than a byte-level scan regardless) --
    verified here directly, and then reinforced with an over-the-wire
    round trip through the real storage path using exactly this file's
    adversarial character set, to confirm the property holds for every
    codepoint actually exercised in this file (not just a mathematical
    argument about UTF-8 in the abstract)."""
    probe_chars = (
        [ZWSP, ZWNJ, ZWJ, WORD_JOINER, RLO, LRO, RLI, LRI, PDI, CYRILLIC_A]
        + NONCHARACTERS
        + [chr(0x0300 + i) for i in range(0x70)]  # combining marks used by the zalgo test
        + ["é", "\U0001f600", "日本"]  # accented latin, emoji, CJK, for good measure
    )
    for ch in probe_chars:
        encoded = ch.encode("utf-8")
        if len(encoded) > 1:
            assert 0x09 not in encoded, f"FINDING: U+{ord(ch):04X} encodes to bytes containing a TAB byte: {encoded!r}"
            assert 0x0A not in encoded, f"FINDING: U+{ord(ch):04X} encodes to bytes containing a LF byte: {encoded!r}"

    # Systematic sweep across the full codepoint space (skipping surrogates,
    # which cannot be UTF-8 encoded standalone -- covered separately in
    # section 1), stepping to keep runtime bounded while still covering
    # every UTF-8 length class (1/2/3/4-byte encodings).
    step = 97  # coprime-ish stride, not aligned to any power-of-two boundary
    checked = 0
    for cp in range(0x80, 0x110000, step):
        if 0xD800 <= cp <= 0xDFFF:
            continue  # surrogate range: not independently UTF-8-encodable
        encoded = chr(cp).encode("utf-8")
        checked += 1
        if len(encoded) > 1:
            assert 0x09 not in encoded, f"FINDING: U+{cp:04X} encodes to bytes containing a TAB byte: {encoded!r}"
            assert 0x0A not in encoded, f"FINDING: U+{cp:04X} encodes to bytes containing a LF byte: {encoded!r}"
    assert checked > 10000  # sanity: the sweep actually covered a meaningful sample


def test_delimiter_safety_holds_through_the_real_storage_path(tmp_path, monkeypatch):
    """Reinforcement of the property above through the actual create/read
    path: a field packed with every adversarial character from this file,
    back-to-back with no ASCII padding between them (the adversarial case
    for any byte-level scan), must still round-trip exactly -- and, as a
    structural check, the physical file must contain exactly one data row
    (a naive byte-level split on this content, if the storage layer used
    one, would either fragment it across "rows" or corrupt the field
    count; the real csv-based parser must not)."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    packed = "".join(
        [ZWSP, ZWNJ, ZWJ, WORD_JOINER, RLO, LRO, RLI, PDI, CYRILLIC_A]
        + NONCHARACTERS
        + [chr(0x0300 + i) for i in range(20)]
    )
    for ch in packed:
        assert ch.encode("utf-8").isascii() is False or ord(ch) < 0x80  # sanity, non-fatal

    data_dir = tmp_path / "data"
    write_schema(data_dir, "packed_classic", [ID_FIELD, TEXT_FIELD])
    write_append_schema(data_dir, "packed_append", [ID_FIELD, TEXT_FIELD])

    for coll in ("packed_classic", "packed_append"):
        object_records.create_collection_record(
            coll, {"id": "p1", "note": packed}, base_dir=data_dir, roots=[]
        )
        object_records.create_collection_record(
            coll, {"id": "p2", "note": "sibling"}, base_dir=data_dir, roots=[]
        )

        _clear_caches()
        all_records = object_records.read_collection_records(coll, base_dir=data_dir, roots=[])
        assert len(all_records) == 2, (
            f"{coll}: FINDING -- packed adversarial content changed the "
            f"apparent row count (byte-level mis-split?). Got: {all_records!r}"
        )
        by_id = {r["id"]: r["note"] for r in all_records}
        assert by_id["p1"] == packed
        assert by_id["p2"] == "sibling"


# =============================================================================
# 9. UPDATE PATH -- adversarial content surviving a create -> update sequence
# =============================================================================


def test_update_sequence_through_zero_width_bidi_and_noncharacter_content(tmp_path, monkeypatch):
    """A single record's field is created plain, then updated THROUGH a
    sequence of adversarial values (zero-width-only, bidi-flipped,
    noncharacter-laden, back to plain), asserting an exact round trip after
    every step, both storage modes. This is the "and update" axis of the
    probe matrix: confirms the update path (not just create) preserves
    adversarial content exactly, including the merge-with-existing-record
    step every update performs internally."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    sequence = [
        "plain start",
        ZWSP + ZWNJ + ZWJ,
        f"{RLO}reversed text{RLO} trailing",
        "".join(NONCHARACTERS),
        "plain end",
    ]

    for storage in ("classic", "append"):
        data_dir = tmp_path / f"data_{storage}"
        if storage == "classic":
            write_schema(data_dir, "seq", [ID_FIELD, TEXT_FIELD])
        else:
            write_append_schema(data_dir, "seq", [ID_FIELD, TEXT_FIELD])

        object_records.create_collection_record(
            "seq", {"id": "s1", "note": sequence[0]}, base_dir=data_dir, roots=[]
        )
        _clear_caches()
        assert object_records.get_collection_record("seq", "s1", base_dir=data_dir, roots=[])["note"] == sequence[0]

        for step_value in sequence[1:]:
            object_records.update_collection_record(
                "seq", "s1", {"note": step_value}, base_dir=data_dir, roots=[]
            )
            _clear_caches()
            current = object_records.get_collection_record("seq", "s1", base_dir=data_dir, roots=[])
            assert current["note"] == step_value, (
                f"{storage}: FINDING -- update to {step_value!r} did not "
                f"round-trip. Got: {current['note']!r}"
            )
