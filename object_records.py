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
from typing import Any, Iterable

import object_collections
import object_ids
import object_schemas
from object_versions import DEFAULT_DATA_DIR

COLLECTIONS_DIR = "collections"
RECORDS_FILE = "records.tsv"
DEFAULT_RECORD_LIMIT = 100
MAX_RECORD_LIMIT = 1000
EXTRA_FIELD = "extra"

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
# Value is (stat_signature, fields, records, id_index):
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
_RECORDS_CACHE: "OrderedDict[str, tuple[tuple[int, int, int], list[str], list[dict[str, str]], dict[str, int]]]" = OrderedDict()

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
) -> dict[str, Any]:
    """Return a paginated record list for one collection.

    Copies only the window (records[offset:offset + limit]) rather than
    the whole collection first: callers of a paginated list only ever
    consume a small page, and copying every record before slicing it away
    (the straightforward implementation) dominates cost on large
    collections for a window that is thrown away 99% copied. `total` is
    computed from the cache's own record count, not from the copied
    window.
    """
    _ensure_collection_known(collection, base_dir=base_dir, roots=roots)
    _validate_page(limit=limit, offset=offset)

    records_ref = _cached_records_ref(collection, base_dir=base_dir)
    total = len(records_ref)
    extra_names = _extra_field_names(collection, base_dir=base_dir, roots=roots)
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
    _, records, id_index = _cache_entry(collection, path)
    index = id_index.get(record_id)
    if index is None:
        raise RecordNotFoundError(f"Record not found: {collection}/{record_id}")

    # Copy before handing it out: `records[index]` is the module cache's
    # own dict, and _surface_extra mutates its argument in place.
    return _surface_extra(dict(records[index]), extra_names=extra_names)


def create_collection_record(
    collection: str,
    record: dict[str, Any],
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
) -> dict[str, str]:
    """Append one record to a collection TSV and return the stored row."""
    _ensure_collection_known(collection, base_dir=base_dir, roots=roots)
    extra_names = _extra_field_names(collection, base_dir=base_dir, roots=roots)
    clean = _normalize_record_payload(record, require_id=True)
    record_id = clean["id"]
    if not validate_record_id(record_id):
        raise InvalidRecordIdError(f"Invalid record id: {record_id}")
    submitted_fields = frozenset(clean)
    clean = _apply_schema_defaults(collection, clean, base_dir=base_dir, roots=roots)
    clean = _apply_auto_created_at(collection, clean, submitted_fields, base_dir=base_dir, roots=roots)
    _validate_record_against_schema(
        collection,
        clean,
        submitted_fields=submitted_fields,
        base_dir=base_dir,
        roots=roots,
    )
    clean = _canonicalize_schema_values(collection, clean, base_dir=base_dir, roots=roots)
    clean = _route_extra(clean, existing_blob={}, extra_names=extra_names)

    path = collection_records_file(collection, base_dir=base_dir)
    with _records_file_lock(path):
        # Uncopied cache references (see the ALIASING SAFETY note on
        # _RECORDS_CACHE): fine here because neither is mutated -- only
        # read (the O(1) duplicate check) or captured in a closure that
        # builds a fresh copy lazily, only if a full rewrite turns out to
        # be needed (see _persist_write's folded_records_fn). Skipping an
        # eager copy of every existing record is what keeps a fast-append
        # create O(1) rather than O(n): an `any(row.get("id") == ... for
        # row in records)` linear scan plus a full deep-copy of every
        # existing record were, together, the dominant cost of a "fast"
        # append in an earlier version of this function.
        fields, records_ref, id_index_ref = _cache_entry(collection, path)
        # Duplicate check against the folded record set: in append mode
        # `id_index_ref` is built from the last-wins-by-id folded records
        # (a deleted id is absent, so a create can reuse it --
        # resurrection), same as classic mode's index, which never
        # indexes a stale/removed row.
        if record_id in id_index_ref:
            raise DuplicateRecordIdError(f"Record already exists: {collection}/{record_id}")

        merged_fields = _merge_fields(fields, clean)

        def build_folded_records() -> list[dict[str, str]]:
            records = [dict(existing) for existing in records_ref]
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
        return _surface_extra(_project_record(clean, merged_fields), extra_names=extra_names)


