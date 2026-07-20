"""TSV-backed collection records.

Collection records are the simple data surface Scroll can point generated
tables and forms at. Records live in
``data/collections/{collection}/records.tsv``.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import threading
from collections import OrderedDict
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import object_collections
import object_correlation
import object_ids
import object_permissions
import object_record_changes
import object_schemas
from object_versions import DEFAULT_DATA_DIR

COLLECTIONS_DIR = "collections"
RECORDS_FILE = "records.tsv"
DEFAULT_RECORD_LIMIT = 100
MAX_RECORD_LIMIT = 1000
EXTRA_FIELD = "extra"

# Maximum bytes for a single TSV field value. Python's csv module defaults
# csv.field_size_limit() to 128 KiB; a single record field can legitimately
# exceed that (a long article body, a big template JSON, the embedded-items
# array of plan/vocabulary/66). Left unraised, a cell over 128 KiB is a
# silent data-loss bug: a classic-mode read raises an uncaught csv.Error,
# and an APPEND-mode read returns an EMPTY collection (the oversize row trips
# the torn-tail-tolerant `except csv.Error: break` in _parse_append_body,
# discarding every row from that point on). We raise the PARSE ceiling here
# and enforce the SAME ceiling on WRITE (_check_field_sizes, called from
# create/update) so no row is ever persisted that cannot be read back. 16 MiB
# matches the platform's max request body -- a field cannot exceed the
# request that wrote it. Per-surface limits (e.g. the 256 KiB line-items cap)
# are stricter and enforced above this hard floor.
MAX_TSV_FIELD_BYTES = 16 * 1024 * 1024
csv.field_size_limit(MAX_TSV_FIELD_BYTES)

_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

# Append-only storage (docs/append-only-storage-design.md). `OP_FIELD` is
# an INTERNAL, file-layer-only column: when present, it is always the
# first column of the physical header, and it never appears in a schema,
# an API response, or a record returned by any public function in this
# module -- it is stripped during parse-fold and only ever written by the
# append/rewrite helpers below. `OP_UPSERT` ("") marks a row that sets/
# replaces a record; `OP_DELETE` ("del") marks a tombstone.
OP_FIELD = "_op"
OP_UPSERT = ""
OP_DELETE = "del"

# Threshold (physical row count) above which a collection becomes eligible
# for auto-compaction: see _maybe_flag_auto_compact.
APPEND_COMPACT_MIN_ROWS_ENV = "DBBASIC_APPEND_COMPACT_MIN_ROWS"
_DEFAULT_APPEND_COMPACT_MIN_ROWS = 10_000

# id -> byte-offset sidecar (docs/append-only-storage-design.md item 4).
# Lives next to records.tsv as a dotfile so it is invisible to every glob
# this codebase uses to enumerate a collection's real content
# (iter_record_collections' `*/records.tsv` glob, object_collections' same
# pattern) and to backup (object_backup._should_skip drops any path with a
# dotfile part) -- both were checked, not just assumed. Format (text,
# greppable, torn-tail-tolerant like the data file itself):
#   line 1 (header): "oidx1\t<data_ino>" -- the records.tsv inode this
#     sidecar describes. No byte count lives in the header (a header
#     rewrite-in-place would itself need the same torn-tail care as a data
#     row, for no real benefit): "how much of the file is indexed" is
#     instead DERIVED from the body, as the row_end_offset of the last
#     complete data line -- see _load_oidx_body.
#   subsequent lines: "<row_start_offset>\t<row_end_offset>\t<op>\t<id>",
#     one per indexed PHYSICAL row of records.tsv (op "" or "del", exactly
#     like OP_UPSERT/OP_DELETE). No CSV quoting is used or needed here:
#     unlike a record's own field values, `id` is restricted to
#     _RECORD_ID_RE (no tabs/newlines possible) and the offsets/op are
#     plain digits/literals, so a bare tab-split is always unambiguous.
# The sidecar is 100% disposable and NEVER required for correctness: any
# reader unable to make sense of it (missing, corrupt, inode mismatch,
# torn tail beyond what a catch-up scan can resolve) rebuilds it with one
# sequential scan of records.tsv rather than raising -- see _load_oidx.
OIDX_FILE = ".records.oidx"
OIDX_HEADER_TAG = "oidx1"

OIDX_CACHE_MAX_ENTRIES_ENV = "DBBASIC_OIDX_CACHE_MAX_ENTRIES"
_DEFAULT_OIDX_CACHE_MAX_ENTRIES = 2

# Env knobs for the records cache below. Read at call time (not cached in a
# module global) via _records_cache_max_entries()/_records_cache_max_rows()
# so tests can monkeypatch os.environ and see the change take effect on the
# next cache operation without reimporting the module.
RECORDS_CACHE_MAX_ENTRIES_ENV = "DBBASIC_RECORDS_CACHE_MAX_ENTRIES"
RECORDS_CACHE_MAX_ROWS_ENV = "DBBASIC_RECORDS_CACHE_MAX_ROWS"
_DEFAULT_RECORDS_CACHE_MAX_ENTRIES = 64
_DEFAULT_RECORDS_CACHE_MAX_ROWS = 500_000

# Module-level cache for parsed records.tsv files, keyed by the resolved
# file path. An OrderedDict bounded to DBBASIC_RECORDS_CACHE_MAX_ENTRIES
# entries (default 64): every cache hit calls move_to_end() so the dict
# stays ordered oldest-to-newest by use, and storing a new entry evicts the
# oldest (popitem(last=False)) once the dict exceeds capacity -- see
# _store_records_cache. Without a bound, a long-running server that touches
# many collections over its lifetime (e.g. a no-downtime game server) would
# pin every collection it has ever parsed in RAM forever; the LRU keeps
# only the most-recently-used entries resident.
#
# A second knob, DBBASIC_RECORDS_CACHE_MAX_ROWS (default 500_000), bounds
# the cache by size rather than recency: a collection whose parse produces
# more rows than this is still returned to its caller normally, but is
# never stored in (or left in) the cache. Without this, reading one huge
# collection once would occupy a full LRU slot and could evict many small,
# hot collections just to make room for something that will likely never
# be reused whole. _cache_entry and _refresh_records_cache both apply this
# threshold before writing to _RECORDS_CACHE; _cache_entry additionally
# drops any stale entry already cached for a path that has since grown
# past the threshold, so an oversized collection never lingers there under
# an old signature.
#
# Value is (stat_signature, fields, records, id_index, covered_bytes):
#   - stat_signature = (st_mtime_ns, st_size, st_ino). A CLASSIC-mode write
#     goes through an atomic tempfile-then-replace, so a new inode backs the
#     path after each write; including st_ino means a same-tick mtime
#     collision (coarse clocks/filesystems) still isn't mistaken for
#     "unchanged" as long as the replace produced a different inode, which
#     is the common case for both our own writes and any other well-behaved
#     writer using the same replace pattern.
#       APPEND-mode writes (docs/append-only-storage-design.md) are the
#     deliberate exception: a fast append opens the SAME inode in "a" mode
#     and writes one more line, so st_ino is unchanged and only st_mtime_ns
#     / st_size move. That's still sufficient to invalidate a stale cache
#     entry (the signature tuple as a whole changes), it just means "same
#     inode" is no longer proof of "unchanged content" the way it was
#     before this feature -- st_size growing is what a reader actually
#     relies on to notice a completed append. A torn tail (a final physical
#     line with no trailing newline, from a write interrupted mid-append)
#     is never cached as data: the append-mode parser drops it, and the
#     next append's self-heal (_repair_torn_tail) removes it from disk
#     before adding a new line, so it can never resurface as a stale-but-
#     plausible cached row either.
#   - fields/records mirror exactly what a fresh parse of that exact file
#     content would produce (see _refresh_records_cache: written rows are
#     projected to the full field set before caching, matching what
#     csv.DictWriter/csv.reader would round-trip). For an append-mode file
#     this means the FOLDED (last-wins-by-id, tombstones removed, `_op`
#     stripped) view -- see _parse_append_body/_fold_append_rows -- never
#     the raw physical rows; everything below this cache (get, list,
#     window-copy, extra-field surfacing) operates on folded records with
#     zero changes, exactly as it does for a classic-mode file.
#   - id_index maps record id -> index into `records`, first-occurrence-wins
#     (matching the original linear scan's behavior on hand-edited files
#     with duplicate ids). For append mode this is built from the already-
#     deduplicated folded records, so in practice every live id maps to
#     exactly one position.
#   - covered_bytes is the byte offset (from 0) that this entry's fold
#     already accounts for -- always equal to the file's size at the
#     stat this entry was built from (signature[1]), for every entry
#     produced by a full parse or a full rewrite (_cache_entry's
#     full-parse branch, _refresh_records_cache, _refresh_records_cache_
#     after_append). It exists so _cache_entry's INCREMENTAL branch
#     (docs/append-only-storage-design.md Sidecars, bullet 2: "cache
#     entries remember the byte offset they consumed; when a stat
#     signature changes by growth alone, parse only the tail delta") can
#     tell how much of a GROWN append-mode file is already folded into
#     `records`, so only the new bytes need parsing -- see
#     _try_incremental_cache_entry. A CLASSIC-mode entry carries this
#     field too (harmless: it's just the file's size at cache time,
#     unused by anything) since a classic rewrite always produces a new
#     inode, which alone rules out the incremental branch regardless of
#     covered_bytes. Only an incrementally-built entry can end up with
#     covered_bytes < signature[1] -- when the tail delta itself ends in
#     a torn (uncommitted) row, that fragment's bytes are correctly left
#     out of both `records` and covered_bytes, exactly as a full parse
#     would also drop them (_drop_torn_tail); a later append still
#     compares against this same covered_bytes to find its own delta.
#
# ALIASING SAFETY: the `records` list and its dicts stored here are shared,
# mutable module state. Nothing in this module may mutate them in place.
# Every function that hands a record or records list back across the
# public API must copy first (see _read_collection_records,
# get_collection_record, list_collection_records). Internal callers that
# need to build on cached data for a write (create/update/delete) read
# `_cache_entry`'s tuple directly -- fine as long as they only ever READ
# from `records_ref`/`id_index_ref` (e.g. an O(1) duplicate/lookup check)
# or capture them in a closure that builds a fresh, independent copy on
# demand (see each function's `build_folded_records`, and _persist_write's
# `folded_records_fn`, which calls it only on a full-rewrite path -- never
# on the fast-append path, which is what keeps a steady-state append O(1)
# instead of paying an eager O(n) copy on every single write).
#
# CONCURRENCY (readers hold no lock; writers hold _records_file_lock only
# around their own read-modify-write): a reader's path is stat -> dict.get
# -> compare signature -> return (falling through to parse+store on a
# miss). A writer's path builds a complete new (signature, fields, records,
# id_index) tuple off to the side and installs it with a single dict
# assignment (see _store_records_cache). That assignment is one atomic
# store under the GIL, so a concurrent reader's dict.get() always observes
# either the entry from before the write or the entry from after it in
# full -- never a torn mix of old fields with new records, or similar.
#
# The only reachable skew is an old signature serving newer content: a
# reader stats the file (capturing the OLD signature), a writer's replace()
# lands, and only then does the reader open+parse the file via its own
# fresh fd. Because replace() is atomic and that fd is opened after the
# stat, the fd sees either the pre- or post-write content in full, never a
# mix -- so if it lands on the post-write content, what the reader parses
# is complete and correct, it's just paired with a signature it captured a
# moment too early. That mismatch means this parse won't be stored under a
# key matching what's already cached, so it simply fails every future
# signature comparison and self-heals with one extra reparse next time
# (typically immediately, since the writer's own _refresh_records_cache
# call installs the current signature right after the write completes).
# The reverse -- a new signature paired with old content -- is structurally
# impossible: content is always read at or after the stat that produced
# its signature, never before.
#
# Cross-process coherence relies on every writer in this codebase using the
# same temp-file + atomic-rename pattern, so a fresh inode backs every
# write (st_ino is part of the signature): this module's own
# _write_collection_records (below), object_packages.py's package-install
# writer (~line 1022, os.replace), and object_backup.py's restore writer
# (~line 607, Path.replace) were all checked and write via a temp path
# followed by an atomic replace, never an in-place open("w").
_RECORDS_CACHE: "OrderedDict[str, tuple[tuple[int, int, int], list[str], list[dict[str, str]], dict[str, int], int]]" = OrderedDict()

# Observability-only counter: incremented once per successful tail-delta
# incremental fold in _cache_entry (see _try_incremental_cache_entry).
# Never read by any runtime code path -- it exists purely so tests can
# assert the incremental path actually ran (rather than a full re-parse)
# without needing to monkeypatch internals just to observe that.
_INCREMENTAL_CACHE_FOLDS = 0

# In-memory mirror of the on-disk id->offset sidecar (see OIDX_FILE above),
# keyed by resolved records.tsv path. Value is (data_ino, covered_bytes,
# id->row_start_offset): `covered_bytes` is how much of the file (from
# byte 0) this dict accounts for -- see _load_oidx, the sole reader AND
# writer of this dict. A DELIBERATELY SEPARATE, much smaller LRU than
# _RECORDS_CACHE (default 2 entries, not 64): the two caches serve
# opposite size regimes -- _RECORDS_CACHE holds fully-parsed records and
# is capped by ROW COUNT (never stores anything over
# DBBASIC_RECORDS_CACHE_MAX_ROWS), while this sidecar is only ever
# consulted for collections ABOVE that same threshold (see
# _fast_record_lookup: it's tried only on a _RECORDS_CACHE miss), so an
# entry here is exactly the case _RECORDS_CACHE refuses to hold. A plain
# {id: int} dict is far lighter per row than a full record dict, but at
# millions of rows it is still tens of MB (roughly 80-120MB at 1M ids per
# the design doc) -- sharing _RECORDS_CACHE's 64-entry budget would let a
# few huge collections' sidecars alone occupy gigabytes. Bounded instead
# by DBBASIC_OIDX_CACHE_MAX_ENTRIES (default 2): a server touching more
# than a couple of huge append collections concurrently pays a reload
# (still just a sidecar-body read, not a records.tsv fold) rather than
# holding every one of them resident forever.
_OIDX_CACHE: "OrderedDict[str, tuple[int, int, dict[str, int]]]" = OrderedDict()

# Collections (by resolved records.tsv path) flagged during a parse/fold as
# having enough superseded+deleted rows to be worth compacting -- see
# _maybe_flag_auto_compact. Consulted (and cleared) by _persist_write on
# that collection's NEXT write, which performs a compacting rewrite instead
# of a plain append. Never acted on from a read path: setting membership
# here is the only thing a read ever does, and it is not a disk write.
_PENDING_COMPACTION: set[str] = set()
_INTEGER_TYPES = {"int", "integer"}
_FLOAT_TYPES = {"float", "number", "currency"}
_BOOLEAN_TYPES = {"bool", "boolean"}
_TRUE_VALUES = {"true", "1", "yes", "on"}
_FALSE_VALUES = {"false", "0", "no", "off"}


class InvalidRecordIdError(ValueError):
    """Raised when a record id is not safe for routes or lookup."""


class RecordNotFoundError(LookupError):
    """Raised when a record cannot be found in a collection."""


class DuplicateRecordIdError(ValueError):
    """Raised when a create would reuse an existing record id."""


class InvalidRecordPayloadError(ValueError):
    """Raised when a record write payload is not usable."""


class TransitionNotAllowedError(InvalidRecordPayloadError):
    """Raised when a transition's value is valid but its guard denies the subject.

    Subclasses InvalidRecordPayloadError so callers that don't distinguish
    the two keep working (still a 400-shaped payload error); callers that
    plumb a subject into update_collection_record (see
    _validate_field_transitions) can catch this specifically to report a
    permission-style 403 instead.
    """


def validate_record_id(record_id: str) -> bool:
    """Return True when a record id is route-safe."""
    if not isinstance(record_id, str):
        return False
    return bool(_RECORD_ID_RE.fullmatch(record_id))


def normalize_record_payload(payload: dict[str, Any], *, require_id: bool = False) -> dict[str, str]:
    """Return a TSV-safe record payload with scalar values converted to strings."""
    return _normalize_record_payload(payload, require_id=require_id)


def list_collection_records(
    collection: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
    limit: int = DEFAULT_RECORD_LIMIT,
    offset: int = 0,
    where: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a paginated record list for one collection.

    Copies only the window (records[offset:offset + limit]) rather than
    the whole collection first: callers of a paginated list only ever
    consume a small page, and copying every record before slicing it away
    (the straightforward implementation) dominates cost on large
    collections for a window that is thrown away 99% copied. `total` is
    computed from the cache's own record count, not from the copied
    window.

    `where` (plan/vocabulary/58-query-filter-spec.md) is an optional
    normalized field filter, evaluated with `filter_records` -- see there
    for the shape. It is purely a field filter with no permission
    awareness of its own: callers that also need row-level permission
    filtering (object_server's HTTP/MCP surfaces) apply that FIRST and
    pass only the already-permitted records through the same evaluator
    (`filter_records`), so a field filter can only narrow what a caller
    already decided is readable, never widen it -- this function's own
    `where` follows the identical narrow-only contract, just without a
    row filter of its own to narrow. When `where` is given, the windowing
    optimization above no longer applies (a filtered window can't be
    known without first testing every row), so the whole collection is
    read and filtered before slicing -- same O(rows) cost as any other
    full-collection read (see 58's Storage section).
    """
    _ensure_collection_known(collection, base_dir=base_dir, roots=roots)
    _validate_page(limit=limit, offset=offset)

    records_ref = _cached_records_ref(collection, base_dir=base_dir)
    extra_names = _extra_field_names(collection, base_dir=base_dir, roots=roots)

    if where:
        matching = [
            _surface_extra(dict(record), extra_names=extra_names)
            for record in records_ref
            if object_permissions.record_matches_filter(
                record, where, object_permissions.PermissionSubject.anonymous()
            )
        ]
        total = len(matching)
        window = matching[offset:offset + limit]
        return _build_records_payload(collection, window, total=total, limit=limit, offset=offset)

    total = len(records_ref)
    # Copy + surface only the window's records: entries in `records_ref`
    # are the module cache's own dicts (see _RECORDS_CACHE ALIASING
    # SAFETY above) and _surface_extra mutates its argument in place, so
    # each must be copied before use -- but only the rows inside the
    # window, never the rest of the collection.
    window = [
        _surface_extra(dict(record), extra_names=extra_names)
        for record in records_ref[offset:offset + limit]
    ]
    return _build_records_payload(collection, window, total=total, limit=limit, offset=offset)


