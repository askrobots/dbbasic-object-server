"""CHARACTERIZATION tests: does an ARBITRARY JSON payload -- specifically the
shape plan/vocabulary/66-line-items-spec.md proposes (a document's `items`
array, compact-`json.dumps`'d into one TSV cell) -- survive storage
byte-exactly and re-parse deep-equal, through BOTH the record storage layer
(object_records.py, classic + append storage) AND the HTTP API
(object_server.py's ASGI app, admin collection-record routes)?

This file is pure characterization of EXISTING behavior. It does not modify
any production module. Where a case fails, the assertion is written to state
the correctness property a "safe to hold embedded JSON" substrate SHOULD
have, and is left to fail (or is downgraded to a soft print+record) -- that
outcome IS the finding. Do not "fix" a failing assertion here by weakening it
to match broken behavior.

Companion to tests/test_embedded_json_lines_characterization.py (which
established the record-layer setup conventions this file reuses, and already
covers hostile-content-across-physical-lines / offset-index / scale / torn
tail -- NOT retested here; in particular the "torn tail is quote-blind"
substrate bug #2 is a KNOWN, DEFERRED finding and is deliberately NOT probed
by this file). This file's unique angle: full Unicode character-CLASS
coverage (RTL scripts, emoji+ZWJ+skin-tone+flags, combining marks,
zero-width chars), JSON structural edge cases (unicode keys, nesting, null/
bool/large-int/float), the ensure_ascii=True vs False choice, the
MAX_TSV_FIELD_BYTES cap under multibyte content, idempotent re-serialization,
and the HTTP API surface, which the companion file never drives at all.

Per instructions: do NOT probe torn-tail. Recent fixes (csv.field_size_limit
raise + write cap; _oidx_get_row csv-aware) are assumed already covered by
the companion file's regression guards and are not retested here except as
incidental background for the size-cap section.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

import object_records
import object_server

# Conformance tier: heavy charset characterization -- deselected from the
# per-commit run (see pyproject 'conformance' marker). Run: pytest -m conformance
pytestmark = pytest.mark.conformance

ID_FIELD = {"name": "id"}
ITEMS_FIELD = {"name": "items", "type": "textarea"}

TEST_ADMIN_TOKEN = "unit-test-only-admin-token"


# =============================================================================
# setup helpers (mirror tests/test_embedded_json_lines_characterization.py)
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
    """Cold the in-process record cache + id->offset sidecar so a
    following read genuinely exercises storage, not a warm hit -- same
    trick the companion characterization file and object_records' own
    oidx tests use."""
    object_records._RECORDS_CACHE.clear()
    object_records._OIDX_CACHE.clear()


# =============================================================================
# HTTP in-process ASGI driver (mirrors tests/test_object_server.py's
# asgi_request/request/raw_request helpers exactly, trimmed to what this
# file needs)
# =============================================================================


def auth_headers():
    return [("authorization", f"Token {TEST_ADMIN_TOKEN}")]


def enable_admin_token(monkeypatch):
    monkeypatch.setenv("DBBASIC_ADMIN_TOKEN", TEST_ADMIN_TOKEN)


async def asgi_request(path, method="GET", query_string="", body=b"", headers=None):
    messages = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    scope_headers = [(b"accept", b"application/json")]
    for name, value in headers or []:
        if isinstance(name, str):
            name = name.encode("latin-1")
        if isinstance(value, str):
            value = value.encode("latin-1")
        scope_headers.append((name, value))

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query_string.encode("utf-8"),
        "headers": scope_headers,
        "client": ("127.0.0.1", 12345),
    }
    await object_server.app(scope, receive, send)

    start = next(m for m in messages if m["type"] == "http.response.start")
    body_parts = [m.get("body", b"") for m in messages if m["type"] == "http.response.body"]
    payload = b"".join(body_parts)
    return start["status"], dict(start["headers"]), payload


def request(path, method="GET", query_string="", body=b"", headers=None):
    status, headers_out, payload = asyncio.run(
        asgi_request(path, method=method, query_string=query_string, body=body, headers=headers)
    )
    return status, headers_out, json.loads(payload.decode("utf-8"))


# =============================================================================
# Realistic + adversarial JSON item-array fixtures
# =============================================================================


def _charclass_string() -> str:
    """One string mixing every character class the probe cares about."""
    return (
        'ASCII "quoted" back\\slash, comma, and a semicolon; '
        "café résumé naïve "  # accented Latin
        "日本語のテスト文字列 "  # CJK
        "العربية "  # Arabic (RTL)
        "עברית "  # Hebrew (RTL)
        "ȩ́ "  # combining acute + cedilla
        "​‌zero‍width "  # ZWSP, ZWNJ, ZWJ
        "\U0001f469‍\U0001f469‍\U0001f466‍\U0001f466 "  # family ZWJ sequence
        "\U0001f44d\U0001f3fd "  # thumbs up + medium skin tone modifier
        "\U0001f1fa\U0001f1f8 "  # flag: US (regional indicator pair)
        "\U0001f600"  # simple emoji
    )


def _realistic_items() -> list[dict]:
    """The actual embed use case: a document's `items` array, per
    plan/vocabulary/66-line-items-spec.md's item_fields shape."""
    return [
        {
            "product_id": "prod-001",
            "description": "Café crème, 12oz — 日本茶セット",
            "quantity": 2,
            "unit_price_cents": 1099,
            "tax_rate_bps": 875,
        },
        {
            "product_id": "prod-002",
            "description": _charclass_string(),
            "quantity": 1,
            "unit_price_cents": 2500,
            "tax_rate_bps": 0,
        },
        {
            "product_id": "prod-003",
            "description": "",
            "quantity": 0,
            "unit_price_cents": 0,
            "tax_rate_bps": None,
            "note": None,
            "gift_wrapped": True,
            "backordered": False,
        },
    ]