def update_collection_record(
    collection: str,
    record_id: str,
    changes: dict[str, Any],
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
) -> dict[str, str]:
    """Update one existing record by id and return the stored row."""
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
        # See the matching comment in create_collection_record: uncopied
        # cache references, read-only here, used for an O(1) lookup and a
        # lazily-built copy (only made if a full rewrite is actually
        # needed). id_index_ref is first-occurrence-wins (_build_id_index),
        # same as the linear scan this replaces, so this changes nothing
        # about WHICH row a duplicate-id file resolves to.
        fields, records_ref, id_index_ref = _cache_entry(collection, path)
        index = id_index_ref.get(record_id)
        if index is None:
            raise RecordNotFoundError(f"Record not found: {collection}/{record_id}")

        raw_existing = records_ref[index]
        existing_blob = _parse_extra_blob(raw_existing.get(EXTRA_FIELD))
        existing = _surface_extra(dict(raw_existing), extra_names=extra_names)

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
        _validate_field_transitions(
            collection,
            existing,
            updated,
            base_dir=base_dir,
            roots=roots,
        )
        updated = _canonicalize_schema_values(
            collection, updated, base_dir=base_dir, roots=roots
        )
        updated = _route_extra(updated, existing_blob=existing_blob, extra_names=extra_names)
        merged_fields = _merge_fields(fields, updated)

        def build_folded_records() -> list[dict[str, str]]:
            records = [dict(existing_row) for existing_row in records_ref]
            records[index] = updated
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
        return _surface_extra(_project_record(updated, merged_fields), extra_names=extra_names)


def delete_collection_record(
    collection: str,
    record_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
) -> dict[str, str]:
    """Delete one existing record by id and return the removed row."""
    _ensure_collection_known(collection, base_dir=base_dir, roots=roots)
    if not validate_record_id(record_id):
        raise InvalidRecordIdError(f"Invalid record id: {record_id}")

    path = collection_records_file(collection, base_dir=base_dir)
    with _records_file_lock(path):
        # See the matching comment in create_collection_record.
        fields, records_ref, id_index_ref = _cache_entry(collection, path)
        index = id_index_ref.get(record_id)
        if index is None:
            raise RecordNotFoundError(f"Record not found: {collection}/{record_id}")

        removed = dict(records_ref[index])  # copy before handing back across the API

        def build_folded_records() -> list[dict[str, str]]:
            records = [dict(existing_row) for existing_row in records_ref]
            records.pop(index)
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
        return _project_record(removed, fields)