def filter_records(
    records: Iterable[Mapping[str, Any]],
    where: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return the subset of `records` matching a normalized field filter.

    Pure and in-memory: no read, no schema/permission awareness of its
    own. Reuses `object_permissions.record_matches_filter` -- the same
    flat-condition evaluator that backs guarded transitions and
    permission row filters -- so there is exactly one filter evaluator in
    the codebase (plan/vocabulary/58-query-filter-spec.md).

    Callers that also apply a permission row filter MUST do so first and
    pass only its output here: this function can only remove rows from
    what it is given, never add any back, so "row filter's output is
    this function's input" is what structurally guarantees a field
    filter can narrow the readable set but never widen it (58's
    Permissions Posture).
    """
    if not where:
        return [dict(record) for record in records]
    anonymous = object_permissions.PermissionSubject.anonymous()
    return [
        dict(record)
        for record in records
        if object_permissions.record_matches_filter(record, where, anonymous)
    ]


_ORDERED_FILTER_OPERATORS = frozenset({"gte", "lte", "gt", "lt"})
_ORDERED_FILTERABLE_TYPES = _INTEGER_TYPES | _FLOAT_TYPES | {"date", "datetime", "timestamp"}


def normalize_filter_value(field: dict[str, Any] | None, op: str, value: str) -> str:
    """Validate one query-filter condition's value against a schema field
    and return its canonical stored-string form.

    `field` is the schema field definition (or None for a schemaless/
    derived collection, which has no type to validate against and is
    treated as permissive -- consistent with every other schema-optional
    check in this module). Raises InvalidRecordPayloadError -- the same
    exception schema validation already raises -- naming the field on a
    bad type, or on an ordered operator (gte/lte/gt/lt) against a field
    type that doesn't support ordered comparison (58 restricts those to
    date/datetime/integer/number fields). Reuses `_validate_field_type`,
    the same per-type parser create/update already validates against, so
    there is exactly one value-type validator, not a second one for
    filters.

    A boolean field's value is normalized to "true"/"false" -- the same
    canonical form `_canonicalize_schema_values` stores on write -- so an
    `eq`/`ne` filter written as `?is_public=1` still string-matches a
    stored "true" rather than silently never matching.
    """
    if field is None:
        return value

    name = field.get("name", "")
    field_type = str(field.get("type") or "text").lower()
    if op in _ORDERED_FILTER_OPERATORS and field_type not in _ORDERED_FILTERABLE_TYPES:
        raise InvalidRecordPayloadError(
            f"Record field '{name}' does not support operator '{op}' "
            f"(only date, datetime, integer, and number fields support "
            f"ordered comparison)"
        )
    _validate_field_type(field, value)
    if field_type in _BOOLEAN_TYPES:
        return "true" if _parse_boolean(value, field_name=name) else "false"
    return value


def read_collection_records(
    collection: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
) -> list[dict[str, str]]:
    """Return all records for one known collection."""
    _ensure_collection_known(collection, base_dir=base_dir, roots=roots)
    records = _read_collection_records(collection, base_dir=base_dir)
    extra_names = _extra_field_names(collection, base_dir=base_dir, roots=roots)
    return [_surface_extra(record, extra_names=extra_names) for record in records]


def collection_records_payload(
    collection: str,
    records: list[dict[str, str]],
    *,
    limit: int = DEFAULT_RECORD_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Return the standard paginated record-list payload shape.

    Takes a full records list and slices the window out of it here --
    unlike list_collection_records, which slices before copying. This
    public entry point's contract is "you already have the full list";
    callers (e.g. object_server's search/permission-filtered list paths)
    rely on that.
    """
    _validate_page(limit=limit, offset=offset)
    total = len(records)
    window = records[offset:offset + limit]
    return _build_records_payload(collection, window, total=total, limit=limit, offset=offset)