def _adversarial_items() -> list:
    """JSON structural edge cases: unicode keys, deep nesting, empty
    string, null, booleans, ints (incl. large), floats, a JSON-looking
    string value, and a value containing literal TSV delimiter chars."""
    return [
        {
            "étoile": "unicode key (accented Latin)",
            "日本": "unicode key (CJK)",
            "\U0001f600key": "unicode key (emoji)",
        },
        {
            "nested": {
                "level2": {
                    "level3": [1, 2, {"level4": [{"level5": "deep"}]}],
                },
                "array_of_arrays": [[1, 2], [3, [4, 5, [6, 7]]]],
            }
        },
        {
            "empty_string": "",
            "is_null": None,
            "is_true": True,
            "is_false": False,
            "small_int": 42,
            "large_int": 9223372036854775807,  # int64 max
            "huge_int": 123456789012345678901234567890,  # beyond int64
            "float_val": 19.99,
            "float_tricky": 0.1,
            "json_looking_string": '{"not":"parsed","nested":[1,2,3]}',
            "delimiter_string": "a\tb,c;d\nrepresented-as-escaped-in-json",
        },
    ]


# =============================================================================
# 1. Realistic items array -- classic + append, fold + by-id, plus update
# =============================================================================


def test_realistic_items_round_trip_classic_mode_fold_and_by_id(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(data_dir, "orders", [ID_FIELD, ITEMS_FIELD])
    original = _realistic_items()
    blob = json.dumps(original)

    object_records.create_collection_record(
        "orders", {"id": "ord-1", "items": blob}, base_dir=data_dir, roots=[]
    )

    _clear_caches()
    all_records = object_records.read_collection_records("orders", base_dir=data_dir, roots=[])
    assert json.loads(all_records[0]["items"]) == original

    _clear_caches()
    by_id = object_records.get_collection_record("orders", "ord-1", base_dir=data_dir, roots=[])
    assert json.loads(by_id["items"]) == original

    # Stored cell must be delimiter-safe: compact json.dumps escapes all
    # control chars in string VALUES, so no raw tab/newline byte should
    # appear in the stored blob itself.
    assert "\t" not in by_id["items"]
    assert "\n" not in by_id["items"]


def test_realistic_items_round_trip_append_mode_fold_and_by_id(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "orders", [ID_FIELD, ITEMS_FIELD])
    original = _realistic_items()
    blob = json.dumps(original)

    object_records.create_collection_record(
        "orders", {"id": "ord-1", "items": blob}, base_dir=data_dir, roots=[]
    )

    _clear_caches()
    all_records = object_records.read_collection_records("orders", base_dir=data_dir, roots=[])
    assert json.loads(all_records[0]["items"]) == original

    _clear_caches()
    by_id = object_records.get_collection_record("orders", "ord-1", base_dir=data_dir, roots=[])
    assert json.loads(by_id["items"]) == original
    assert "\t" not in by_id["items"]
    assert "\n" not in by_id["items"]


@pytest.mark.parametrize("storage", ["classic", "append"])
def test_realistic_items_update_one_item_then_reread(tmp_path, monkeypatch, storage):
    """The document-lines-spec's mutation shape: read the whole array,
    mutate ONE item, re-serialize the WHOLE array, write it back -- then
    confirm a cold-cache reread agrees deep-equal."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    collection = "orders"
    if storage == "classic":
        write_schema(data_dir, collection, [ID_FIELD, ITEMS_FIELD])
    else:
        write_append_schema(data_dir, collection, [ID_FIELD, ITEMS_FIELD])

    original = _realistic_items()
    object_records.create_collection_record(
        collection, {"id": "ord-1", "items": json.dumps(original)}, base_dir=data_dir, roots=[]
    )

    _clear_caches()
    current = object_records.get_collection_record(collection, "ord-1", base_dir=data_dir, roots=[])
    items = json.loads(current["items"])
    items[1]["quantity"] += 5
    items[1]["description"] = items[1]["description"] + " (updated) " + _charclass_string()
    expected = copy.deepcopy(items)

    object_records.update_collection_record(
        collection, "ord-1", {"items": json.dumps(items)}, base_dir=data_dir, roots=[]
    )

    _clear_caches()
    after = object_records.get_collection_record(collection, "ord-1", base_dir=data_dir, roots=[])
    assert json.loads(after["items"]) == expected


# =============================================================================
# 2. Adversarial JSON structural edge cases -- both modes
# =============================================================================


@pytest.mark.parametrize("storage", ["classic", "append"])
def test_adversarial_structural_edge_cases_round_trip(tmp_path, monkeypatch, storage):
    """unicode keys, deep nesting, null/bool/int/float, a JSON-looking
    string value, and a string literally containing TSV delimiter chars
    (tab/comma/semicolon/newline) -- all inside JSON so json.dumps has
    already escaped the control chars before the TSV layer ever sees
    them."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    collection = "orders"
    if storage == "classic":
        write_schema(data_dir, collection, [ID_FIELD, ITEMS_FIELD])
    else:
        write_append_schema(data_dir, collection, [ID_FIELD, ITEMS_FIELD])

    original = _adversarial_items()
    blob = json.dumps(original)

    object_records.create_collection_record(
        collection, {"id": "adv-1", "items": blob}, base_dir=data_dir, roots=[]
    )

    _clear_caches()
    all_records = object_records.read_collection_records(collection, base_dir=data_dir, roots=[])
    loaded_fold = json.loads(all_records[0]["items"])

    _clear_caches()
    by_id = object_records.get_collection_record(collection, "adv-1", base_dir=data_dir, roots=[])
    loaded_by_id = json.loads(by_id["items"])

    # unicode keys survive
    assert loaded_fold[0] == original[0]
    assert loaded_by_id[0] == original[0]
    assert set(loaded_by_id[0].keys()) == set(original[0].keys())

    # deep nesting survives
    assert loaded_fold[1] == original[1]
    assert loaded_by_id[1] == original[1]

    # null/bool/int survive exactly
    assert loaded_by_id[2]["is_null"] is None
    assert loaded_by_id[2]["is_true"] is True
    assert loaded_by_id[2]["is_false"] is False
    assert loaded_by_id[2]["small_int"] == 42
    assert loaded_by_id[2]["large_int"] == 9223372036854775807
    assert loaded_by_id[2]["huge_int"] == 123456789012345678901234567890

    # a JSON-looking string value must come back as a STRING, not be
    # double-parsed into a nested object
    assert isinstance(loaded_by_id[2]["json_looking_string"], str)
    assert loaded_by_id[2]["json_looking_string"] == original[2]["json_looking_string"]

    # a value containing literal delimiter characters (tab/comma/
    # semicolon/newline) survives -- these are JSON-string-escaped by
    # json.dumps, so the TSV layer never sees the raw bytes
    assert loaded_by_id[2]["delimiter_string"] == original[2]["delimiter_string"]

    assert loaded_fold == original
    assert loaded_by_id == original


def test_float_round_trip_report(tmp_path):
    """Report-only: do JSON floats survive json.dumps -> TSV cell ->
    json.loads exactly? (Money doctrine elsewhere in this codebase already
    mandates integer minor units / cents for currency -- this test exists
    to CONFIRM that recommendation empirically for the items-array use
    case, not to relitigate it.)"""
    data_dir = tmp_path / "data"
    write_schema(data_dir, "orders", [ID_FIELD, ITEMS_FIELD])
    original = [{"float_a": 19.99, "float_b": 0.1, "float_c": 1e300, "float_d": 1.1e-10}]
    blob = json.dumps(original)

    object_records.create_collection_record(
        "orders", {"id": "f-1", "items": blob}, base_dir=data_dir, roots=[]
    )
    _clear_caches()
    by_id = object_records.get_collection_record("orders", "f-1", base_dir=data_dir, roots=[])
    loaded = json.loads(by_id["items"])

    # Python's float repr round-trips via repr()/json for IEEE-754
    # doubles (json encodes via float.__repr__, which is the shortest
    # string that round-trips) -- so THIS layer (storage) does not lose
    # precision. The finding is about REPRESENTATION choice, not this
    # substrate: any float value is inherently binary-imprecise for
    # currency math (e.g. 19.99 is not exactly representable), so even
    # though it round-trips bit-for-bit through storage here, arithmetic
    # on such floats elsewhere will accumulate error. Report both facts.
    print(f"\n[float report] storage round-trip exact: {loaded == original}")
    print(f"[float report] original: {original}")
    print(f"[float report] loaded:   {loaded}")
    assert loaded == original, (
        "FINDING: float values did NOT survive storage exactly -- "
        f"original={original!r} loaded={loaded!r}"
    )
    print(
        "[float report] RECOMMENDATION: storage itself preserves float bits "
        "exactly (json+repr round-trip), but floats remain the wrong "
        "representation for money -- integer minor units (cents) per the "
        "codebase's existing money doctrine, not because storage corrupts "
        "them, but because binary floats can't exactly represent most "
        "decimal currency values in the first place (e.g. 19.99), so any "
        "arithmetic over them (subtotals/tax) will drift."
    )


# =============================================================================
# 3. ensure_ascii True vs False
# =============================================================================


@pytest.mark.parametrize("storage", ["classic", "append"])
def test_ensure_ascii_true_default_round_trips_and_is_pure_ascii_cell(tmp_path, monkeypatch, storage):
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    collection = "orders"
    if storage == "classic":
        write_schema(data_dir, collection, [ID_FIELD, ITEMS_FIELD])
    else:
        write_append_schema(data_dir, collection, [ID_FIELD, ITEMS_FIELD])

    original = _realistic_items()
    blob = json.dumps(original)  # ensure_ascii=True is the json.dumps default
    assert blob.isascii(), "sanity: default json.dumps must produce a pure-ASCII string"

    object_records.create_collection_record(
        collection, {"id": "ascii-1", "items": blob}, base_dir=data_dir, roots=[]
    )
    _clear_caches()
    by_id = object_records.get_collection_record(collection, "ascii-1", base_dir=data_dir, roots=[])
    assert by_id["items"].isascii(), "stored cell should remain pure ASCII (\\uXXXX escapes only)"
    assert json.loads(by_id["items"]) == original


@pytest.mark.parametrize("storage", ["classic", "append"])
def test_ensure_ascii_false_round_trips_with_raw_multibyte_in_cell(tmp_path, monkeypatch, storage):
    """ensure_ascii=False puts raw UTF-8 multibyte (and raw emoji, RTL
    script, etc.) directly in the TSV cell instead of \\uXXXX escapes.
    This still round-trips through this substrate (the TSV/CSV layer is
    UTF-8 text throughout), but the cell is no longer pure ASCII on disk."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    collection = "orders"
    if storage == "classic":
        write_schema(data_dir, collection, [ID_FIELD, ITEMS_FIELD])
    else:
        write_append_schema(data_dir, collection, [ID_FIELD, ITEMS_FIELD])

    original = _realistic_items()
    blob = json.dumps(original, ensure_ascii=False)
    assert not blob.isascii(), "sanity: ensure_ascii=False should emit raw multibyte here"

    object_records.create_collection_record(
        collection, {"id": "nonascii-1", "items": blob}, base_dir=data_dir, roots=[]
    )
    _clear_caches()
    by_id = object_records.get_collection_record(collection, "nonascii-1", base_dir=data_dir, roots=[])
    assert not by_id["items"].isascii()
    assert json.loads(by_id["items"]) == original
    # No raw tab/newline bytes even with ensure_ascii=False -- control-char
    # escaping in JSON strings is independent of the ensure_ascii setting
    # (only affects non-ASCII codepoints >= 0x80, not control chars).
    assert "\t" not in by_id["items"]
    assert "\n" not in by_id["items"]


# =============================================================================
# 4. Size cap interaction with multibyte content
# =============================================================================


def test_size_cap_multibyte_content_byte_accurate(tmp_path):
    """A JSON array whose serialized bytes approach MAX_TSV_FIELD_BYTES,
    built from multibyte (CJK) content so char-count and byte-count
    diverge sharply -- verify the cap is enforced on BYTES (as
    MAX_TSV_FIELD_BYTES's docstring/companion test claims), not characters,
    and that a comfortably-under-cap multibyte blob still round-trips.

    Uses ensure_ascii=False so each CJK codepoint costs its real 3 raw
    UTF-8 bytes in the cell. NOTE (finding, surfaced in the report): with
    the codebase's actual convention -- default json.dumps, ensure_ascii=
    True -- the SAME logical content costs 6 ASCII bytes per CJK
    codepoint (a "\\uXXXX" escape) instead of 3 raw UTF-8 bytes, i.e.
    roughly DOUBLE the serialized size for heavily non-ASCII item text.
    That inflation eats into the effective headroom under MAX_TSV_FIELD_
    BYTES / max_items caps for non-Latin-script content specifically, and
    was surprising enough during test construction (a naive byte budget
    silently produced a blob 2x the intended size) that it's worth
    reporting explicitly rather than treating as obvious."""
    data_dir = tmp_path / "data"
    write_schema(data_dir, "orders", [ID_FIELD, ITEMS_FIELD])

    # Each item's description is a CJK string (3 bytes/char in UTF-8) --
    # build enough items to land close to, but safely under, the cap.
    cjk_char = "日"  # 3 bytes UTF-8 (raw); 6 bytes if json.dumps escapes it
    target_bytes = object_records.MAX_TSV_FIELD_BYTES - 2048  # headroom for JSON syntax overhead
    # each item is roughly: {"d":"...."} -- budget item body length below
    item_overhead = len('{"d":""}')
    n_items = 50
    chars_per_item = (target_bytes // n_items - item_overhead) // 3
    items = [{"d": cjk_char * chars_per_item} for _ in range(n_items)]
    blob = json.dumps(items, ensure_ascii=False)
    blob_bytes = len(blob.encode("utf-8"))
    assert blob_bytes < object_records.MAX_TSV_FIELD_BYTES, (
        f"test construction sanity: blob_bytes={blob_bytes} must stay under cap "
        f"{object_records.MAX_TSV_FIELD_BYTES} to prove the under-cap case"
    )
    # Same logical content, but serialized the codebase's actual way
    # (ensure_ascii=True, the json.dumps default used everywhere else in
    # this repo) -- report the inflation factor.
    blob_ascii_escaped = json.dumps(items)
    blob_ascii_escaped_bytes = len(blob_ascii_escaped.encode("utf-8"))
    print(f"\n[size-cap] multibyte blob (ensure_ascii=False, raw UTF-8): "
          f"{blob_bytes} bytes (cap={object_records.MAX_TSV_FIELD_BYTES}), {len(blob)} chars")
    print(f"[size-cap] SAME content (ensure_ascii=True, \\uXXXX escapes): "
          f"{blob_ascii_escaped_bytes} bytes -- "
          f"{blob_ascii_escaped_bytes / blob_bytes:.2f}x larger")

    object_records.create_collection_record(
        "orders", {"id": "big-multibyte", "items": blob}, base_dir=data_dir, roots=[]
    )
    _clear_caches()
    by_id = object_records.get_collection_record("orders", "big-multibyte", base_dir=data_dir, roots=[])
    assert json.loads(by_id["items"]) == items

    # Now push a multibyte blob just OVER the byte cap (but with a
    # char-count that would be well under it) -- the cap must reject on
    # BYTES, not code points, or this write would wrongly succeed.
    over_target_bytes = object_records.MAX_TSV_FIELD_BYTES + 300
    n_items_over = 60
    chars_per_item_over = (over_target_bytes // n_items_over - item_overhead) // 3 + 50
    over_items = [{"d": cjk_char * chars_per_item_over} for _ in range(n_items_over)]
    over_blob = json.dumps(over_items, ensure_ascii=False)
    over_bytes = len(over_blob.encode("utf-8"))
    over_chars = len(over_blob)
    assert over_bytes > object_records.MAX_TSV_FIELD_BYTES, (
        f"test construction sanity: need over_bytes > cap, got {over_bytes}"
    )
    print(f"[size-cap] over-cap multibyte blob: {over_bytes} bytes, {over_chars} chars "
          f"(chars < bytes: {over_chars < object_records.MAX_TSV_FIELD_BYTES})")

    with pytest.raises(object_records.InvalidRecordPayloadError):
        object_records.create_collection_record(
            "orders", {"id": "too-big-multibyte", "items": over_blob}, base_dir=data_dir, roots=[]
        )


# =============================================================================
# 5. Idempotent re-serialization (compact-form stability)
# =============================================================================


@pytest.mark.parametrize("storage", ["classic", "append"])
def test_idempotent_reserialization_stability(tmp_path, monkeypatch, storage):
    """json.loads(read) then json.dumps(...) again (compact form, same
    separators/key order as the original write) must reproduce the exact
    stored blob byte-for-byte -- i.e. round-tripping through this layer
    introduces no drift a naive re-save-without-edits would compound."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    collection = "orders"
    if storage == "classic":
        write_schema(data_dir, collection, [ID_FIELD, ITEMS_FIELD])
    else:
        write_append_schema(data_dir, collection, [ID_FIELD, ITEMS_FIELD])

    original = _realistic_items() + _adversarial_items()
    blob = json.dumps(original)

    object_records.create_collection_record(
        collection, {"id": "stable-1", "items": blob}, base_dir=data_dir, roots=[]
    )
    _clear_caches()
    by_id = object_records.get_collection_record(collection, "stable-1", base_dir=data_dir, roots=[])
    stored = by_id["items"]

    reparsed = json.loads(stored)
    reserialized = json.dumps(reparsed)
    assert reserialized == stored, (
        "idempotency FINDING: re-serializing the parsed value did not "
        "reproduce the stored blob byte-for-byte"
    )
    assert reserialized == blob


# =============================================================================
# 6. HTTP API path -- admin collection-record routes
# =============================================================================


def test_http_create_and_read_realistic_items_round_trip_classic(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_schema(data_dir, "orders", [ID_FIELD, ITEMS_FIELD])
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    original = _realistic_items()
    blob = json.dumps(original)

    create_status, _, create_payload = request(
        "/admin/collections/orders/records",
        method="POST",
        body=json.dumps({"id": "http-1", "items": blob}).encode("utf-8"),
        headers=auth_headers(),
    )
    assert create_status == 201, create_payload
    assert json.loads(create_payload["record"]["items"]) == original

    _clear_caches()
    list_status, _, list_payload = request(
        "/admin/collections/orders/records", headers=auth_headers()
    )
    assert list_status == 200
    fold_row = next(r for r in list_payload["records"] if r["id"] == "http-1")
    assert json.loads(fold_row["items"]) == original

    _clear_caches()
    get_status, _, get_payload = request(
        "/admin/collections/orders/records/http-1", headers=auth_headers()
    )
    assert get_status == 200
    assert json.loads(get_payload["record"]["items"]) == original


def test_http_create_and_read_realistic_items_round_trip_append(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "orders", [ID_FIELD, ITEMS_FIELD])
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    original = _adversarial_items()
    blob = json.dumps(original)

    create_status, _, create_payload = request(
        "/admin/collections/orders/records",
        method="POST",
        body=json.dumps({"id": "http-adv-1", "items": blob}).encode("utf-8"),
        headers=auth_headers(),
    )
    assert create_status == 201, create_payload
    assert json.loads(create_payload["record"]["items"]) == original

    _clear_caches()
    get_status, _, get_payload = request(
        "/admin/collections/orders/records/http-adv-1", headers=auth_headers()
    )
    assert get_status == 200
    assert json.loads(get_payload["record"]["items"]) == original


def test_http_update_one_item_then_reread(tmp_path, monkeypatch):
    """The HTTP path's version of the update-an-item-then-reread probe:
    POST create, GET, mutate client-side, PUT the whole items array back,
    GET again (cold cache) and confirm deep equality."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "orders", [ID_FIELD, ITEMS_FIELD])
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    original = _realistic_items()
    request(
        "/admin/collections/orders/records",
        method="POST",
        body=json.dumps({"id": "http-upd-1", "items": json.dumps(original)}).encode("utf-8"),
        headers=auth_headers(),
    )

    _clear_caches()
    get_status, _, get_payload = request(
        "/admin/collections/orders/records/http-upd-1", headers=auth_headers()
    )
    assert get_status == 200
    items = json.loads(get_payload["record"]["items"])
    items[0]["description"] = _charclass_string() + " EDITED"
    items[0]["quantity"] = 999
    expected = copy.deepcopy(items)

    update_status, _, update_payload = request(
        "/admin/collections/orders/records/http-upd-1",
        method="PUT",
        body=json.dumps({"items": json.dumps(items)}).encode("utf-8"),
        headers=auth_headers(),
    )
    assert update_status == 200, update_payload
    assert json.loads(update_payload["record"]["items"]) == expected

    _clear_caches()
    reget_status, _, reget_payload = request(
        "/admin/collections/orders/records/http-upd-1", headers=auth_headers()
    )
    assert reget_status == 200
    assert json.loads(reget_payload["record"]["items"]) == expected


def test_http_request_body_cap_is_far_below_the_field_cap_FINDING(tmp_path, monkeypatch):
    """FINDING: object_server's own HTTP request-body-size gate
    (DBBASIC_MAX_REQUEST_BYTES, default 1 MiB -- see
    object_server.DEFAULT_MAX_REQUEST_BYTES) is enforced BEFORE the JSON
    body is even parsed, and its default (1,048,576 bytes) is 16x SMALLER
    than object_records.MAX_TSV_FIELD_BYTES (16 MiB). A single `items`
    field anywhere near the record-layer cap can never actually reach
    create_collection_record's InvalidRecordPayloadError check via HTTP
    under default configuration -- it is rejected first, at the transport
    layer, with 413 "Request body too large" (a different status/shape
    than the record-layer's 400 InvalidRecordPayloadError). This is not
    unsafe (both reject the oversize write), but it means the HTTP path's
    effective per-field ceiling for `items` is ~1 MiB by default, not
    16 MiB, and any client-facing error handling for "items too large"
    over HTTP must expect 413, not 400, unless the operator raises
    DBBASIC_MAX_REQUEST_BYTES."""
    data_dir = tmp_path / "data"
    write_schema(data_dir, "orders", [ID_FIELD, ITEMS_FIELD])
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    # A field comfortably PAST MAX_TSV_FIELD_BYTES, well past the
    # object_server.DEFAULT_MAX_REQUEST_BYTES (1 MiB) transport gate too.
    over = json.dumps([{"d": "x" * (object_records.MAX_TSV_FIELD_BYTES + 1000)}])
    status, _, payload = request(
        "/admin/collections/orders/records",
        method="POST",
        body=json.dumps({"id": "http-too-big", "items": over}).encode("utf-8"),
        headers=auth_headers(),
    )
    print(f"\n[http-cap FINDING] default DBBASIC_MAX_REQUEST_BYTES gate hit first: "
          f"status={status} payload={payload}")
    assert status == 413, payload
    assert payload["status"] == "error"


def test_http_oversize_items_payload_rejected_when_request_cap_raised(tmp_path, monkeypatch):
    """With the transport-level request-body cap explicitly raised above
    MAX_TSV_FIELD_BYTES (DBBASIC_MAX_REQUEST_BYTES), the request now
    reaches record-layer validation and the FIELD cap itself is exercised
    over HTTP: InvalidRecordPayloadError -> structured 400 (see
    object_server.py's exception-to-status mapping around
    InvalidRecordPayloadError)."""
    data_dir = tmp_path / "data"
    write_schema(data_dir, "orders", [ID_FIELD, ITEMS_FIELD])
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_MAX_REQUEST_BYTES", str(object_records.MAX_TSV_FIELD_BYTES + 1_000_000))
    enable_admin_token(monkeypatch)

    over = json.dumps([{"d": "x" * (object_records.MAX_TSV_FIELD_BYTES + 1000)}])
    status, _, payload = request(
        "/admin/collections/orders/records",
        method="POST",
        body=json.dumps({"id": "http-too-big", "items": over}).encode("utf-8"),
        headers=auth_headers(),
    )
    assert status == 400, payload
    assert payload["status"] == "error"