def compact_collection(
    collection: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Fold an append-mode collection's records file to its live rows only.

    Rewrites atomically via the same _write_collection_records path a
    plain write uses (temp file + replace), under the collection's file
    lock, keeping the `_op` column (every row becomes an upsert). A manual
    trigger for now -- no HTTP surface in this change (see
    docs/append-only-storage-design.md Compaction: "run on a schedule or
    when the superseded-row ratio passes a threshold — never inline in a
    request"). Auto-compaction (_maybe_flag_auto_compact) causes a
    collection's next ordinary write to perform this same rewrite instead
    of a plain append; this function can also be called directly at any
    time, including on a classic-mode or never-written collection, where
    it is a correctly-reported no-op.

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

        bytes_after = path.stat().st_size
        return {
            "rows_before": physical_row_count,
            "rows_after": len(folded_records),
            "bytes_before": bytes_before,
            "bytes_after": bytes_after,
        }


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


def _store_records_cache(
    cache_key: str,
    signature: tuple[int, int, int],
    fields: list[str],
    records: list[dict[str, str]],
    id_index: dict[str, int],
) -> None:
    """Insert/refresh one entry as most-recently-used, then evict over capacity.

    Eviction is pure LRU by entry count: once the dict exceeds
    DBBASIC_RECORDS_CACHE_MAX_ENTRIES, the oldest (least-recently-used)
    entries are dropped first via OrderedDict.popitem(last=False).
    """
    _RECORDS_CACHE[cache_key] = (signature, fields, records, id_index)
    _RECORDS_CACHE.move_to_end(cache_key)
    max_entries = _records_cache_max_entries()
    while len(_RECORDS_CACHE) > max_entries:
        _RECORDS_CACHE.popitem(last=False)


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
    """
    if not path.exists():
        return ["id"], [], {}
    if not path.is_file():
        raise ValueError(f"Collection records path is not a file: {collection}")

    cache_key = str(path.resolve(strict=False))
    signature = _stat_signature(path)
    if signature is not None:
        cached = _RECORDS_CACHE.get(cache_key)
        if cached is not None and cached[0] == signature:
            _RECORDS_CACHE.move_to_end(cache_key)
            return cached[1], cached[2], cached[3]

    fields, records = _parse_records_file(collection, path)
    id_index = _build_id_index(records)
    if signature is not None:
        if len(records) <= _records_cache_max_rows():
            _store_records_cache(cache_key, signature, fields, records, id_index)
        else:
            # Too large to cache: don't pin it, and drop any stale entry
            # left over from before this collection grew past the
            # threshold, so it doesn't linger under an old signature.
            _RECORDS_CACHE.pop(cache_key, None)
    return fields, records, id_index


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


def _drop_torn_tail(text: str) -> str:
    """Return `text` with any unterminated final physical line removed.

    Append-mode writers only ever consider a row committed once it is
    followed by "\\n" (see _append_records_rows / _repair_torn_tail): a
    physical line at EOF that isn't followed by "\\n" represents a write
    interrupted before it completed and must not be treated as data (the
    classic log torn-tail rule -- docs/append-only-storage-design.md Crash
    Safety). This is a plain text-level trim: it does not need to
    understand CSV quoting, because a completed row's own writer always
    terminates it with "\\n" regardless of what its quoted content
    contains, so "does the file end with \\n" is a reliable, cheap signal
    on its own.
    """
    if text == "" or text.endswith("\n"):
        return text
    cut = text.rfind("\n")
    return text[: cut + 1] if cut >= 0 else ""


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


def _append_compact_min_rows() -> int:
    return _env_int(APPEND_COMPACT_MIN_ROWS_ENV, _DEFAULT_APPEND_COMPACT_MIN_ROWS)


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
    _store_records_cache(cache_key, signature, list(fields), cached_records, id_index)


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

    Falls back (returns False) when there is no usable prior entry to
    build on -- cold cache, evicted, over the row-count cache threshold,
    or (defensively) a fields mismatch -- and the caller does a normal
    full _refresh_records_cache rebuild instead.
    """
    signature = _stat_signature(path)
    if signature is None:
        return True  # nothing to cache either way; nothing left to do

    cache_key = str(path.resolve(strict=False))
    prior = _RECORDS_CACHE.get(cache_key)
    if prior is None or prior[1] != fields:
        return False

    _, _prior_fields, old_records, old_id_index = prior
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

    _store_records_cache(cache_key, signature, list(fields), new_records, new_id_index)
    return True


def _repair_torn_tail(path: Path) -> None:
    """Ensure `path` ends with a complete row before an append lands.

    A write interrupted mid-row leaves a fragment after the last real
    "\\n". Merely appending a "\\n" after that fragment would make the
    byte stream *look* terminated while leaving the fragment's bytes in
    place as ordinary (wrong) data -- a later parse could then misread it
    as a legitimately short row and resurrect it. Instead, this finds the
    newline ending the last COMPLETE row and truncates everything after
    it, so the fragment can never resurface. After this call the file
    always ends with "\\n" (or is empty), so the append that follows never
    has to think about the torn-tail case itself.

    Scans backward from EOF in growing chunks (most torn fragments are a
    single short row, so this is normally one small read) rather than
    reading the whole file, since this runs on every append in append
    mode and must stay cheap even on a huge collection.
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
) -> None:
    """Append one or more pre-built physical rows to an append-format file.

    Each row in `rows` must already carry an `_op` key (OP_UPSERT or
    OP_DELETE). Self-heals a torn tail first (_repair_torn_tail) so the
    new row(s) always land as clean, complete physical lines, then writes
    via csv.writer (not DictWriter -- rows are already fully projected)
    on a text-mode "a" handle, flushing once at the end. Same dialect as
    _write_collection_records: tab-delimited, "\\n" line terminator,
    QUOTE_MINIMAL.
    """
    _repair_torn_tail(path)
    with path.open("a", newline="") as handle:
        writer = csv.writer(
            handle, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_MINIMAL
        )
        for row in rows:
            projected = _project_record(row, physical_fields)
            writer.writerow([projected[field] for field in physical_fields])
        handle.flush()


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
        collection -- append one row, then refresh the cache the same way
        a full rewrite would (store the folded content + new signature).
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
        _append_records_rows(path, physical_fields, [row])
        projected_row = _project_record(row, merged_fields)
        if not _refresh_records_cache_after_append(
            collection, path, merged_fields, delta_op, delta_id, projected_row
        ):
            _refresh_records_cache(collection, path, merged_fields, folded_records_fn())
        return

    _PENDING_COMPACTION.discard(cache_key)
    folded_records = folded_records_fn()
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
) -> None:
    """Enforce declared value transitions on update.

    A field may declare which values each current value can move to:

        {"name": "status", "type": "enum", "enum": [...],
         "transitions": {"open": ["assigned", "cancelled"], ...}}

    This is deliberately data plus one check — not a state machine
    framework: no hooks, no side effects, no transition callbacks. A
    current value missing from the map cannot change; an empty existing
    value may move anywhere.
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
        allowed = transitions.get(old_value)
        allowed_values = [str(item) for item in allowed] if isinstance(allowed, list) else []
        if new_value not in allowed_values:
            options = ", ".join(allowed_values) if allowed_values else "none"
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
) -> None:
    fields = _schema_fields(collection, base_dir=base_dir, roots=roots)
    if not fields:
        return

    for field in fields:
        name = field["name"]
        value = record.get(name, "")

        if name in submitted_fields and _is_computed_or_read_only(field):
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