def _build_records_payload(
    collection: str,
    window: list[dict[str, str]],
    *,
    total: int,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    return {
        "collection": collection,
        "records": window,
        "count": len(window),
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(window) < total,
    }


def get_collection_record(
    collection: str,
    record_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Return one record by its ``id`` column."""
    _ensure_collection_known(collection, base_dir=base_dir, roots=roots)
    if not validate_record_id(record_id):
        raise InvalidRecordIdError(f"Invalid record id: {record_id}")

    extra_names = _extra_field_names(collection, base_dir=base_dir, roots=roots)
    path = collection_records_file(collection, base_dir=base_dir)
    # _fast_record_lookup tries the warm cache, then (append mode, over
    # the cache's row threshold) the id->offset sidecar, before falling
    # back to a full fold -- see its docstring. `existing_row`, when
    # returned, is always already a fresh, independent copy.
    _, exists, existing_row, _, _ = _fast_record_lookup(
        collection, path, record_id, need_row=True
    )
    if not exists:
        raise RecordNotFoundError(f"Record not found: {collection}/{record_id}")

    return _surface_extra(existing_row, extra_names=extra_names)


def create_collection_record(
    collection: str,
    record: dict[str, Any],
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
    actor: str | None = None,
    preserve_read_only: bool = False,
) -> dict[str, str]:
    """Append one record to a collection TSV and return the stored row.

    Every successful write is durably attributed here (universal
    attribution -- every mutation, on every path, emits a record change).
    ``actor`` identifies who/what caused the write; callers that omit it
    are logged as ``"unattributed"`` rather than silently skipped, so gaps
    stay visible instead of disappearing. See object_record_changes for
    the append itself and its recursion note.

    ``preserve_read_only``: an ordinary write (client-submitted payload)
    may never set a ``read_only`` field (e.g. a schema's ``created_at``) --
    the server owns it, per ``_apply_auto_created_at``. A bulk loader
    replaying another system's history (object_import.py) legitimately
    needs to carry that system's own ``created_at`` through instead of
    stamping "now", which is exactly what a hand-typed form must never be
    able to do. Setting this narrows, not removes, the read-only
    protection: a genuinely ``computed`` field (a server-derived formula,
    not just a field the client shouldn't touch) still rejects a
    client-submitted value regardless of this flag -- see
    ``_validate_record_against_schema``. Trusted, explicit, opt-in callers
    only; never wired to an HTTP request payload.
    """
    _ensure_collection_known(collection, base_dir=base_dir, roots=roots)
    extra_names = _extra_field_names(collection, base_dir=base_dir, roots=roots)
    clean = _normalize_record_payload(record, require_id=True)
    record_id = clean["id"]
    if not validate_record_id(record_id):
        raise InvalidRecordIdError(f"Invalid record id: {record_id}")
    submitted_fields = frozenset(clean)
    clean = _apply_schema_defaults(collection, clean, base_dir=base_dir, roots=roots)
    _check_field_storable(clean)
    clean = _apply_auto_created_at(collection, clean, submitted_fields, base_dir=base_dir, roots=roots)
    _validate_record_against_schema(
        collection,
        clean,
        submitted_fields=submitted_fields,
        base_dir=base_dir,
        roots=roots,
        allow_read_only_submission=preserve_read_only,
    )
    clean = _canonicalize_schema_values(collection, clean, base_dir=base_dir, roots=roots)
    clean = _route_extra(clean, existing_blob={}, extra_names=extra_names)

    path = collection_records_file(collection, base_dir=base_dir)
    with _records_file_lock(path):
        # _fast_record_lookup resolves the duplicate check via the
        # cheapest source available: warm cache, then (append mode, over
        # threshold) the id->offset sidecar -- neither of which needs a
        # full fold or a copy of every existing record. `records_ref`/
        # `id_index_ref` are the module cache's own uncopied objects when
        # sourced from the cache (or a forced full fold below) -- fine to
        # read (the O(1) duplicate check already happened above) or
        # capture in a closure that builds a fresh copy lazily, only if a
        # full rewrite turns out to be needed (see _persist_write's
        # folded_records_fn). Skipping an eager copy of every existing
        # record is what keeps a fast-append create O(1) rather than
        # O(n): an `any(row.get("id") == ... for row in records)` linear
        # scan plus a full deep-copy of every existing record were,
        # together, the dominant cost of a "fast" append in an earlier
        # version of this function -- and, before the sidecar, the O(n)
        # fold itself was the dominant cost above the cache's row
        # threshold.
        #
        # In append mode, "exists" reflects the last-wins-by-id folded
        # view either way (a deleted id is absent, so a create can reuse
        # it -- resurrection), same as classic mode's index, which never
        # indexes a stale/removed row.
        fields, exists, _, records_ref, id_index_ref = _fast_record_lookup(
            collection, path, record_id, need_row=False
        )
        if exists:
            raise DuplicateRecordIdError(f"Record already exists: {collection}/{record_id}")

        merged_fields = _merge_fields(fields, clean)

        def build_folded_records() -> list[dict[str, str]]:
            if records_ref is not None:
                records = [dict(existing) for existing in records_ref]
            else:
                # Sidecar-sourced lookup: no full record list was ever
                # built. Only reached on a rare full-rewrite path (new
                # field / pending auto-compaction / mode transition), so
                # paying one full fold here -- unavoidable, a full
                # rewrite needs the complete record set -- doesn't
                # regress the fast steady-state append this whole path
                # exists to keep O(1).
                _, full_records, _ = _cache_entry(collection, path)
                records = [dict(existing) for existing in full_records]
            records.append(clean)
            return records

        _persist_write(
            collection,
            path,
            base_dir=base_dir,
            roots=roots,
            prior_fields=fields,
            merged_fields=merged_fields,
            folded_records_fn=build_folded_records,
            delta_row=clean,
            delta_op=OP_UPSERT,
            delta_id=record_id,
        )
        result = _surface_extra(_project_record(clean, merged_fields), extra_names=extra_names)
        object_record_changes.append_record_change(
            collection=collection,
            record_id=record_id,
            action="create",
            before=None,
            after=result,
            actor=actor or "unattributed",
            correlation_id=object_correlation.current_correlation_id(),
            base_dir=base_dir,
        )
        return result


def update_collection_record(
    collection: str,
    record_id: str,
    changes: dict[str, Any],
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
    actor: str | None = None,
    transition_subject: object_permissions.PermissionSubject | None = None,
) -> dict[str, str]:
    """Update one existing record by id and return the stored row.

    See create_collection_record for the attribution contract: every
    successful update is durably attributed, defaulting to
    ``"unattributed"`` when the caller doesn't identify itself.

    ``transition_subject`` is optional and only affects schema-declared
    transition guards (see _validate_field_transitions). The HTTP update
    path resolves the request subject once permissions have already run
    and passes it through here; direct library callers (daemon, CLI,
    tests) that don't pass one are trusted callers -- guarded moves are
    still checked for validity, just not for who is making them.
    """
    _ensure_collection_known(collection, base_dir=base_dir, roots=roots)
    if not validate_record_id(record_id):
        raise InvalidRecordIdError(f"Invalid record id: {record_id}")
    extra_names = _extra_field_names(collection, base_dir=base_dir, roots=roots)

    clean = _normalize_record_payload(changes, require_id=False)
    if "id" in clean and clean["id"] != record_id:
        raise InvalidRecordPayloadError("Record id cannot be changed")
    submitted_fields = frozenset(clean)

    path = collection_records_file(collection, base_dir=base_dir)
    with _records_file_lock(path):
        # See the matching comment in create_collection_record: this
        # resolves via warm cache, then (append mode, over threshold) the
        # id->offset sidecar, before a full fold -- see
        # _fast_record_lookup. `existing_row` is always already a fresh,
        # independent copy. When sourced from the cache (or a forced full
        # fold below), id_index_ref is first-occurrence-wins
        # (_build_id_index), same as the linear scan this replaces, so
        # this changes nothing about WHICH row a duplicate-id file
        # resolves to.
        fields, exists, existing_row, records_ref, id_index_ref = _fast_record_lookup(
            collection, path, record_id, need_row=True
        )
        if not exists:
            raise RecordNotFoundError(f"Record not found: {collection}/{record_id}")

        existing_blob = _parse_extra_blob(existing_row.get(EXTRA_FIELD))
        existing = _surface_extra(existing_row, extra_names=extra_names)

        updated = dict(existing)
        updated.update(clean)
        updated["id"] = record_id
        _validate_record_against_schema(
            collection,
            updated,
            submitted_fields=submitted_fields,
            base_dir=base_dir,
            roots=roots,
        )
        _check_field_storable(updated)
        _validate_field_transitions(
            collection,
            existing,
            updated,
            base_dir=base_dir,
            roots=roots,
            subject=transition_subject,
        )
        updated = _canonicalize_schema_values(
            collection, updated, base_dir=base_dir, roots=roots
        )
        updated = _route_extra(updated, existing_blob=existing_blob, extra_names=extra_names)
        merged_fields = _merge_fields(fields, updated)

        def build_folded_records() -> list[dict[str, str]]:
            if records_ref is not None:
                records = [dict(existing_row) for existing_row in records_ref]
                records[id_index_ref[record_id]] = updated
            else:
                # Sidecar-sourced lookup -- see the matching comment in
                # create_collection_record's build_folded_records.
                _, full_records, full_id_index = _cache_entry(collection, path)
                records = [dict(existing_row) for existing_row in full_records]
                records[full_id_index[record_id]] = updated
            return records

        _persist_write(
            collection,
            path,
            base_dir=base_dir,
            roots=roots,
            prior_fields=fields,
            merged_fields=merged_fields,
            folded_records_fn=build_folded_records,
            delta_row=updated,
            delta_op=OP_UPSERT,
            delta_id=record_id,
        )
        result = _surface_extra(_project_record(updated, merged_fields), extra_names=extra_names)
        object_record_changes.append_record_change(
            collection=collection,
            record_id=record_id,
            action="update",
            before=existing,
            after=result,
            actor=actor or "unattributed",
            correlation_id=object_correlation.current_correlation_id(),
            base_dir=base_dir,
        )
        return result


def delete_collection_record(
    collection: str,
    record_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
    actor: str | None = None,
) -> dict[str, str]:
    """Delete one existing record by id and return the removed row.

    See create_collection_record for the attribution contract: every
    successful delete is durably attributed, defaulting to
    ``"unattributed"`` when the caller doesn't identify itself.
    """
    _ensure_collection_known(collection, base_dir=base_dir, roots=roots)
    if not validate_record_id(record_id):
        raise InvalidRecordIdError(f"Invalid record id: {record_id}")

    path = collection_records_file(collection, base_dir=base_dir)
    with _records_file_lock(path):
        # See the matching comment in create_collection_record.
        # `existing_row` is already a fresh, independent copy -- safe to
        # hand back across the API as-is.
        fields, exists, existing_row, records_ref, id_index_ref = _fast_record_lookup(
            collection, path, record_id, need_row=True
        )
        if not exists:
            raise RecordNotFoundError(f"Record not found: {collection}/{record_id}")

        removed = existing_row

        def build_folded_records() -> list[dict[str, str]]:
            if records_ref is not None:
                records = [dict(existing_row) for existing_row in records_ref]
                records.pop(id_index_ref[record_id])
            else:
                # Sidecar-sourced lookup -- see the matching comment in
                # create_collection_record's build_folded_records.
                _, full_records, full_id_index = _cache_entry(collection, path)
                records = [dict(existing_row) for existing_row in full_records]
                records.pop(full_id_index[record_id])
            return records

        _persist_write(
            collection,
            path,
            base_dir=base_dir,
            roots=roots,
            prior_fields=fields,
            merged_fields=fields,
            folded_records_fn=build_folded_records,
            delta_row=None,
            delta_op=OP_DELETE,
            delta_id=record_id,
        )
        result = _project_record(removed, fields)
        object_record_changes.append_record_change(
            collection=collection,
            record_id=record_id,
            action="delete",
            before=result,
            after=None,
            actor=actor or "unattributed",
            correlation_id=object_correlation.current_correlation_id(),
            base_dir=base_dir,
        )
        return result


def compact_collection(
    collection: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Fold an append-mode collection's records file to its live rows only.

    Rewrites atomically via the same _write_collection_records path a
    plain write uses (temp file + replace), under the collection's file
    lock, keeping the `_op` column (every row becomes an upsert). Callable
    directly (this function), via POST /admin/collections/{collection}/
    compact (object_server.py), or automatically: Auto-compaction
    (_maybe_flag_auto_compact) causes a collection's next ordinary write
    to perform this same rewrite instead of a plain append, and the
    daemon's process_compactions (object_daemon.py) polls append_
    collection_stats and compacts any collection over its bloat/row
    thresholds on a timer (see docs/append-only-storage-design.md
    Compaction: "run on a schedule or when the superseded-row ratio
    passes a threshold — never inline in a request"). This function can
    also be called directly at any time, including on a classic-mode or
    never-written collection, where it is a correctly-reported no-op.

    Returns {"rows_before", "rows_after", "bytes_before", "bytes_after"}
    (row counts are physical rows on disk before/after; for a collection
    not currently in append physical format, before == after).
    """
    _ensure_collection_known(collection, base_dir=base_dir, roots=roots)
    path = collection_records_file(collection, base_dir=base_dir)

    with _records_file_lock(path):
        try:
            bytes_before = path.stat().st_size
        except OSError:
            bytes_before = 0

        physical_header = _physical_header(path)
        if not physical_header or physical_header[0] != OP_FIELD:
            rows_before = len(_read_collection_records(collection, base_dir=base_dir))
            return {
                "rows_before": rows_before,
                "rows_after": rows_before,
                "bytes_before": bytes_before,
                "bytes_after": bytes_before,
            }

        with path.open(newline="") as handle:
            text = handle.read()
        folded_records, physical_row_count = _parse_append_body(collection, text, physical_header)
        logical_fields = physical_header[1:]

        _write_collection_records(
            collection, path, physical_header, folded_records, cache_fields=logical_fields
        )
        _PENDING_COMPACTION.discard(str(path.resolve(strict=False)))
        _discard_oidx(path)

        bytes_after = path.stat().st_size
        return {
            "rows_before": physical_row_count,
            "rows_after": len(folded_records),
            "bytes_before": bytes_before,
            "bytes_after": bytes_after,
        }


def append_collection_stats(
    collection: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
    allow_fold: bool = True,
) -> dict[str, Any] | None:
    """Return compaction-observability stats for one collection, or None
    when it is not currently in append storage mode (docs/storage-modes.md
    "Compaction").

    Returns:
        {"collection", "storage": "append", "physical_rows", "live_rows",
         "dead_rows", "bloat_ratio" (dead_rows / max(live_rows, 1), rounded
         3dp), "file_bytes", "sidecar_present", "compaction_flagged"}

    Cheapest-source-first, same doctrine as _fast_record_lookup's point-op
    resolution (docs/append-only-storage-design.md Sidecars), tried in
    order and combined -- either can answer `live_rows`, but only the
    sidecar can answer `physical_rows` short of a fold:

      1. A warm _RECORDS_CACHE hit (_peek_records_cache) answers
         `live_rows` for free -- it's already the folded record count, no
         I/O at all.
      2. The id->offset sidecar (.records.oidx), read directly via
         _load_oidx_body -- a "pure line-count read" as opposed to a
         records.tsv parse: one line per physical row was written when
         that row was appended (OIDX_FILE's format), so the sidecar's own
         body length IS `physical_rows` without touching records.tsv at
         all, and the same read's last-wins fold gives `live_rows` too
         (used only when tier 1 didn't already answer it). Deliberately
         narrower than the hot-path loader _load_oidx: this never triggers
         a catch-up scan or a from-scratch rebuild when the sidecar is
         stale or missing (`covered_bytes` short of the file's current
         size, wrong header inode, or absent) -- an observability call
         should never be the reason records.tsv gets scanned; it just
         doesn't answer from this tier, and falls through to tier 3 (or
         the estimated shortcut below) instead.
      3. Neither tier answered both numbers: a full fold via
         _parse_append_body -- the same O(file) cost compact_collection
         itself pays to read the file (docs/storage-modes.md: "cold reads
         of an append file cost ~1.6x a classic parse"). This is the ONLY
         expensive path in this function, and only runs when `allow_fold`
         is True (the default for direct/CLI callers, and for the daemon's
         polling pass, where an occasional full fold is an acceptable
         cost against an interval timer). Callers that must never pay it
         -- GET /admin/storage (object_server.py), which must stay O(1)
         regardless of any one collection's cache/sidecar state -- pass
         allow_fold=False and get an {"estimated": True, "physical_rows":
         None, "live_rows": None, "dead_rows": None, "bloat_ratio": None}
         entry instead (still with a real, cheap `file_bytes` from the
         initial stat).

    A collection whose records.tsv doesn't exist yet (schema declared
    "append" but never written) short-circuits to an honest all-zero
    result before any of the above -- that check (`path.exists()`, done as
    part of the initial stat) is itself O(1), so it's never worth
    representing as "estimated".
    """
    _ensure_collection_known(collection, base_dir=base_dir, roots=roots)
    mode = _collection_storage_mode(collection, base_dir=base_dir, roots=roots)
    if mode != object_schemas.STORAGE_APPEND:
        return None

    path = collection_records_file(collection, base_dir=base_dir)
    try:
        st = path.stat()
        file_bytes = st.st_size
        data_ino: int | None = st.st_ino
    except OSError:
        file_bytes = 0
        data_ino = None

    cache_key = str(path.resolve(strict=False))
    sidecar_path = _oidx_path(path)
    sidecar_present = sidecar_path.exists()
    compaction_flagged = cache_key in _PENDING_COMPACTION

    if data_ino is None:
        # No records.tsv at all -- a schema can declare "append" storage
        # before its collection is ever written. Nothing to fold, sidecar
        # or otherwise; report honest zeros rather than routing this
        # through the fold/estimate machinery below.
        return {
            "collection": collection,
            "storage": object_schemas.STORAGE_APPEND,
            "physical_rows": 0,
            "live_rows": 0,
            "dead_rows": 0,
            "bloat_ratio": 0.0,
            "file_bytes": 0,
            "sidecar_present": sidecar_present,
            "compaction_flagged": compaction_flagged,
        }

    live_rows: int | None = None
    physical_rows: int | None = None

    peeked = _peek_records_cache(path)
    if peeked is not None:
        live_rows = len(peeked[1])

    if sidecar_present:
        header_ino = _read_oidx_header(sidecar_path)
        if header_ino == data_ino:
            loaded = _load_oidx_body(sidecar_path)
            if loaded is not None:
                body_dict, body_covered, body_physical = loaded
                covered = (
                    body_covered if body_covered is not None else _records_header_length(path)
                )
                if covered == file_bytes:
                    physical_rows = body_physical
                    if live_rows is None:
                        live_rows = len(body_dict)

    if physical_rows is None or live_rows is None:
        if not allow_fold:
            return {
                "collection": collection,
                "storage": object_schemas.STORAGE_APPEND,
                "physical_rows": None,
                "live_rows": None,
                "dead_rows": None,
                "bloat_ratio": None,
                "file_bytes": file_bytes,
                "sidecar_present": sidecar_present,
                "compaction_flagged": compaction_flagged,
                "estimated": True,
            }

        physical_header = _physical_header(path)
        if not physical_header or physical_header[0] != OP_FIELD:
            # Exists on disk but never written in append physical format
            # (e.g. an empty file) -- nothing to fold.
            physical_rows = 0
            live_rows = 0
        else:
            with path.open(newline="") as handle:
                text = handle.read()
            folded_records, physical_row_count = _parse_append_body(collection, text, physical_header)
            physical_rows = physical_row_count
            live_rows = len(folded_records)

    dead_rows = max(physical_rows - live_rows, 0)
    bloat_ratio = round(dead_rows / max(live_rows, 1), 3)
    return {
        "collection": collection,
        "storage": object_schemas.STORAGE_APPEND,
        "physical_rows": physical_rows,
        "live_rows": live_rows,
        "dead_rows": dead_rows,
        "bloat_ratio": bloat_ratio,
        "file_bytes": file_bytes,
        "sidecar_present": sidecar_present,
        "compaction_flagged": compaction_flagged,
    }


def list_append_collection_stats(
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
    allow_fold: bool = True,
) -> list[dict[str, Any]]:
    """Return append_collection_stats() for every collection whose schema
    currently declares "storage": "append", sorted by collection name.

    Discovery (object_schemas.list_append_storage_collections) is cheap
    regardless of how many collections exist on disk: it globs the schemas
    directory and loads each manual schema through the same mtime/size-
    keyed cache every other schema read in this codebase already goes
    through, never touching records.tsv. A collection whose schema
    disappears between that listing and its own stats call (a benign race
    under concurrent schema edits) is skipped rather than raising -- same
    tolerance _ensure_collection_known's callers generally apply to
    concurrent schema/collection changes.
    """
    names = object_schemas.list_append_storage_collections(base_dir=base_dir)
    stats: list[dict[str, Any]] = []
    for name in names:
        try:
            entry = append_collection_stats(
                name, base_dir=base_dir, roots=roots, allow_fold=allow_fold
            )
        except (
            object_collections.InvalidCollectionNameError,
            object_collections.CollectionNotFoundError,
        ):
            continue
        if entry is not None:
            stats.append(entry)
    return stats


def collection_records_file(collection: str, base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    """Return the validated TSV file path for a collection."""
    if not object_collections.validate_collection_name(collection):
        raise object_collections.InvalidCollectionNameError(
            f"Invalid collection name: {collection}"
        )

    root = Path(base_dir) / COLLECTIONS_DIR
    path = root / collection / RECORDS_FILE
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)

    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise object_collections.InvalidCollectionNameError(
            f"Collection path escapes collection directory: {collection}"
        ) from exc

    return path


def collection_has_records(collection: str, base_dir: Path | str = DEFAULT_DATA_DIR) -> bool:
    """Return True when a collection has a records TSV file."""
    try:
        path = collection_records_file(collection, base_dir=base_dir)
    except object_collections.InvalidCollectionNameError:
        return False
    return path.is_file()


def iter_record_collections(base_dir: Path | str = DEFAULT_DATA_DIR) -> list[str]:
    """Return collection names with a ``records.tsv`` file."""
    root = Path(base_dir) / COLLECTIONS_DIR
    if not root.exists() or not root.is_dir():
        return []

    names = []
    for path in sorted(root.glob(f"*/{RECORDS_FILE}"), key=lambda item: item.as_posix()):
        name = path.parent.name
        if object_collections.validate_collection_name(name):
            names.append(name)
    return names


def _ensure_collection_known(
    collection: str,
    *,
    base_dir: Path | str,
    roots: Iterable[Path] | None,
) -> None:
    if not object_collections.validate_collection_name(collection):
        raise object_collections.InvalidCollectionNameError(
            f"Invalid collection name: {collection}"
        )

    if collection_has_records(collection, base_dir=base_dir):
        return

    try:
        object_schemas.get_schema(collection, base_dir=base_dir, roots=roots)
        return
    except object_schemas.SchemaNotFoundError:
        pass

    raise object_collections.CollectionNotFoundError(f"Collection not found: {collection}")


def _cached_records_ref(
    collection: str,
    *,
    base_dir: Path | str,
) -> list[dict[str, str]]:
    """Return the module cache's own records list for a collection.

    Callers must not mutate the returned list or its dicts in place, and
    must copy before handing anything derived from it back across the
    public API -- see the _RECORDS_CACHE ALIASING SAFETY note above.
    """
    path = collection_records_file(collection, base_dir=base_dir)
    _, records, _ = _cache_entry(collection, path)
    return records


def _read_collection_records(
    collection: str,
    *,
    base_dir: Path | str,
) -> list[dict[str, str]]:
    records = _cached_records_ref(collection, base_dir=base_dir)
    # Copy: `records` is the module cache's own list of its own dicts.
    return [dict(record) for record in records]


def _stat_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _build_id_index(records: list[dict[str, str]]) -> dict[str, int]:
    index: dict[str, int] = {}
    for position, record in enumerate(records):
        record_id = record.get("id")
        if record_id and record_id not in index:
            index[record_id] = position
    return index


def _env_int(name: str, default: int) -> int:
    """Read an int env var, falling back to `default` when unset or unparseable."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _records_cache_max_entries() -> int:
    return _env_int(RECORDS_CACHE_MAX_ENTRIES_ENV, _DEFAULT_RECORDS_CACHE_MAX_ENTRIES)


def _records_cache_max_rows() -> int:
    return _env_int(RECORDS_CACHE_MAX_ROWS_ENV, _DEFAULT_RECORDS_CACHE_MAX_ROWS)


def _oidx_cache_max_entries() -> int:
    return _env_int(OIDX_CACHE_MAX_ENTRIES_ENV, _DEFAULT_OIDX_CACHE_MAX_ENTRIES)


def _store_records_cache(
    cache_key: str,
    signature: tuple[int, int, int],
    fields: list[str],
    records: list[dict[str, str]],
    id_index: dict[str, int],
    covered_bytes: int,
) -> None:
    """Insert/refresh one entry as most-recently-used, then evict over capacity.

    Eviction is pure LRU by entry count: once the dict exceeds
    DBBASIC_RECORDS_CACHE_MAX_ENTRIES, the oldest (least-recently-used)
    entries are dropped first via OrderedDict.popitem(last=False).

    `covered_bytes` is normally just `signature[1]` (every caller except
    _try_incremental_cache_entry's torn-tail case passes exactly that) --
    see the _RECORDS_CACHE block comment above for what it means and the
    one case where it differs.
    """
    _RECORDS_CACHE[cache_key] = (signature, fields, records, id_index, covered_bytes)
    _RECORDS_CACHE.move_to_end(cache_key)
    max_entries = _records_cache_max_entries()
    while len(_RECORDS_CACHE) > max_entries:
        _RECORDS_CACHE.popitem(last=False)


def _peek_records_cache(
    path: Path,
) -> tuple[list[str], list[dict[str, str]], dict[str, int]] | None:
    """Return a warm _RECORDS_CACHE hit for `path`, or None on any miss.

    Never parses: this is the "is there already a fully-folded view of
    this file sitting in memory" check used to decide, in
    _fast_record_lookup, whether a point op can skip both a full fold AND
    the sidecar (cache hit -- the common case for any collection at or
    under DBBASIC_RECORDS_CACHE_MAX_ROWS once warm) or must try the
    cheaper-than-a-fold sidecar path next (a miss -- cold cache, an
    evicted LRU entry, or a collection that has never fit under the row
    threshold and so is never stored here at all; see _cache_entry, this
    function's parsing counterpart, which _fast_record_lookup and every
    non-fast-path caller fall back to when neither a cache hit nor the
    sidecar can answer).
    """
    cache_key = str(path.resolve(strict=False))
    signature = _stat_signature(path)
    if signature is None:
        return None
    cached = _RECORDS_CACHE.get(cache_key)
    if cached is not None and cached[0] == signature:
        _RECORDS_CACHE.move_to_end(cache_key)
        return cached[1], cached[2], cached[3]
    return None


def _cache_entry(
    collection: str,
    path: Path,
) -> tuple[list[str], list[dict[str, str]], dict[str, int]]:
    """Return (fields, records, id_index) for path's current content.

    Serves from the module cache when the file's stat signature matches
    (moving the entry to most-recently-used); otherwise parses fresh and,
    when the file exists and its row count is within
    DBBASIC_RECORDS_CACHE_MAX_ROWS, stores the result for next time (a
    collection over that threshold is returned normally but left out of
    the cache -- see the _RECORDS_CACHE block comment above). The returned
    fields/records/id_index are the CACHED objects -- see the
    _RECORDS_CACHE docstring above. Callers within this module must copy
    before handing anything back across the public API.

    On a peek miss, tries one more thing before paying a full parse: if a
    STALE entry is sitting in _RECORDS_CACHE for this exact path and the
    file has purely grown in place (same inode, size beyond what that
    entry covered) on an append-mode file, _try_incremental_cache_entry
    folds only the new tail bytes onto the stale entry instead -- O(new
    rows), not O(file). See that function for the full precondition list
    and fallback rules; anything it can't handle falls through to the
    ordinary full parse below, unchanged.

    Absent that, this is the O(n) fold path: a full parse of the whole
    file whenever there's no warm cache hit. _fast_record_lookup (get/
    create/update/delete's shared point-op resolver) tries
    _peek_records_cache and then the id->offset sidecar FIRST specifically
    to avoid paying this on every write to a collection too large to fit
    in _RECORDS_CACHE; this function remains the correctness fallback for
    everything else (list, read_collection_records, a cold/small
    collection's first touch, and any point op the sidecar can't answer
    authoritatively).
    """
    if not path.exists():
        return ["id"], [], {}
    if not path.is_file():
        raise ValueError(f"Collection records path is not a file: {collection}")

    peeked = _peek_records_cache(path)
    if peeked is not None:
        return peeked

    cache_key = str(path.resolve(strict=False))
    signature = _stat_signature(path)

    incremental = _try_incremental_cache_entry(path, cache_key, signature)
    if incremental is not None:
        return incremental

    fields, records = _parse_records_file(collection, path)
    id_index = _build_id_index(records)
    if signature is not None:
        if len(records) <= _records_cache_max_rows():
            _store_records_cache(cache_key, signature, fields, records, id_index, signature[1])
        else:
            # Too large to cache: don't pin it, and drop any stale entry
            # left over from before this collection grew past the
            # threshold, so it doesn't linger under an old signature.
            _RECORDS_CACHE.pop(cache_key, None)
    return fields, records, id_index


def _try_incremental_cache_entry(
    path: Path,
    cache_key: str,
    signature: tuple[int, int, int] | None,
) -> tuple[list[str], list[dict[str, str]], dict[str, int]] | None:
    """Attempt the tail-delta incremental fold for an append-mode file that
    grew in place since it was last cached (docs/append-only-storage-
    design.md Sidecars, bullet 2: "when a stat signature changes by
    growth alone, parse only the tail delta"). Returns the fresh (fields,
    records, id_index) on success, or None when the fast path doesn't
    apply -- _cache_entry then falls back to its ordinary full parse,
    unchanged.

    This is what makes a WARM cache stay O(delta) instead of O(file) when
    the growth happened outside this process's own write path (another
    process's writer, the CLI, a sibling pool worker, or this process's
    cache having gone cold and been rebuilt from a stat mismatch) -- the
    in-process fast-append path (_refresh_records_cache_after_append)
    already keeps this process's OWN writes O(1); this covers the gap
    where the growth was observed on a read instead.

    Preconditions, ALL required (anything off -> None, no partial work is
    trusted):
      - a stat signature could be taken (file still exists, readable)
      - a PRIOR entry is warm in _RECORDS_CACHE for this exact path (a
        cold/evicted/never-cached collection has nothing to build on --
        the ordinary full parse is the only option)
      - same inode as the prior entry's signature -- a classic-mode
        rewrite, and an append-mode compaction/transition/new-field
        rewrite, all produce a fresh inode via temp+replace
        (_write_collection_records), so this alone rules out every case
        except an in-place append-mode growth
      - the new size is strictly greater than the prior entry's
        covered_bytes -- pure growth, nothing rewritten in place
      - the file's CURRENT physical header is append-format (`_op` first
        column) and its logical fields match the prior entry's fields
        exactly (a header/field change under the same inode should be
        impossible given the point above, but this stays defensive
        rather than trusting that unconditionally)

    On success, parses only the new bytes (_append_tail_delta_records,
    the shared torn-tail-tolerant tail reader also used by the id->offset
    sidecar's catch-up scan -- see _scan_append_tail) and folds them onto
    the PRIOR entry's records/id_index -- never mutating the prior
    entry's own list/dict in place (a fresh list/dict is always built;
    other code in this same event-loop turn may still hold references to
    the prior entry's objects, same rule as every other cache-installing
    function in this module).

    A tombstone (`_op == "del"`) ANYWHERE in the delta bails out (returns
    None, falling back to the full fold) rather than special-casing
    position shifts: a delete removes a slot and shifts every later
    position down by one (see _fold_append_rows), which this cheap
    replace-or-append merge does not model. Deletes are the rare case for
    the append-shaped workloads this exists for (game-server state,
    impression logs, price history), so this is an accepted, documented
    trade, not a gap -- correctness always wins over speed here. A
    blank-id row in the delta (only reachable via a hand-edited file --
    see _fold_append_rows) bails out for the same reason: its fold
    position depends on the FULL row history, which an incremental merge
    doesn't have.

    The row-count cache threshold (DBBASIC_RECORDS_CACHE_MAX_ROWS) is
    still honored: a successful merge that grows the collection past it
    is still returned to the caller (correct, and far cheaper than a
    full parse either way), but is evicted rather than (re)cached -- the
    same rule _cache_entry's full-parse branch applies just above.
    """
    if signature is None:
        return None

    prior = _RECORDS_CACHE.get(cache_key)
    if prior is None:
        return None
    old_signature, old_fields, old_records, old_id_index, old_covered = prior

    if old_signature[2] != signature[2]:
        return None  # different inode: not an in-place append
    if signature[1] <= old_covered:
        return None  # no growth beyond what's already folded

    physical_header = _physical_header(path)
    if not physical_header or physical_header[0] != OP_FIELD:
        return None  # not (or no longer) append-physical
    if physical_header[1:] != old_fields:
        return None  # defensive: header shouldn't change under the same inode

    delta_rows, new_covered = _append_tail_delta_records(path, physical_header, old_covered)
    if not delta_rows:
        return None  # nothing complete to fold yet (e.g. a torn tail only)

    if any(
        row.get(OP_FIELD, OP_UPSERT) == OP_DELETE or not row.get("id")
        for row in delta_rows
    ):
        return None  # tombstone or blank id: fall back to a full fold

    new_records = list(old_records)
    new_id_index = dict(old_id_index)
    for row in delta_rows:
        record_id = row["id"]
        clean = {key: value for key, value in row.items() if key != OP_FIELD}
        idx = new_id_index.get(record_id)
        if idx is not None:
            new_records[idx] = clean
        else:
            new_id_index[record_id] = len(new_records)
            new_records.append(clean)

    global _INCREMENTAL_CACHE_FOLDS
    _INCREMENTAL_CACHE_FOLDS += 1

    if len(new_records) <= _records_cache_max_rows():
        _store_records_cache(cache_key, signature, old_fields, new_records, new_id_index, new_covered)
    else:
        _RECORDS_CACHE.pop(cache_key, None)
    return old_fields, new_records, new_id_index


def _parse_records_file(collection: str, path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Parse one records.tsv file into (fields, records).

    Uses csv.reader rather than csv.DictReader: csv.reader is the same
    C-accelerated tokenizer DictReader wraps, but skips DictReader's
    per-row dict-construction overhead (zip + restkey/restval handling) in
    favor of a direct index walk here. Using csv.reader (not a naive
    line.split("\t")) is required for correctness, not just speed: a value
    written by csv.DictWriter's default QUOTE_MINIMAL may legitimately
    contain a quoted tab or embedded newline, and only real CSV/TSV parsing
    reconstructs that value correctly.

    Dispatches on the physical header (docs/append-only-storage-design.md):
    a file whose first column is `_op` is in append-only physical format
    and is parsed+folded by _parse_append_body; every other file is parsed
    by the original classic-mode routine, unchanged. This dispatch is by
    the FILE's own header, not the collection's current schema `storage`
    setting -- a collection that just switched storage modes keeps reading
    correctly until its next write physically transitions the file (see
    _persist_write), and a collection that switched back to classic still
    reads correctly (via the append path) until its next write compacts
    the `_op` column away.
    """
    with path.open(newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        try:
            header = next(reader)
        except StopIteration:
            header = None
    _validate_header(collection, header)
    physical_fields = list(header)

    if physical_fields and physical_fields[0] == OP_FIELD:
        with path.open(newline="") as handle:
            text = handle.read()
        folded_records, physical_row_count = _parse_append_body(collection, text, physical_fields)
        _maybe_flag_auto_compact(
            path, physical_row_count=physical_row_count, live_row_count=len(folded_records)
        )
        return physical_fields[1:], folded_records

    return physical_fields, _parse_classic_records(collection, path, physical_fields)


def _parse_classic_records(
    collection: str, path: Path, fields: list[str]
) -> list[dict[str, str]]:
    """Parse the data rows of a classic-mode (no `_op` column) file.

    Replicates csv.DictReader's exact semantics for short/long rows and
    blank-line skipping -- see the block comment above _RECORDS_CACHE and
    the tests in tests/test_object_records.py for the specific cases this
    was checked against on the pre-change code. Unchanged from before
    append-only storage existed: a strict parse, no torn-tail tolerance.
    """
    field_count = len(fields)
    with path.open(newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        next(reader, None)  # header, already parsed/validated by the caller

        records: list[dict[str, str]] = []
        row_number = 1  # the header is row 1; DictReader's enumerate started data rows at 2
        for row in reader:
            # csv.DictReader's __next__ silently skips rows the underlying
            # csv.reader returns as [] (blank physical lines) rather than
            # emitting a record for them; replicate that here so row_number
            # (used only in the error message below) still lines up with
            # DictReader's numbering of *emitted* rows, not physical lines.
            if not row:
                continue
            row_number += 1
            if len(row) > field_count:
                # Mirrors DictReader putting overflow values under the
                # `None` restkey, which the pre-change code detected via
                # `None in row`.
                raise ValueError(
                    f"Collection records file has extra fields on row {row_number}: {collection}"
                )
            # Mirrors DictReader's restval=None for a short row, which the
            # pre-change code then normalized to "" (`value if value is not
            # None else ""`) -- do that normalization directly here.
            record = {
                fields[i]: (row[i] if i < len(row) else "")
                for i in range(field_count)
            }
            records.append(record)

    return records


def _committed_prefix_len(text: str) -> int:
    """Char length of the prefix of `text` ending at the last CSV ROW
    TERMINATOR -- a "\\n" seen OUTSIDE a quoted field.

    QUOTE-AWARE, single O(len) forward pass tracking csv quote state: a "\\n"
    INSIDE a quoted field (a multi-line cell) is field content, never a row
    boundary. "" inside a quoted field is an escaped quote (stays quoted),
    matching csv QUOTE_MINIMAL/QUOTE_ALL. Everything after the last real
    terminator is an incomplete (torn) trailing row -- an interrupted write --
    and is excluded. This can only ever trim a torn TAIL, never bisect a
    committed row.
    """
    in_quote = False
    last_row_end = 0
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if in_quote:
            if c == '"':
                if i + 1 < n and text[i + 1] == '"':
                    i += 2          # escaped "" -- stays inside the quoted field
                    continue
                in_quote = False
            i += 1
        else:
            if c == '"':
                in_quote = True
            elif c == "\n":
                last_row_end = i + 1
            i += 1
    return last_row_end


def _drop_torn_tail(text: str) -> str:
    """Return `text` truncated to the end of its last COMPLETE csv row.

    QUOTE-AWARE. This was previously a quote-BLIND check ("ends with \\n", else
    rfind the last \\n) which mistook a "\\n" INSIDE a quoted multi-line field
    for a row boundary -- silently resurrecting a torn multi-line row and, in
    append mode, cascading to swallow later writes (the torn-write durability
    characterization mapped this to ~97% of a multi-line row's write window).
    A row is committed only when a row-terminating "\\n" (outside any quoted
    field) follows it; anything after the last such terminator is an
    interrupted write and is dropped.

    Fast path: a text containing no '"' at all has no quoted fields, so every
    "\\n" is a row terminator and the cheap check is exact -- this keeps
    collections whose cells never need quoting (no tab/quote/newline in any
    value) on the original O(1)/rfind path. Only text with quoting pays the
    O(len) quote-aware scan, and that path is already parsing the same text.
    """
    if text == "":
        return text
    if '"' not in text:
        if text.endswith("\n"):
            return text
        cut = text.rfind("\n")
        return text[: cut + 1] if cut >= 0 else ""
    return text[:_committed_prefix_len(text)]


def _parse_append_body(
    collection: str, text: str, physical_fields: list[str]
) -> tuple[list[dict[str, str]], int]:
    """Parse+fold an append-mode file's body. Returns (folded_records, physical_row_count).

    Torn-tail tolerant: a trailing unterminated line is dropped first
    (_drop_torn_tail); a row that still fails to parse (e.g. a value with
    an embedded newline whose closing quote never arrived, so its
    apparent line boundary landed mid-field) stops consumption at that
    point rather than raising -- everything after an unparseable point is,
    by construction, either that same torn write or unreachable. This
    differs from classic mode, which keeps raising on malformed content
    (see _parse_classic_records): append mode's contract is specifically
    that in-flight writes are tolerated, not silently-corrupt files.
    """
    field_count = len(physical_fields)
    reader = csv.reader(io.StringIO(_drop_torn_tail(text)), delimiter="\t")
    next(reader, None)  # header, already parsed/validated by the caller

    physical_rows: list[dict[str, str]] = []
    while True:
        try:
            row = next(reader)
        except StopIteration:
            break
        except csv.Error:
            break
        if not row:
            continue
        if len(row) > field_count:
            raise ValueError(
                f"Collection records file has extra fields: {collection}"
            )
        record = {
            physical_fields[i]: (row[i] if i < len(row) else "")
            for i in range(field_count)
        }
        physical_rows.append(record)

    return _fold_append_rows(physical_rows), len(physical_rows)


def _fold_append_rows(physical_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Fold physical append-mode rows into live records, last-wins per id.

    An id's position in the returned list is where it was inserted since
    it was last live: an upsert for an id already present updates it in
    place (same position, matching classic mode's update-in-place); an
    upsert for a not-currently-live id inserts at the current end
    (matching classic mode's create-appends); a delete (`_op == "del"`)
    removes the id's slot entirely (matching classic mode's delete
    popping the row) -- so a later upsert for that same id is a fresh
    insertion at the end, not a return to its old position. Built with an
    OrderedDict so this falls out of normal dict semantics rather than
    needing to be modeled by hand; this is what gives append mode the
    same list order as classic mode for the same operation sequence (see
    the equivalence tests in tests/test_object_records.py). `_op` is
    stripped from every folded record.

    Rows with a blank/missing id (only reachable via a hand-edited file --
    every write path in this module requires a valid id) cannot be
    last-wins folded against each other for lack of a key; each is kept as
    its own entry and appended after the id-keyed records, which does not
    preserve their original interleaving with id-keyed rows. This is a
    documented limitation of a pre-existing edge case, not a behavior this
    feature needs to support: normalize_record_payload never lets a create
    omit its id, and update always operates on an already-valid id.
    """
    folded: "OrderedDict[str, dict[str, str]]" = OrderedDict()
    blank_id_rows: list[dict[str, str]] = []
    blank_counter = 0
    for row in physical_rows:
        op = row.get(OP_FIELD, OP_UPSERT)
        record_id = row.get("id", "")
        clean = {key: value for key, value in row.items() if key != OP_FIELD}
        if not record_id:
            if op == OP_DELETE:
                continue
            blank_id_rows.append((f"__blank__{blank_counter}", clean))
            blank_counter += 1
            continue
        if op == OP_DELETE:
            folded.pop(record_id, None)
            continue
        folded[record_id] = clean

    return list(folded.values()) + [record for _, record in blank_id_rows]


def _scan_append_tail(
    path: Path, physical_fields: list[str], start_byte: int
) -> tuple[list[tuple[int, int, dict[str, str]]], int]:
    """Read and parse complete physical rows of an append-mode file from
    `start_byte` to EOF, returning (rows, covered_bytes) where each row is
    (row_start_offset, row_end_offset, physical_record) -- `physical_record`
    keyed by every column in `physical_fields`, `_op` included, exactly
    like a row from _parse_append_body before folding. `covered_bytes` is
    the byte offset up to which parsing is authoritative (>= start_byte,
    equal to it when nothing new/complete was found). `start_byte` must
    land exactly on a row boundary -- every offset any caller ever hands
    back here (a prior scan/cache entry's covered_bytes, or the
    records-header length) always does, since it is always either 0 or a
    previous row's end.

    Torn-tail tolerant exactly like _parse_append_body: a trailing
    unterminated physical line is dropped first (_drop_torn_tail), and a
    row that still fails to parse (or reports more fields than the header
    -- real corruption, not a torn write) stops consumption at that point
    rather than raising. NEVER raises: this is the shared low-level scan
    behind two callers that both need "read complete new rows appended
    since an earlier point, without re-reading or re-folding the whole
    file" -- the id->offset sidecar's incremental catch-up
    (_scan_append_rows_for_offsets, which projects each row down to just
    its (op, id) for the offset index) and the records-cache's tail-delta
    incremental fold (_append_tail_delta_records, which keeps every field
    since it must produce real records). Neither caller may raise either,
    so this doesn't.

    Byte offsets are computed by decoding `path`'s bytes from `start_byte`
    once, then, after each row csv.reader consumes, converting the
    now-consumed prefix of that decoded text back to its exact byte
    length (`str.encode` on the newly consumed slice only, not the whole
    prefix each time, so total work stays O(scanned bytes) instead of
    O(scanned bytes squared)). `start_byte` itself is always a byte
    offset immediately after a "\\n", which is a single ASCII byte and
    therefore never a UTF-8 continuation byte -- so slicing raw bytes from
    there and decoding is always safe.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(start_byte)
            raw = handle.read()
    except OSError:
        return [], start_byte
    if not raw:
        return [], start_byte

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return [], start_byte
    text = _drop_torn_tail(text)
    if not text:
        return [], start_byte

    field_count = len(physical_fields)
    stream = io.StringIO(text)
    reader = csv.reader(stream, delimiter="\t")
    rows: list[tuple[int, int, dict[str, str]]] = []
    prev_char = 0
    byte_cursor = start_byte
    while True:
        try:
            row = next(reader)
        except StopIteration:
            break
        except csv.Error:
            break
        cur_char = stream.tell()
        row_bytes = len(text[prev_char:cur_char].encode("utf-8"))
        prev_char = cur_char
        row_start = byte_cursor
        row_end = byte_cursor + row_bytes
        byte_cursor = row_end
        if not row:
            continue
        if len(row) > field_count:
            break
        record = {
            physical_fields[i]: (row[i] if i < len(row) else "")
            for i in range(field_count)
        }
        rows.append((row_start, row_end, record))

    return rows, byte_cursor


def _append_tail_delta_records(
    path: Path, physical_fields: list[str], start_byte: int
) -> tuple[list[dict[str, str]], int]:
    """Parse complete physical rows appended to an append-mode file since
    `start_byte`, returning (physical_rows, covered_bytes) -- a thin
    projection of _scan_append_tail down to just the row dicts (`_op`
    included, unfolded), for _try_incremental_cache_entry's tail-delta
    fold. See _scan_append_tail for the shared torn-tail-tolerant scan
    this builds on: never raises, stops at the first row it can't make
    sense of.
    """
    scanned, covered_bytes = _scan_append_tail(path, physical_fields, start_byte)
    return [record for _, _, record in scanned], covered_bytes


def _append_compact_min_rows() -> int:
    return _env_int(APPEND_COMPACT_MIN_ROWS_ENV, _DEFAULT_APPEND_COMPACT_MIN_ROWS)


def append_compact_min_rows() -> int:
    """Public read of DBBASIC_APPEND_COMPACT_MIN_ROWS (default 10_000).

    Exists so callers outside this module (object_daemon.py's
    process_compactions) can apply the exact same physical-row-count floor
    _maybe_flag_auto_compact uses for its own auto-compaction trigger,
    without duplicating the env var's default value.
    """
    return _append_compact_min_rows()


def _maybe_flag_auto_compact(path: Path, *, physical_row_count: int, live_row_count: int) -> None:
    """Flag a collection for compaction on its NEXT WRITE.

    Never compacts here -- this runs from the read/parse path, and reads
    must stay read-only (docs/append-only-storage-design.md Compaction).
    Triggers when the physical file is at least DBBASIC_APPEND_COMPACT_
    MIN_ROWS rows (so a small collection's ordinary churn never matters)
    AND superseded-or-deleted rows outnumber live rows.
    """
    if physical_row_count <= _append_compact_min_rows():
        return
    stale_row_count = physical_row_count - live_row_count
    if stale_row_count > live_row_count:
        _PENDING_COMPACTION.add(str(path.resolve(strict=False)))


def _physical_header(path: Path) -> list[str] | None:
    """Return the raw on-disk header (leading `_op` included, if present).

    None when the file doesn't exist or is empty. Cheap: reads only the
    first physical line, not the whole file.
    """
    if not path.exists():
        return None
    with path.open(newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        try:
            return next(reader)
        except StopIteration:
            return None


def _write_collection_records(
    collection: str,
    path: Path,
    fields: list[str],
    records: list[dict[str, str]],
    *,
    cache_fields: list[str] | None = None,
) -> None:
    """Rewrite the whole records file atomically (temp file + replace).

    `fields` is the PHYSICAL header to write -- for an append-mode full
    rewrite (transition-in, compaction, or the new-field-fallback rewrite;
    see _persist_write) this includes a leading `_op` column, which every
    record in `records` implicitly projects to "" (upsert) since none of
    them carry an `_op` key (docs/append-only-storage-design.md).
    `cache_fields`, when given, is the LOGICAL field list (without `_op`)
    to store in the module cache instead of `fields` -- callers writing an
    append-format file must pass this, or `_op` would leak into the cache
    and, from there, into API responses (get/list/create/update all
    return cache-derived data). Classic-mode callers omit it and the
    written `fields` doubles as the cache's fields, exactly as before this
    parameter existed.
    """
    if "id" not in fields:
        fields = ["id", *fields]

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temp_path.open("w", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=fields,
                delimiter="\t",
                lineterminator="\n",
                extrasaction="ignore",
                quoting=(csv.QUOTE_ALL if _rows_need_full_quoting(records) else csv.QUOTE_MINIMAL),
            )
            writer.writeheader()
            for record in records:
                writer.writerow(_project_record(record, fields))
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    _refresh_records_cache(collection, path, cache_fields if cache_fields is not None else fields, records)


def _refresh_records_cache(
    collection: str,
    path: Path,
    fields: list[str],
    records: list[dict[str, str]],
) -> None:
    """Populate the module cache with the content just written to `path`.

    Called immediately after the atomic replace above, under the caller's
    _records_file_lock, so this process's view of the file is authoritative
    right now. Storing the new content (rather than merely invalidating)
    means the next read for this collection -- often the same request,
    returning its own write -- skips re-parsing the file it just wrote.

    If the post-write stat can't be trusted (the file vanished, or some
    other OSError), skip caching entirely rather than caching something
    stale or guessed: the next reader will just parse fresh, same as
    before this change existed.

    Honors the same DBBASIC_RECORDS_CACHE_MAX_ROWS threshold as
    _cache_entry: a write that grows a collection past the threshold is
    not cached (and any stale smaller-collection entry under this key is
    dropped), so a single giant write can't occupy an LRU slot.
    """
    signature = _stat_signature(path)
    if signature is None:
        return

    cache_key = str(path.resolve(strict=False))
    if len(records) > _records_cache_max_rows():
        _RECORDS_CACHE.pop(cache_key, None)
        return

    # Project every record to the full field set before caching, so a
    # cache hit yields exactly what a fresh parse of this file would
    # yield (same as what was just written: `_project_record` fills any
    # field missing from a given record with "", matching how
    # csv.DictWriter/extrasaction wrote that row).
    cached_records = [_project_record(record, fields) for record in records]
    id_index = _build_id_index(cached_records)
    _store_records_cache(cache_key, signature, list(fields), cached_records, id_index, signature[1])


def _refresh_records_cache_after_append(
    collection: str,
    path: Path,
    fields: list[str],
    delta_op: str,
    delta_id: str,
    projected_row: dict[str, str],
) -> bool:
    """Incrementally update the cache after a fast append. Returns True if
    it did (the caller then skips the full _refresh_records_cache rebuild).

    The entire point of the fast-append path is O(1) disk work -- calling
    the general _refresh_records_cache after every append would silently
    reintroduce an O(n) cost per write, just moved from disk I/O to cache
    bookkeeping (a full re-projection of every record plus a full
    id_index rebuild). This instead derives the new cache entry from the
    PRIOR entry plus this one delta:

      - create (an id not already in the index): `old_records + [row]`
        and `dict(old_id_index)` plus one new key -- still technically
        O(n) (a fresh list/dict must be built; nothing cached is ever
        mutated in place, see the ALIASING SAFETY note on
        _RECORDS_CACHE), but as bulk C-level copies (list concat, dict
        copy) rather than a Python-level per-record rebuild.
      - update (an id already in the index -- always at the SAME
        position, since _fold_append_rows updates in place): `list(
        old_records)` with one element replaced; the id_index is REUSED
        UNCHANGED (no copy at all), since no id's position moved.
      - delete: a genuine O(n) Python-level index rebuild (removing a
        position means every later position shifts down by one) -- the
        one case this doesn't make cheap. Deletes are the rare case for
        the append-shaped workloads this feature targets (game-server
        state, impression logs, price history), so this is an accepted,
        documented trade rather than a gap.

    Returns False when there is no usable prior entry to build on -- cold
    cache, evicted, over the row-count cache threshold, or (defensively)
    a fields mismatch. The caller (_persist_write's fast-append branch)
    does NOT force a full rebuild in that case (an earlier version of
    this code did, via folded_records_fn() + _refresh_records_cache --
    exactly the O(n)-per-write cost the id->offset sidecar, docs/append-
    only-storage-design.md item 4, exists to eliminate for a collection
    too large to be a "usable prior entry" in the first place). A False
    return simply leaves _RECORDS_CACHE as it was: the next reader either
    hits the sidecar (if the file is append-physical and over threshold)
    or pays one full fold on a genuine cold/small-collection miss, same
    as any other cache miss elsewhere in this module.
    """
    signature = _stat_signature(path)
    if signature is None:
        return True  # nothing to cache either way; nothing left to do

    cache_key = str(path.resolve(strict=False))
    prior = _RECORDS_CACHE.get(cache_key)
    if prior is None or prior[1] != fields:
        return False

    _, _prior_fields, old_records, old_id_index, _prior_covered = prior
    if len(old_records) > _records_cache_max_rows():
        _RECORDS_CACHE.pop(cache_key, None)
        return True

    if delta_op == OP_DELETE:
        idx = old_id_index.get(delta_id)
        if idx is None:
            return False
        new_records = old_records[:idx] + old_records[idx + 1:]
        new_id_index = {
            key: (value if value < idx else value - 1)
            for key, value in old_id_index.items()
            if key != delta_id
        }
    else:
        idx = old_id_index.get(delta_id)
        if idx is not None:
            new_records = list(old_records)
            new_records[idx] = projected_row
            new_id_index = old_id_index
        else:
            new_records = old_records + [projected_row]
            new_id_index = dict(old_id_index)
            new_id_index[delta_id] = len(old_records)

    if len(new_records) > _records_cache_max_rows():
        _RECORDS_CACHE.pop(cache_key, None)
        return True

    # This process just performed the append that produced `signature`, so
    # it authoritatively covers the whole current file, same as any other
    # fully-covering entry (see the covered_bytes bullet in the
    # _RECORDS_CACHE block comment above).
    _store_records_cache(cache_key, signature, list(fields), new_records, new_id_index, signature[1])
    return True


def _repair_torn_tail(path: Path) -> None:
    """Ensure `path` ends with a complete row before an append lands.

    A write interrupted mid-row leaves a fragment; appending after it would
    concatenate the next row onto the fragment -- and if the fragment holds an
    unclosed quoted field, silently swallow that next row. This truncates the
    file to the end of its last COMPLETE csv row, computed QUOTE-AWARE
    (_committed_prefix_len), so a "\\n" INSIDE a quoted multi-line field is
    never mistaken for a row boundary. That was the old backward
    rfind(b"\\n")'s bug (substrate bug #2): it could cut mid-quoted-field,
    leave an open-quote fragment, and let the next append be swallowed.

    Cheap common case: the in-memory oidx cache records how many bytes of the
    file the last completed write covered. A torn tail can only appear after a
    process DEATH, which clears the in-memory cache -- so a WARM cache whose
    covered_bytes equals the current size proves the file ends exactly at a
    committed row, and we return without reading it. The quote-aware whole-file
    scan below runs only when the cache is cold (the first append after a
    restart -- exactly when a torn tail might exist and repair matters) or
    stale (e.g. a concurrent cross-process append this process hasn't cached);
    in the stale case the scan simply finds the file already clean and trims
    nothing. Correctness rests entirely on _committed_prefix_len, which only
    ever trims a torn TAIL and never bisects a committed row; the cache gate
    affects only WHEN the scan runs, never its result.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size == 0:
        return

    with path.open("rb+") as handle:
        handle.seek(size - 1)
        if handle.read(1) == b"\n":
            return

        window = 8192
        read_from = size
        while read_from > 0:
            read_from = max(0, read_from - window)
            handle.seek(read_from)
            chunk = handle.read(size - read_from)
            idx = chunk.rfind(b"\n")
            if idx >= 0:
                handle.truncate(read_from + idx + 1)
                return
            window *= 2
        handle.truncate(0)


def _append_records_rows(
    path: Path,
    physical_fields: list[str],
    rows: list[dict[str, str]],
) -> list[tuple[int, int]]:
    """Append one or more pre-built physical rows to an append-format file.

    Each row in `rows` must already carry an `_op` key (OP_UPSERT or
    OP_DELETE). Self-heals a torn tail first (_repair_torn_tail) so the
    new row(s) always land as clean, complete physical lines, then writes
    via csv.writer (not DictWriter -- rows are already fully projected)
    on a text-mode "a" handle, flushing once at the end. Same dialect as
    _write_collection_records: tab-delimited, "\\n" line terminator,
    QUOTE_MINIMAL.

    Returns the (row_start_offset, row_end_offset) byte span written for
    each row, in order -- _persist_write hands these straight to the id
    sidecar (_update_oidx_after_append) so it never has to re-derive them
    with a separate stat or scan. `handle.tell()` on a text-mode stream is
    only reliably a byte offset when nothing has been read from it in
    this session (reading with a stateful decoder can return an opaque
    cookie instead); a pure sequential-write "a" session like this one
    never reads, so it's exact here -- verified empirically (UTF-8,
    non-ASCII content, mid-stream tell() calls all matched the file's
    actual byte length).
    """
    _repair_torn_tail(path)
    offsets: list[tuple[int, int]] = []
    with path.open("a", newline="") as handle:
        writer = csv.writer(
            handle, delimiter="\t", lineterminator="\n",
            quoting=(csv.QUOTE_ALL if _rows_need_full_quoting(rows) else csv.QUOTE_MINIMAL),
        )
        for row in rows:
            start = handle.tell()
            projected = _project_record(row, physical_fields)
            writer.writerow([projected[field] for field in physical_fields])
            offsets.append((start, handle.tell()))
        handle.flush()
    return offsets


# ---------------------------------------------------------------------------
# id -> byte-offset sidecar (docs/append-only-storage-design.md item 4).
#
# SCOPE: every function below is reached only from append-physical code
# paths -- _fast_record_lookup only looks past a _peek_records_cache miss
# into the sidecar when `_physical_header(path)[0] == OP_FIELD`, and
# _persist_write/_write_collection_records only call _update_oidx_after_
# append/_discard_oidx on the append-mode branches. A collection that has
# never been append-mode (no schema ever set "storage": "append") never
# executes a line past that header check: its _physical_header is a plain
# ["id", ...] with no leading `_op`, so the guard is false every time.
# ---------------------------------------------------------------------------


def _oidx_path(records_path: Path) -> Path:
    return records_path.with_name(OIDX_FILE)


def _store_oidx_cache(
    cache_key: str,
    data_ino: int,
    covered_bytes: int,
    id_offsets: dict[str, int],
) -> None:
    _OIDX_CACHE[cache_key] = (data_ino, covered_bytes, id_offsets)
    _OIDX_CACHE.move_to_end(cache_key)
    max_entries = _oidx_cache_max_entries()
    while len(_OIDX_CACHE) > max_entries:
        _OIDX_CACHE.popitem(last=False)


def _records_header_length(path: Path) -> int:
    """Byte length of records.tsv's own physical header line (newline
    included), or 0 if the file is empty/missing. Reads only the first
    physical line -- cheap regardless of file size."""
    try:
        with path.open("rb") as handle:
            return len(handle.readline())
    except OSError:
        return 0


def _scan_append_rows_for_offsets(
    path: Path,
    physical_fields: list[str],
    start_byte: int,
) -> tuple[list[tuple[int, int, str, str]], int, int]:
    """Scan records.tsv from `start_byte` to EOF for complete physical
    rows, returning (entries, covered_bytes, row_count).

    `entries` is one (row_start, row_end, op, id) per row found with a
    non-empty id (blank-id rows -- reachable only via a hand-edited file,
    see _fold_append_rows -- can't be looked up by id, so are simply never
    indexed, though they DO count towards `row_count`, matching
    _parse_append_body's physical_row_count exactly); `covered_bytes` is
    the byte offset up to which scanning is authoritative (>= start_byte,
    equal to it when nothing new/complete was found). `start_byte` must
    land exactly on a row boundary -- every offset this module ever hands
    back here (a prior scan's covered_bytes, or the records-header
    length) always does, since it is always either 0 or a previous
    row_end.

    Torn-tail tolerant exactly like _parse_append_body: a trailing
    unterminated physical line is dropped first (_drop_torn_tail), and a
    row that still fails to parse (or reports more fields than the header
    -- a real corruption, not a torn write) stops consumption at that
    point rather than raising. This function must NEVER raise: it is the
    sidecar's own scan, and the sidecar is never allowed to be less
    tolerant than the reader it exists to speed up (any row it can't
    make sense of just isn't indexed yet -- the caller's `coherent` flag,
    derived from comparing covered_bytes to the file's actual size,
    reports that honestly rather than guessing).

    Delegates the actual scan to _scan_append_tail (shared with the
    records-cache's tail-delta incremental fold, _append_tail_delta_
    records) and projects each full physical row down to just (op, id)
    plus its byte span -- all this function's callers need.
    """
    scanned, covered_bytes = _scan_append_tail(path, physical_fields, start_byte)
    entries: list[tuple[int, int, str, str]] = []
    row_count = 0
    for row_start, row_end, record in scanned:
        row_count += 1
        record_id = record.get("id", "")
        if not record_id:
            continue
        op = record.get(OP_FIELD, OP_UPSERT)
        entries.append((row_start, row_end, op, record_id))

    return entries, covered_bytes, row_count


def _fold_oidx_entries(
    entries: list[tuple[int, int, str, str]],
    *,
    base: dict[str, int] | None = None,
) -> dict[str, int]:
    """Fold sidecar entries last-wins into {id: row_start_offset},
    dropping deleted ids -- the same rule _fold_append_rows applies to
    full rows, applied here to bare offsets.

    Mutates and returns `base` directly when given, rather than copying
    it first: every call site's `base` is either freshly built moments
    earlier with nothing else referencing it (_load_oidx_body's return),
    or is _OIDX_CACHE's own entry being brought up to date in a catch-up
    that is about to replace that same cache slot anyway (see the
    matching note on _update_oidx_after_append for why this dict is safe
    to mutate in place: it's never exposed outside this module). A copy
    here would be a one-time-per-catch-up cost rather than a per-write
    one, so it's lower-stakes than the bug that note describes -- but
    there's no reason to pay even that when nothing needs the old
    version preserved.
    """
    result = base if base is not None else {}
    for row_start, _row_end, op, record_id in entries:
        if op == OP_DELETE:
            result.pop(record_id, None)
        else:
            result[record_id] = row_start
    return result


def _write_oidx_file(
    oidx_path: Path, data_ino: int, entries: list[tuple[int, int, str, str]]
) -> None:
    """Write a fresh sidecar from scratch (REBUILD), atomically (temp file
    + replace) so a concurrent reader never observes a half-written file
    mid-rebuild -- unlike the steady-state append path
    (_append_oidx_lines), which is cheap specifically because it skips
    this."""
    oidx_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = oidx_path.with_name(f".{oidx_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temp_path.open("w", newline="") as handle:
            handle.write(f"{OIDX_HEADER_TAG}\t{data_ino}\n")
            handle.writelines(
                f"{row_start}\t{row_end}\t{op}\t{record_id}\n"
                for row_start, row_end, op, record_id in entries
            )
        temp_path.replace(oidx_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _append_oidx_lines(
    oidx_path: Path, data_ino: int, entries: list[tuple[int, int, str, str]]
) -> None:
    """Append idx lines to an existing sidecar whose header ino the
    caller has already confirmed matches `data_ino`. Self-heals a torn
    idx tail first (same helper the data file uses -- the format needs no
    CSV awareness to repair: an idx line can never contain a literal tab
    or newline inside a field, since ids are restricted to _RECORD_ID_RE
    and offsets/op are plain digits/literals, so "does the file end with
    \\n" is exactly as reliable a completeness signal here as it is for
    records.tsv)."""
    if not entries:
        return
    _repair_torn_tail(oidx_path)
    with oidx_path.open("a", newline="") as handle:
        handle.writelines(
            f"{row_start}\t{row_end}\t{op}\t{record_id}\n"
            for row_start, row_end, op, record_id in entries
        )
        handle.flush()


def _read_oidx_header(oidx_path: Path) -> int | None:
    """Return the data_ino recorded in the sidecar's header line, or None
    when the file is missing, empty, or its header doesn't parse (all
    treated identically by the caller: rebuild)."""
    try:
        with oidx_path.open("r", newline="") as handle:
            first_line = handle.readline()
    except OSError:
        return None
    parts = first_line.rstrip("\n").split("\t")
    if len(parts) != 2 or parts[0] != OIDX_HEADER_TAG:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _load_oidx_body(
    oidx_path: Path,
) -> tuple[dict[str, int], int | None, int] | None:
    """Parse a sidecar's data lines (everything after the header) into
    ({id: offset}, covered_bytes, indexed_row_count). Returns None when the
    file can't be trusted at all (missing, or a data line doesn't have
    exactly the 4 expected columns / non-integer offsets -- genuine
    corruption, not a torn write, so this doesn't try to salvage a prefix:
    the caller rebuilds instead of guessing).

    covered_bytes is None when there are no data lines yet (a header-only
    sidecar, e.g. right after this collection's very first indexed
    write): the caller supplies the real baseline in that case
    (_records_header_length), since a brand-new sidecar covers exactly
    the data file's own header bytes, not zero.

    indexed_row_count is the number of body lines parsed -- one per
    PHYSICAL row of records.tsv this sidecar has indexed (create, update,
    and delete rows all count; only the blank-id rows
    _scan_append_rows_for_offsets never indexes are excluded, per that
    function's docstring). Exists for object_records.append_collection_
    stats' cheap physical-row-count path (a "pure line-count read of the
    sidecar", avoiding a full fold of records.tsv) -- no other caller
    needs it, so it costs this function nothing beyond incrementing a
    counter already being iterated for the fold below.

    A torn last line (the file doesn't end with "\\n") is dropped, same
    rule as everywhere else: an idx line, like a data row, is only
    trustworthy once its own newline lands.
    """
    try:
        text = oidx_path.read_text()
    except OSError:
        return None
    lines = text.split("\n")
    if not lines or not lines[0].startswith(f"{OIDX_HEADER_TAG}\t"):
        return None

    body_lines = lines[1:]
    if body_lines and body_lines[-1] == "":
        body_lines = body_lines[:-1]  # file ends with \n -- drop the trailing empty split
    elif body_lines:
        body_lines = body_lines[:-1]  # torn last line -- drop it, unindexed

    result: dict[str, int] = {}
    covered_bytes: int | None = None
    indexed_row_count = 0
    for line in body_lines:
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            return None
        try:
            row_start = int(parts[0])
            row_end = int(parts[1])
        except ValueError:
            return None
        op, record_id = parts[2], parts[3]
        if op == OP_DELETE:
            result.pop(record_id, None)
        else:
            result[record_id] = row_start
        covered_bytes = row_end
        indexed_row_count += 1

    return result, covered_bytes, indexed_row_count


def _oidx_get_row(
    path: Path, physical_fields: list[str], offset: int
) -> dict[str, str] | None:
    """Seek to `offset` in records.tsv and parse exactly the one physical
    row starting there, returning the LOGICAL record (id + fields, `_op`
    stripped) -- or None if the row can't be read/parsed cleanly, which
    the caller treats as "sidecar inconclusive, fall back to a full
    fold" rather than as "record missing"."""
    # Read the full LOGICAL row via csv, not a raw readline(). A row whose
    # value contains an embedded newline (a quoted field spanning several
    # physical lines -- e.g. multi-line text, or pretty-printed JSON) must be
    # consumed to its real row terminator; a readline() would stop at the
    # first '\n' byte and silently return a truncated (often unparseable)
    # value. csv.reader over the file from `offset` handles the quoted-field
    # continuation exactly as the full-fold path (_parse_append_body) does,
    # keeping the by-id read consistent with it. Any read/parse trouble ->
    # None, so the caller falls back to a full fold rather than trusting a
    # partial row.
    try:
        handle = path.open("rb")
    except OSError:
        return None
    try:
        handle.seek(offset)
        text = io.TextIOWrapper(handle, encoding="utf-8", newline="")
        row = next(csv.reader(text, delimiter="\t"))
    except (OSError, UnicodeDecodeError, StopIteration, csv.Error):
        return None
    finally:
        handle.close()
    field_count = len(physical_fields)
    if not row or len(row) > field_count:
        return None
    record = {
        physical_fields[i]: (row[i] if i < len(row) else "")
        for i in range(field_count)
    }
    return {key: value for key, value in record.items() if key != OP_FIELD}


def _load_oidx(path: Path) -> tuple[dict[str, int], bool]:
    """Return ({id: row_start_offset}, coherent) for `path`'s CURRENT
    append-mode content, using and maintaining the on-disk sidecar plus
    the small in-memory _OIDX_CACHE LRU.

    `coherent` is True exactly when the returned dict accounts for every
    byte of the file as observed by this call's own stat -- i.e. it is
    safe to treat "id not in the dict" as proof the id doesn't currently
    exist. False means only a best-effort snapshot could be produced
    (normally: a torn tail at EOF from a write that crashed before
    self-healing) and callers must not treat an absence -- or, to be
    conservative, even a presence -- as authoritative; see
    _fast_record_lookup, the only caller, which falls all the way back to
    a full fold whenever this is False.

    100% best-effort and NEVER raises: any inconsistency (missing sidecar,
    corrupt sidecar, inode mismatch, a data file that shrank under a
    stable inode -- e.g. a torn-tail repair truncation) triggers a
    rebuild (one sequential scan of records.tsv) rather than propagating
    an error, per docs/append-only-storage-design.md: the sidecar is
    "never required for correctness."
    """
    try:
        st = path.stat()
    except OSError:
        return {}, False
    data_ino = st.st_ino
    data_size = st.st_size
    cache_key = str(path.resolve(strict=False))

    cached = _OIDX_CACHE.get(cache_key)
    if cached is not None:
        cached_ino, cached_covered, cached_dict = cached
        if cached_ino == data_ino and cached_covered <= data_size:
            if cached_covered == data_size:
                _OIDX_CACHE.move_to_end(cache_key)
                return cached_dict, True
            physical_fields = _physical_header(path)
            if physical_fields and physical_fields[0] == OP_FIELD:
                entries, new_covered, _row_count = _scan_append_rows_for_offsets(
                    path, physical_fields, cached_covered
                )
                new_dict = _fold_oidx_entries(entries, base=cached_dict)
                _append_oidx_lines(_oidx_path(path), data_ino, entries)
                _store_oidx_cache(cache_key, data_ino, new_covered, new_dict)
                return new_dict, new_covered == data_size
        # ino mismatch, or the file shrank under a stable inode: this
        # in-memory snapshot can't be trusted as a base -- drop it and
        # fall through to the on-disk / rebuild path below.
        _OIDX_CACHE.pop(cache_key, None)

    physical_fields = _physical_header(path)
    if not physical_fields or physical_fields[0] != OP_FIELD:
        return {}, False

    oidx_path = _oidx_path(path)
    header_ino = _read_oidx_header(oidx_path)
    if header_ino == data_ino:
        loaded = _load_oidx_body(oidx_path)
        if loaded is not None:
            body_dict, body_covered, _body_row_count = loaded
            covered = body_covered if body_covered is not None else _records_header_length(path)
            if covered <= data_size:
                if covered < data_size:
                    entries, covered, _row_count = _scan_append_rows_for_offsets(
                        path, physical_fields, covered
                    )
                    body_dict = _fold_oidx_entries(entries, base=body_dict)
                    _append_oidx_lines(oidx_path, data_ino, entries)
                _store_oidx_cache(cache_key, data_ino, covered, body_dict)
                return body_dict, covered == data_size
            # covered > data_size: the data file shrank under a stable
            # ino -- fall through to a full rebuild rather than trust a
            # sidecar that claims to know about bytes no longer there.

    # REBUILD: a full sequential scan of records.tsv from its own header,
    # functionally equivalent to the classic full-fold parse this branch
    # replaces for an over-threshold collection -- so, like that parse
    # (_parse_records_file -> _maybe_flag_auto_compact), it's also the
    # right place to (re-)evaluate auto-compaction. Incremental catch-up
    # above deliberately does NOT re-flag: this is a documented, narrower
    # trigger window than the pre-sidecar behavior (every full fold used
    # to re-check on every cold read) -- a collection gets an accurate
    # check whenever its sidecar needs rebuilding (first use past the
    # cache threshold, or any time compaction/a mode switch/corruption
    # invalidates it) but not on every subsequent catch-up scan. Flagging
    # on every catch-up too would need a persisted running physical-row
    # count in the sidecar itself; not worth the format complexity for a
    # threshold heuristic that self-corrects the next time a rebuild does
    # happen.
    header_bytes = _records_header_length(path)
    entries, covered, row_count = _scan_append_rows_for_offsets(path, physical_fields, header_bytes)
    fresh_dict = _fold_oidx_entries(entries)
    _write_oidx_file(oidx_path, data_ino, entries)
    _store_oidx_cache(cache_key, data_ino, covered, fresh_dict)
    if covered == data_size:
        _maybe_flag_auto_compact(path, physical_row_count=row_count, live_row_count=len(fresh_dict))
    return fresh_dict, covered == data_size


def _discard_oidx(path: Path) -> None:
    """Drop any sidecar tracking (in-memory and on-disk) for `path` --
    called after a full rewrite of an append-physical file (compaction, a
    mode-transition rewrite in either direction, or the new-field
    fallback rewrite): the rewrite gives the file a new inode via
    temp+rename, which would make any existing sidecar's header ino stale
    even without this (the next _load_oidx would detect the mismatch and
    rebuild on its own) -- this just does it eagerly so a compacted or
    mode-switched collection's directory doesn't carry a permanently
    orphaned `.records.oidx` around
    (docs/append-only-storage-design.md Sidecars: "delete or rebuild
    sidecar after the rewrite"). Never called for a collection with no
    append-mode history -- see the SCOPE note above this section -- so a
    purely classic collection's write path never reaches here. Best-
    effort: an unlink failure is not a correctness problem, so this never
    raises.
    """
    cache_key = str(path.resolve(strict=False))
    _OIDX_CACHE.pop(cache_key, None)
    try:
        _oidx_path(path).unlink()
    except OSError:
        pass


def _update_oidx_after_append(
    path: Path,
    rows: list[dict[str, str]],
    offsets: list[tuple[int, int]],
) -> None:
    """Keep the sidecar in sync with a fast append, in O(1): appends
    matching idx line(s) to the on-disk sidecar and updates the in-memory
    _OIDX_CACHE entry -- but ONLY when that entry is already warm for
    this path. When it isn't, this does nothing at all: a write must
    never pay to build or maintain a sidecar nobody is currently reading
    through (see _load_oidx, which lazily builds/catches-up on the next
    point op that actually needs it -- "Sidecar builds lazily on first
    over-threshold op").

    `offsets` are exactly what _append_records_rows just returned for
    `rows` (same order, same length) -- no re-derivation (stat or scan)
    needed to keep this O(1) regardless of collection size.

    Mutates `cached_dict` IN PLACE rather than copying it first, UNLIKE
    _refresh_records_cache_after_append's equivalent step for
    _RECORDS_CACHE (which always copies, even on its "fast" path -- see
    that function's docstring). That copy is safe there because
    _RECORDS_CACHE's records/id_index are handed across the public API
    (get/list return values, directly or via one more copy) and so must
    never be mutated out from under an earlier caller -- and it's AFFORD-
    ABLE there because _RECORDS_CACHE only ever holds collections at or
    under DBBASIC_RECORDS_CACHE_MAX_ROWS. Neither holds for this dict: it
    is never exposed outside this module (every public entry point that
    might have sourced data from it -- get/create/update/delete via
    _fast_record_lookup -- only ever calls dict.get() on it and returns a
    freshly-parsed ROW, never the dict itself), and it exists specifically
    FOR collections too large for a copy to be the cheap operation it is
    for _RECORDS_CACHE (a 1M-entry dict copy alone was measured to add
    ~35-40ms to every single write here -- see the sidecar benchmark
    report -- reintroducing real per-write cost in exactly the size
    regime this feature exists to make O(1) again). A concurrent
    lock-free reader's dict.get() racing this mutation is benign under
    the GIL (each op is atomic; a race can only return a value from
    just-before or just-after the write, never corrupt memory or raise)
    -- the same class of eventual-consistency already documented and
    accepted for _RECORDS_CACHE's own CONCURRENCY note above.
    """
    cache_key = str(path.resolve(strict=False))
    cached = _OIDX_CACHE.get(cache_key)
    if cached is None:
        return
    cached_ino, cached_covered, cached_dict = cached
    if cached_covered != offsets[0][0]:
        # The cached view doesn't start exactly where this append starts
        # (should not happen -- writes are serialized under the caller's
        # file lock and this cache is only ever advanced by this same
        # process's own writes/reads) -- rather than risk recording wrong
        # offsets, drop it; the next _load_oidx call resyncs or rebuilds.
        _OIDX_CACHE.pop(cache_key, None)
        return

    entries: list[tuple[int, int, str, str]] = []
    for row, (start, end) in zip(rows, offsets, strict=True):
        op = row.get(OP_FIELD, OP_UPSERT)
        record_id = row.get("id", "")
        entries.append((start, end, op, record_id))
        if op == OP_DELETE:
            cached_dict.pop(record_id, None)
        else:
            cached_dict[record_id] = start

    _append_oidx_lines(_oidx_path(path), cached_ino, entries)
    _store_oidx_cache(cache_key, cached_ino, offsets[-1][1], cached_dict)


def _fast_record_lookup(
    collection: str,
    path: Path,
    record_id: str,
    *,
    need_row: bool,
) -> tuple[
    list[str],
    bool,
    dict[str, str] | None,
    list[dict[str, str]] | None,
    dict[str, int] | None,
]:
    """Resolve (fields, exists, existing_row, records_ref, id_index_ref)
    for one record id -- the shared point-op resolver behind get/create/
    update/delete, in increasing order of cost:

      1. A warm _RECORDS_CACHE hit (_peek_records_cache) -- the ordinary
         fast path for any collection at or under
         DBBASIC_RECORDS_CACHE_MAX_ROWS once touched once. records_ref/
         id_index_ref are the module cache's own objects (never mutate --
         see the _RECORDS_CACHE ALIASING SAFETY note).
      2. On a miss, if the file is append-physical: the id->offset
         sidecar (_load_oidx). Answers in O(1) without ever folding the
         file -- this is the whole point of the sidecar (docs/append-
         only-storage-design.md item 4): a collection too large for
         _RECORDS_CACHE no longer pays an O(n) fold on every write.
         Trusted only when _load_oidx reports `coherent` -- otherwise
         (a torn tail at EOF from an unfinished write) this falls all the
         way through to step 3 rather than risk a wrong answer.
         records_ref/id_index_ref are None in this case: the fast-append
         write path this enables never needs the full record list (see
         _persist_write's folded_records_fn, which forces its own full
         fold via _cache_entry lazily, only if a rare full-rewrite path
         is chosen instead of a fast append).
      3. _cache_entry -- the original full fold, unchanged, used for
         classic-mode files, a genuinely cold/small collection's first
         touch, and any sidecar-inconclusive case above.

    fields is always the LOGICAL field list (no `_op`). `existing_row`,
    when requested via need_row and the id exists, is always a fresh,
    independent copy safe to hand across the public API or mutate.
    """
    cached = _peek_records_cache(path)
    if cached is not None:
        fields, records_ref, id_index_ref = cached
        index = id_index_ref.get(record_id)
        existing_row = dict(records_ref[index]) if (need_row and index is not None) else None
        return fields, index is not None, existing_row, records_ref, id_index_ref

    physical_header = _physical_header(path)
    if physical_header and physical_header[0] == OP_FIELD:
        id_offsets, coherent = _load_oidx(path)
        if coherent:
            offset = id_offsets.get(record_id)
            if offset is None:
                return physical_header[1:], False, None, None, None
            if not need_row:
                return physical_header[1:], True, None, None, None
            row = _oidx_get_row(path, physical_header, offset)
            if row is not None:
                return physical_header[1:], True, row, None, None
            # Row unreadable at its recorded offset despite a coherent
            # sidecar -- shouldn't happen; be conservative and fall
            # through to the authoritative full fold below.

    fields, records_ref, id_index_ref = _cache_entry(collection, path)
    index = id_index_ref.get(record_id)
    existing_row = dict(records_ref[index]) if (need_row and index is not None) else None
    return fields, index is not None, existing_row, records_ref, id_index_ref


def _collection_storage_mode(
    collection: str,
    *,
    base_dir: Path | str,
    roots: Iterable[Path] | None,
) -> str:
    """Return a collection's storage mode from its schema's `storage` key.

    Defaults to classic when the schema has no `storage` key, is derived
    (no manual schema file), or doesn't exist yet -- see
    object_schemas.normalize_schema, which validates the key at schema-
    write time, so any value reaching here is already known-good, but this
    stays defensive rather than trusting that unconditionally.
    """
    try:
        schema = object_schemas.get_schema(collection, base_dir=base_dir, roots=roots)
    except object_schemas.SchemaNotFoundError:
        return object_schemas.STORAGE_CLASSIC
    mode = schema.get("storage", object_schemas.STORAGE_CLASSIC)
    return mode if mode in object_schemas.VALID_STORAGE_MODES else object_schemas.STORAGE_CLASSIC


def _persist_write(
    collection: str,
    path: Path,
    *,
    base_dir: Path | str,
    roots: Iterable[Path] | None,
    prior_fields: list[str],
    merged_fields: list[str],
    folded_records_fn,
    delta_row: dict[str, str] | None,
    delta_op: str,
    delta_id: str,
) -> None:
    """Persist one create/update/delete, choosing the write strategy.

    `folded_records_fn` is a zero-argument callable that builds the FULL
    folded record set after this op (a fresh, safe-to-mutate copy -- see
    the callers in create/update/delete_collection_record). It is called
    LAZILY, only on a full-rewrite path, and never on the fast-append
    path: the whole point of a fast append is O(1) work, and building
    that full copy is an O(n) cost callers can otherwise skip entirely
    (see _refresh_records_cache_after_append, which updates the cache
    from its own prior entry plus this one delta instead). `delta_row`/
    `delta_op`/`delta_id` describe just this one op's row, used only by
    the fast-append path. Chooses among (see
    docs/append-only-storage-design.md and the Decisions this module's
    implementation is bound by):

      - FAST APPEND: storage is "append", the file is already physically
        in append format, this write doesn't introduce a field the
        current header lacks, and no auto-compaction is pending for this
        collection -- append one row, then update whichever of
        _RECORDS_CACHE / the id->offset sidecar (docs/append-only-
        storage-design.md item 4) is already warm for this path, in O(1),
        via _refresh_records_cache_after_append / _update_oidx_after_
        append. Neither is FORCED warm here: a collection too large for
        _RECORDS_CACHE (the case the sidecar exists for) is left
        uncached by design, and a sidecar nobody has read through yet is
        left unbuilt (see _update_oidx_after_append) -- either would mean
        paying the exact O(n) fold-per-write cost this branch exists to
        avoid, just moved from "on every write" to "on every write that
        happens to miss the cache," which is most of them for a
        write-hot, over-threshold collection.
      - TRANSITION-IN: storage is "append" but the file isn't (yet) in
        append format (including a brand new collection) -- one full
        rewrite that adds the `_op` header column, applying this op in
        the same pass.
      - NEW-FIELD FALLBACK / AUTO-COMPACT: storage is "append", the file
        is already append-format, but this write needs a header column
        the file doesn't have yet, or a prior read flagged this
        collection for compaction -- a full rewrite that folds current
        content, applies this op, and (in the new-field case) extends the
        header. Still `_op`-columned.
      - COMPACT-TO-CLASSIC: storage is "classic" but the file is still
        physically in append format (opt-out just happened) -- one full
        rewrite that folds current content, applies this op, and drops
        the `_op` column.
      - CLASSIC: storage is "classic" and the file already is (or never
        was anything but) classic format -- the original full-rewrite
        behavior, completely unchanged.
    """
    desired_mode = _collection_storage_mode(collection, base_dir=base_dir, roots=roots)
    physical_header = _physical_header(path)
    file_is_append_physical = bool(physical_header) and physical_header[0] == OP_FIELD
    cache_key = str(path.resolve(strict=False))
    new_field_introduced = merged_fields != prior_fields
    pending_compaction = cache_key in _PENDING_COMPACTION

    if (
        desired_mode == object_schemas.STORAGE_APPEND
        and file_is_append_physical
        and not new_field_introduced
        and not pending_compaction
    ):
        physical_fields = [OP_FIELD, *merged_fields]
        row = dict(delta_row) if delta_row is not None else {}
        row["id"] = delta_id
        row[OP_FIELD] = delta_op
        offsets = _append_records_rows(path, physical_fields, [row])
        projected_row = _project_record(row, merged_fields)
        _refresh_records_cache_after_append(
            collection, path, merged_fields, delta_op, delta_id, projected_row
        )
        _update_oidx_after_append(path, [row], offsets)
        return

    if file_is_append_physical:
        _discard_oidx(path)
    # folded_records_fn() is called BEFORE the discard below (not after,
    # as it may look more natural) because, on the sidecar-sourced lookup
    # path, it can itself force a fresh full fold (_cache_entry ->
    # _parse_records_file), which re-evaluates and can re-set auto-compact
    # flagging for this same cache_key from the PRE-write content. This
    # write is about to make that stale regardless of what it flagged
    # (append: compacting; classic/transition: rewriting from scratch
    # either way), so the discard must be the last word.
    folded_records = folded_records_fn()
    _PENDING_COMPACTION.discard(cache_key)
    if desired_mode == object_schemas.STORAGE_APPEND:
        physical_fields = [OP_FIELD, *merged_fields]
        _write_collection_records(
            collection, path, physical_fields, folded_records, cache_fields=merged_fields
        )
    else:
        _write_collection_records(collection, path, merged_fields, folded_records)


@contextmanager
def _records_file_lock(records_file: Path):
    """Use a best-effort advisory lock for collection record mutations."""
    lock_path = records_file.with_name(f".{records_file.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a") as lock_file:
        try:
            import fcntl
        except ImportError:
            fcntl = None

        if fcntl is not None:
            fcntl.flock(lock_file, fcntl.LOCK_EX)

        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


def _validate_header(collection: str, fields: list[str] | None) -> None:
    if not fields:
        raise ValueError(f"Collection records file is missing a header: {collection}")

    clean = [field.strip() for field in fields]
    if any(not field for field in clean):
        raise ValueError(f"Collection records file has an empty field name: {collection}")
    if clean != fields:
        raise ValueError(f"Collection records file has whitespace in field names: {collection}")
    if len(set(clean)) != len(clean):
        raise ValueError(f"Collection records file has duplicate field names: {collection}")
    if "id" not in clean:
        raise ValueError(f"Collection records file must include an id column: {collection}")


def _normalize_record_payload(payload: dict[str, Any], *, require_id: bool) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise InvalidRecordPayloadError("Record payload must be an object")
    generate_id = require_id and (
        "id" not in payload
        or payload.get("id") is None
        or payload.get("id") == ""
    )

    clean: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not key:
            raise InvalidRecordPayloadError("Record field names must be non-empty strings")
        if key.strip() != key:
            raise InvalidRecordPayloadError(f"Record field name has whitespace: {key!r}")
        if "\t" in key or "\n" in key or "\r" in key:
            raise InvalidRecordPayloadError(f"Record field name is not TSV-safe: {key!r}")
        if key == OP_FIELD:
            raise InvalidRecordPayloadError(
                f"Record field name is reserved for internal storage use: {key!r}"
            )
        if key == EXTRA_FIELD:
            clean[key] = _normalize_extra_value(value)
            continue
        if value is None:
            clean[key] = ""
        elif isinstance(value, (str, int, float, bool)):
            clean[key] = str(value)
        else:
            raise InvalidRecordPayloadError(
                f"Record field value must be scalar or null: {key}"
            )

    if generate_id:
        clean["id"] = object_ids.new_uuid4()
    elif require_id and "id" not in clean:
        raise InvalidRecordPayloadError("Record payload must include an id")

    return clean


def _normalize_extra_value(value: Any) -> str:
    """Return a TSV-safe JSON-object string for the ``extra`` field.

    Unlike other record fields (scalar or null only), ``extra`` may be
    submitted as a dict (serialized here) or as a JSON string that must
    already parse to an object. Anything else is rejected.
    """
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise InvalidRecordPayloadError(
                "Record field 'extra' must be a JSON object"
            ) from exc
        if not isinstance(parsed, dict):
            raise InvalidRecordPayloadError("Record field 'extra' must be a JSON object")
        return value
    raise InvalidRecordPayloadError(
        "Record field 'extra' must be an object or a JSON object string"
    )


def _apply_auto_created_at(
    collection: str,
    record: dict[str, str],
    submitted_fields: frozenset[str],
    *,
    base_dir: Path | str,
    roots: Iterable[Path] | None,
) -> dict[str, str]:
    """Server-set ``created_at`` on create when the schema declares it.

    A schema field named ``created_at`` (date/datetime) is filled with the
    current UTC time on create, unless the client supplied it. Because the
    server fills it (not the client), it can be ``read_only`` — clients can
    neither omit it nor spoof it — which is what lets generated lists show
    a trustworthy relative timestamp.
    """
    if "created_at" in submitted_fields:
        return record
    for field in _schema_fields(collection, base_dir=base_dir, roots=roots):
        if field.get("name") != "created_at":
            continue
        field_type = str(field.get("type") or "").lower()
        now = datetime.now(timezone.utc)
        if field_type == "date":
            value = now.date().isoformat()
        elif field_type in {"datetime", "timestamp"}:
            value = now.isoformat().replace("+00:00", "Z")
        else:
            return record
        clean = dict(record)
        clean["created_at"] = value
        return clean
    return record


def _apply_schema_defaults(
    collection: str,
    record: dict[str, str],
    *,
    base_dir: Path | str,
    roots: Iterable[Path] | None,
) -> dict[str, str]:
    fields = _schema_fields(collection, base_dir=base_dir, roots=roots)
    if not fields:
        return record

    clean = dict(record)
    for field in fields:
        name = field["name"]
        if name in clean and clean[name] != "":
            continue
        if _is_computed_or_read_only(field):
            continue
        if "default" not in field:
            continue
        clean[name] = _schema_scalar_to_string(field["default"], field_name=name)
    return clean


def _validate_field_transitions(
    collection: str,
    existing: dict[str, str],
    updated: dict[str, str],
    *,
    base_dir: Path | str,
    roots: Iterable[Path] | None,
    subject: object_permissions.PermissionSubject | None = None,
) -> None:
    """Enforce declared value transitions on update.

    A field may declare which values each current value can move to. Each
    list entry is either a plain string, allowed for any caller who may
    update the record, or a guarded object:

        {"name": "status", "type": "enum", "enum": [...],
         "transitions": {
             "open": ["assigned", {"to": "cancelled", "when": {"owner_id": "$user_id"}}],
             ...
         }}

    A guarded move is allowed only once every ``when`` clause matches the
    record's CURRENT stored values (``existing``, before this update)
    against the resolved subject variable or literal -- the same closed
    set and matching rules row filters use (see
    object_permissions.record_matches_filter).

    This is deliberately data plus one check — not a state machine
    framework: no hooks, no side effects, no transition callbacks. A
    current value missing from the map cannot change; an empty existing
    value may move anywhere.

    Guards are enforced only when ``subject`` is supplied. The HTTP update
    path resolves and plumbs the request subject once permissions have
    already run (see update_collection_record); direct library callers
    with no subject -- daemon, CLI, tests -- are trusted callers: a
    guarded move is still checked for validity (its "to" is in the list)
    but the "when" clause is not enforced.
    """
    fields = _schema_fields(collection, base_dir=base_dir, roots=roots)
    for field in fields:
        transitions = field.get("transitions")
        if not isinstance(transitions, dict):
            continue
        name = field["name"]
        old_value = existing.get(name, "")
        new_value = updated.get(name, "")
        if old_value == new_value or _is_empty(old_value):
            continue

        entries = transitions.get(old_value)
        entries = entries if isinstance(entries, list) else []

        allowed_values: list[str] = []
        move_is_valid = False
        guard_failed = False
        for entry in entries:
            if isinstance(entry, str):
                to_value, when = entry, None
            elif isinstance(entry, dict):
                to_value, when = str(entry.get("to", "")), entry.get("when") or {}
            else:
                continue

            if to_value and to_value not in allowed_values:
                allowed_values.append(to_value)
            if to_value != new_value:
                continue

            if when is None or subject is None:
                move_is_valid = True
            elif object_permissions.record_matches_filter(existing, when, subject):
                move_is_valid = True
            else:
                guard_failed = True

        if move_is_valid:
            continue

        options = ", ".join(allowed_values) if allowed_values else "none"
        if guard_failed:
            raise TransitionNotAllowedError(
                f"Record field '{name}' cannot move from '{old_value}' to "
                f"'{new_value}' for this subject (allowed: {options})"
            )
        raise InvalidRecordPayloadError(
            f"Record field '{name}' cannot move from '{old_value}' to "
            f"'{new_value}' (allowed: {options})"
        )


def _canonicalize_schema_values(
    collection: str,
    record: dict[str, str],
    *,
    base_dir: Path | str,
    roots: Iterable[Path] | None,
) -> dict[str, str]:
    """Store schema-typed values in one canonical form.

    Boolean fields accept several spellings on input ("True", "1", "yes")
    but must be stored as "true"/"false" so permission row filters like
    {"is_public": "true"} match by string comparison. Runs after
    validation, so every value here is known to parse.
    """
    fields = _schema_fields(collection, base_dir=base_dir, roots=roots)
    if not fields:
        return record

    clean = dict(record)
    for field in fields:
        name = field["name"]
        value = clean.get(name)
        if _is_empty(value):
            continue
        field_type = str(field.get("type") or "text").lower()
        if field_type in _BOOLEAN_TYPES:
            clean[name] = "true" if _parse_boolean(value, field_name=name) else "false"
    return clean


def _validate_record_against_schema(
    collection: str,
    record: dict[str, str],
    *,
    submitted_fields: frozenset[str],
    base_dir: Path | str,
    roots: Iterable[Path] | None,
    allow_read_only_submission: bool = False,
) -> None:
    fields = _schema_fields(collection, base_dir=base_dir, roots=roots)
    if not fields:
        return

    for field in fields:
        name = field["name"]
        value = record.get(name, "")

        if name in submitted_fields and _is_computed_or_read_only(field):
            if not (allow_read_only_submission and not _is_computed_field(field)):
                raise InvalidRecordPayloadError(
                    f"Record field '{name}' is computed or read-only and cannot be written"
                )

        if _field_is_required(field) and not _is_computed_or_read_only(field) and _is_empty(value):
            raise InvalidRecordPayloadError(f"Record field '{name}' is required")

        if _is_empty(value):
            continue

        _validate_field_type(field, value)
        _validate_field_enum(field, value)
        _validate_field_rules(field, value)
        _validate_field_relation(field, value, base_dir=base_dir)


def _check_field_storable(record: dict[str, Any]) -> None:
    """Reject a write whose any field value cannot be safely stored in the
    canonical plain-text TSV substrate, surfacing every case as a clean
    InvalidRecordPayloadError (a 4xx) rather than letting a raw stdlib
    _csv.Error or UnicodeEncodeError escape the public API. Three unstorable
    classes:

    - **Too large** (> MAX_TSV_FIELD_BYTES): without this, an oversize cell
      silently empties an append-mode collection on read.
    - **NUL (0x00)**: Python's csv writer literally cannot represent a NUL
      under QUOTE_ALL/MINIMAL ("need to escape, but no escapechar set"), and a
      NUL corrupts the text tooling the plain-text-durability guarantee rests
      on -- it is not text. Legitimate JSON escapes NUL to \\u0000, so real
      structured data never carries a raw one.
    - **Unencodable UTF-8** (a lone surrogate, e.g. from bad input or
      json.dumps(ensure_ascii=False) over broken data): can't be written at
      all.

    Enforced on create/update (the entry points for new/changed data);
    internal rewrites (compaction, transition) don't pass here -- they only
    re-serialize data that already passed this gate. Iterates the record's own
    values, so it guards schemaless collections and the packed `extra` blob
    too.
    """
    for name, value in record.items():
        if value is None:
            continue
        text = str(value)
        if "\x00" in text:
            raise InvalidRecordPayloadError(
                f"Record field '{name}' contains a NUL byte, which cannot be "
                f"stored in the plain-text substrate (JSON encodes NUL as "
                f"\\u0000)"
            )
        try:
            size = len(text.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise InvalidRecordPayloadError(
                f"Record field '{name}' is not valid UTF-8 (unencodable "
                f"surrogate) and cannot be stored: {exc}"
            ) from exc
        if size > MAX_TSV_FIELD_BYTES:
            raise InvalidRecordPayloadError(
                f"Record field '{name}' is {size} bytes, exceeding the "
                f"{MAX_TSV_FIELD_BYTES}-byte per-field maximum"
            )


# A bare carriage return -- "\r" NOT immediately followed by "\n". csv's
# QUOTE_MINIMAL does not quote a field for a lone CR (it quotes only for the
# delimiter, the quotechar, and "\n"), but csv.reader treats a bare CR in an
# UNQUOTED field as a row terminator -- silently splitting one row into two
# (and, in append mode, hiding every later record from the fold path). "\r\n"
# and lone "\n" are already handled by QUOTE_MINIMAL (both contain "\n").
_BARE_CR_RE = re.compile(r"\r(?!\n)")


def _rows_need_full_quoting(rows: Iterable[dict[str, Any]]) -> bool:
    """True if any field value in `rows` contains a bare CR (see _BARE_CR_RE).

    When it does, that write uses csv.QUOTE_ALL so every field is quoted and
    the CR round-trips intact; the overwhelming common case (no bare CR) stays
    QUOTE_MINIMAL and byte-identical to before. This keeps the compact
    plain-text format for normal data while making a lone CR lossless instead
    of silently corrupting -- no format change for anyone who never stores a
    bare CR. Readers parse quoted and unquoted rows alike, so a file may mix
    both. Note: values destined for the packed `extra` JSON blob have their CR
    escaped by json.dumps, so scanning raw values here only ever OVER-detects
    (a harmless extra-quoted write), never misses a CR that reaches a column.
    """
    for row in rows:
        for value in row.values():
            if value is None:
                continue
            text = str(value)
            if "\r" in text and _BARE_CR_RE.search(text):
                return True
    return False


def _schema_fields(
    collection: str,
    *,
    base_dir: Path | str,
    roots: Iterable[Path] | None,
) -> list[dict[str, Any]]:
    try:
        schema = object_schemas.get_schema(collection, base_dir=base_dir, roots=roots)
    except object_schemas.SchemaNotFoundError:
        return []
    fields = schema.get("fields", [])
    if not isinstance(fields, list):
        return []
    return [field for field in fields if isinstance(field, dict) and "name" in field]


def _field_is_required(field: dict[str, Any]) -> bool:
    validation = field.get("validation") if isinstance(field.get("validation"), dict) else {}
    return bool(field.get("required") or validation.get("required") or validation.get("not_null"))


def _is_computed_or_read_only(field: dict[str, Any]) -> bool:
    field_type = str(field.get("type", "")).lower()
    return bool(
        field_type == "computed"
        or field.get("computed")
        or field.get("read_only")
        or field.get("readonly")
        or field.get("readOnly")
    )


def _is_computed_field(field: dict[str, Any]) -> bool:
    """Return True for a server-derived formula field, as opposed to a
    field that is merely ``read_only`` (client can't set it, but nothing
    computes its value -- see ``preserve_read_only`` on
    ``create_collection_record``)."""
    field_type = str(field.get("type", "")).lower()
    return bool(field_type == "computed" or field.get("computed"))


def _is_empty(value: str | None) -> bool:
    return value is None or value == ""


def _schema_scalar_to_string(value: Any, *, field_name: str) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    raise InvalidRecordPayloadError(f"Schema default for '{field_name}' must be scalar or null")


def _validate_field_type(field: dict[str, Any], value: str) -> None:
    name = field["name"]
    field_type = str(field.get("type") or "text").lower()
    if field_type in _INTEGER_TYPES:
        _parse_integer(value, field_name=name)
    elif field_type in _FLOAT_TYPES:
        _parse_float(value, field_name=name)
    elif field_type in _BOOLEAN_TYPES:
        _parse_boolean(value, field_name=name)
    elif field_type == "date":
        _parse_date(value, field_name=name)
    elif field_type in {"datetime", "timestamp"}:
        _parse_datetime(value, field_name=name)
    elif field_type == "enum":
        _validate_field_enum(field, value, required=True)


def _validate_field_enum(field: dict[str, Any], value: str, *, required: bool = False) -> None:
    enum_payload = field.get("enum")
    validation = field.get("validation") if isinstance(field.get("validation"), dict) else {}
    if enum_payload is None:
        enum_payload = validation.get("choices", validation.get("in"))
    values = _enum_values(enum_payload)
    if not values:
        if required:
            raise InvalidRecordPayloadError(f"Record field '{field['name']}' enum has no values")
        return
    if value not in values:
        allowed = ", ".join(values)
        raise InvalidRecordPayloadError(
            f"Record field '{field['name']}' must be one of: {allowed}"
        )


def _enum_values(enum_payload: Any) -> list[str]:
    if enum_payload is None:
        return []
    if isinstance(enum_payload, dict):
        enum_payload = enum_payload.get("values")
    if not isinstance(enum_payload, list):
        return []
    values = []
    for item in enum_payload:
        if isinstance(item, (str, int, float, bool)):
            values.append(str(item))
    return values


def _validate_field_relation(
    field: dict[str, Any],
    value: str,
    *,
    base_dir: Path | str,
) -> None:
    """Require relation values to be existing record ids in the target collection.

    A relation is a validated pointer plus a display hint — deliberately not
    an association framework: no joins, no lazy loading, no cascades.
    """
    relation = field.get("relation")
    if relation is None:
        return

    name = field["name"]
    if isinstance(relation, str):
        target = relation
    elif isinstance(relation, dict):
        target = relation.get("collection")
    else:
        raise InvalidRecordPayloadError(
            f"Record field '{name}' relation must be a collection name or object"
        )

    if not isinstance(target, str) or not object_collections.validate_collection_name(target):
        raise InvalidRecordPayloadError(
            f"Record field '{name}' relation has an invalid collection"
        )

    try:
        get_collection_record(target, value, base_dir=base_dir)
    except (object_collections.CollectionNotFoundError, RecordNotFoundError) as exc:
        raise InvalidRecordPayloadError(
            f"Record field '{name}' references a missing record: {target}/{value}"
        ) from exc
    except InvalidRecordIdError as exc:
        raise InvalidRecordPayloadError(
            f"Record field '{name}' relation value is not a valid record id"
        ) from exc


def _validate_field_rules(field: dict[str, Any], value: str) -> None:
    validation = field.get("validation")
    if not isinstance(validation, dict):
        return

    name = field["name"]
    if "min_length" in validation and len(value) < _rule_int(validation["min_length"], name, "min_length"):
        raise InvalidRecordPayloadError(f"Record field '{name}' is shorter than min_length")
    if "max_length" in validation and len(value) > _rule_int(validation["max_length"], name, "max_length"):
        raise InvalidRecordPayloadError(f"Record field '{name}' is longer than max_length")

    pattern = validation.get("pattern", validation.get("regex"))
    if pattern is not None:
        if not isinstance(pattern, str):
            raise InvalidRecordPayloadError(f"Record field '{name}' validation pattern must be a string")
        try:
            matches = re.fullmatch(pattern, value)
        except re.error as exc:
            raise InvalidRecordPayloadError(
                f"Record field '{name}' validation pattern is invalid"
            ) from exc
        if not matches:
            raise InvalidRecordPayloadError(f"Record field '{name}' does not match pattern")

    if "min" in validation:
        if _parse_float(value, field_name=name) < _rule_float(validation["min"], name, "min"):
            raise InvalidRecordPayloadError(f"Record field '{name}' is below min")
    if "max" in validation:
        if _parse_float(value, field_name=name) > _rule_float(validation["max"], name, "max"):
            raise InvalidRecordPayloadError(f"Record field '{name}' is above max")


def _parse_integer(value: str, *, field_name: str) -> int:
    try:
        if value.strip() != value or not re.fullmatch(r"[+-]?\d+", value):
            raise ValueError
        return int(value)
    except ValueError as exc:
        raise InvalidRecordPayloadError(f"Record field '{field_name}' must be an integer") from exc


def _parse_float(value: str, *, field_name: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise InvalidRecordPayloadError(f"Record field '{field_name}' must be a number") from exc
    if not math.isfinite(number):
        raise InvalidRecordPayloadError(f"Record field '{field_name}' must be a finite number")
    return number


def _parse_boolean(value: str, *, field_name: str) -> bool:
    text = value.lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    raise InvalidRecordPayloadError(f"Record field '{field_name}' must be a boolean")


def _parse_date(value: str, *, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidRecordPayloadError(f"Record field '{field_name}' must be a date") from exc


def _parse_datetime(value: str, *, field_name: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidRecordPayloadError(f"Record field '{field_name}' must be a datetime") from exc


def _rule_int(value: Any, field_name: str, rule_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidRecordPayloadError(
            f"Record field '{field_name}' validation {rule_name} must be an integer"
        ) from exc


def _rule_float(value: Any, field_name: str, rule_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidRecordPayloadError(
            f"Record field '{field_name}' validation {rule_name} must be a number"
        ) from exc
    if not math.isfinite(number):
        raise InvalidRecordPayloadError(
            f"Record field '{field_name}' validation {rule_name} must be finite"
        )
    return number


def _extra_field_names(
    collection: str,
    *,
    base_dir: Path | str,
    roots: Iterable[Path] | None,
) -> set[str]:
    """Return the names of schema fields whose value lives inside ``extra``."""
    fields = _schema_fields(collection, base_dir=base_dir, roots=roots)
    return {field["name"] for field in fields if field.get("store") == EXTRA_FIELD}


def _parse_extra_blob(value: str | None) -> dict[str, Any]:
    """Parse an ``extra`` TSV cell into a dict, tolerating missing/invalid data."""
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _route_extra(
    record: dict[str, str],
    *,
    existing_blob: dict[str, Any],
    extra_names: set[str],
) -> dict[str, str]:
    """Fold ``extra`` and any ``store: extra`` view fields into one JSON blob.

    Pure: builds on ``existing_blob`` (so a partial update never wipes
    untouched blob keys), shallow-merges any client-submitted ``extra``
    object into it, then pulls each declared view field out of the
    top-level record and into the blob. The record keeps a single
    ``extra`` TSV column holding the serialized blob (or no column at all
    when the blob is empty), never the view field's own column.
    """
    blob = dict(existing_blob)
    if EXTRA_FIELD in record:
        blob.update(_parse_extra_blob(record.pop(EXTRA_FIELD)))
    for name in extra_names:
        if name in record:
            blob[name] = record.pop(name)

    if blob:
        serialized = json.dumps(blob, sort_keys=True)
        if "\t" in serialized or "\n" in serialized or "\r" in serialized:
            raise InvalidRecordPayloadError("Record field 'extra' is not TSV-safe")
        record[EXTRA_FIELD] = serialized
    else:
        record.pop(EXTRA_FIELD, None)
    return record


def _surface_extra(record: dict[str, str], *, extra_names: set[str]) -> dict[str, str]:
    """Expose each declared ``store: extra`` view field at the top level.

    Pure: ``record["extra"]`` is left as-is (still the raw JSON string) so
    undeclared blob data stays reachable. A no-op when there are no view
    fields and no ``extra`` column.
    """
    if not extra_names:
        return record
    blob = _parse_extra_blob(record.get(EXTRA_FIELD))
    for name in extra_names:
        record[name] = str(blob.get(name, ""))
    return record


def _merge_fields(existing_fields: list[str], record: dict[str, str]) -> list[str]:
    fields = list(existing_fields) if existing_fields else ["id"]
    if "id" not in fields:
        fields.insert(0, "id")
    for field in record:
        if field not in fields:
            fields.append(field)
    return fields


def _project_record(record: dict[str, str], fields: list[str]) -> dict[str, str]:
    return {field: record.get(field, "") for field in fields}


def _validate_page(*, limit: int, offset: int) -> None:
    if limit < 1:
        raise ValueError("Record limit must be at least 1")
    if limit > MAX_RECORD_LIMIT:
        raise ValueError(f"Record limit must be at most {MAX_RECORD_LIMIT}")
    if offset < 0:
        raise ValueError("Record offset must be at least 0")
