"""Read-only inventory and on-demand creation for runtime backups.

Backups are the tar/gzip archives written by ``object_backup`` under the
configured backups directory. This module lists them, creates a new
full-runtime backup on demand, and resolves a backup id to a safe path
for download. It is the data layer behind the admin backup endpoints.

A backup contains the whole runtime data directory — records, identity,
credentials, service keys — so the HTTP surface that uses this module is
strictly admin-gated. Nothing here is public.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import object_backup
import object_collections

MANUAL_LABEL = "manual"
_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,255}\.tar\.gz$")


def backups_dir(data_dir: Path | str | None = None) -> Path:
    """Return the configured backups directory (honors DBBASIC_BACKUPS_DIR)."""
    return object_backup._backups_dir(None, data_dir=data_dir)


def validate_backup_id(backup_id: str) -> bool:
    """Return True when a backup id is a safe archive filename (no traversal)."""
    if not isinstance(backup_id, str) or "/" in backup_id or "\\" in backup_id:
        return False
    return bool(_ID_RE.fullmatch(backup_id))


def backup_path(backup_id: str, *, data_dir: Path | str | None = None) -> Path:
    """Resolve a backup id to its path, refusing anything outside the dir."""
    if not validate_backup_id(backup_id):
        raise ValueError(f"invalid backup id: {backup_id!r}")
    root = backups_dir(data_dir).resolve(strict=False)
    path = (backups_dir(data_dir) / backup_id).resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("backup path escapes the backups directory") from exc
    return path


def list_backups(*, data_dir: Path | str | None = None) -> list[dict[str, object]]:
    """Return backup metadata, newest first — never the archive contents."""
    directory = backups_dir(data_dir)
    if not directory.is_dir():
        return []
    entries = [
        _entry(path)
        for path in directory.glob("*.tar.gz")
        if path.is_file() and validate_backup_id(path.name)
    ]
    entries.sort(key=lambda entry: entry["created_at"], reverse=True)
    return entries


def create_backup(*, data_dir: Path | str | None = None) -> dict[str, object]:
    """Create a full-runtime backup now and return its metadata."""
    summary = object_backup.create_runtime_restore_point(MANUAL_LABEL, data_dir=data_dir)
    return _entry(Path(summary.path))


def _entry(path: Path) -> dict[str, object]:
    stat = path.stat()
    name = path.name
    stem = name[: -len(".tar.gz")] if name.endswith(".tar.gz") else name
    parts = stem.split("-", 1)
    label = parts[1] if len(parts) == 2 else stem
    if label.startswith("package-"):
        kind, scope = "package", label[len("package-"):]
    elif label == MANUAL_LABEL:
        kind, scope = "manual", "runtime"
    else:
        kind, scope = "restore-point", label
    created_at = (
        datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    return {
        "id": name,
        "created_at": created_at,
        "size": stat.st_size,
        "kind": kind,
        "scope": scope,
    }


# Append-mode physical format (docs/storage-modes.md "Append"; the
# authoritative implementation is object_records.py's OP_FIELD/OP_UPSERT/
# OP_DELETE and _fold_append_rows). Kept as plain local constants rather
# than importing object_records's, so this module's read of a records.tsv
# stays independent of that module's private helpers.
_OP_FIELD = "_op"
_OP_DELETE = "del"


def _drop_torn_tail(text: str) -> str:
    """Drop an unterminated final physical line (a write caught mid-append).

    Mirrors object_records._drop_torn_tail: append-mode writers only
    consider a row committed once it is followed by "\\n" (see
    docs/append-only-storage-design.md, Crash Safety), so a trailing line
    with no newline is an in-flight write, not data, on either the backup
    or the live side.
    """
    if text == "" or text.endswith("\n"):
        return text
    cut = text.rfind("\n")
    return text[: cut + 1] if cut >= 0 else ""


def _fold_append_rows(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    """Fold physical append-mode rows into live records, last-wins per id.

    Minimal local replica of object_records._fold_append_rows's id
    semantics, for backup preview/diff purposes only (docs/storage-modes.md
    "Append"): a delete (`_op == "del"`) removes the id; any other row is
    an upsert that (re)sets it — so an id updated multiple times, or
    deleted and later re-created, ends up holding just its final live
    values. `_op` itself is stripped from every returned row. Unlike
    object_records's fold, this doesn't need to preserve list order (the
    callers here only ever look records up by id for a diff), so it
    returns a plain dict rather than an OrderedDict-backed list.
    """
    folded: dict[str, dict[str, str]] = {}
    for row in rows:
        record_id = row.get("id")
        if not record_id:
            continue
        if row.get(_OP_FIELD) == _OP_DELETE:
            folded.pop(record_id, None)
            continue
        folded[record_id] = {key: value for key, value in row.items() if key != _OP_FIELD}
    return folded


def _parse_append_tsv_by_id(text: str, header: list[str]) -> dict[str, dict[str, str]]:
    """Parse+fold an append-mode records.tsv body into a dict keyed by id.

    `header` is the already-inspected physical header (first column
    `_op`). A torn final line is dropped first (_drop_torn_tail); a row
    that still fails to tokenize stops consumption at that point rather
    than raising, matching object_records._parse_append_body's tolerance
    of an in-flight write.
    """
    field_count = len(header)
    reader = csv.reader(io.StringIO(_drop_torn_tail(text)), delimiter="\t")
    next(reader, None)  # header, already parsed by the caller

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
        physical_rows.append(
            {header[i]: (row[i] if i < len(row) else "") for i in range(field_count)}
        )

    return _fold_append_rows(physical_rows)


def _parse_tsv_by_id(raw: bytes | None) -> dict[str, dict[str, str]]:
    """Parse a records.tsv payload into a dict keyed by the "id" column.

    Dispatches on the physical header, exactly like object_records does
    (docs/append-only-storage-design.md): a file whose first column is
    `_op` is an append-mode log and is folded last-wins-by-id first (see
    _fold_append_rows), so callers only ever see LIVE logical records —
    never raw log rows, superseded values, or tombstoned ids. Classic
    files (no `_op` header) go through the original DictReader-based
    parse, unchanged.
    """
    if not raw:
        return {}
    text = raw.decode("utf-8")

    header_row = next(csv.reader(io.StringIO(text), delimiter="\t"), None)
    if header_row and header_row[0] == _OP_FIELD:
        return _parse_append_tsv_by_id(text, header_row)

    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    rows: dict[str, dict[str, str]] = {}
    for row in reader:
        record_id = row.get("id")
        if record_id is None:
            continue
        rows[record_id] = dict(row)
    return rows


def _records_tsv_member(collection: str) -> str:
    return f"data/collections/{collection}/records.tsv"


def _live_records_tsv(collection: str, *, data_dir: Path | str | None) -> bytes | None:
    path = object_backup._data_dir(data_dir) / "collections" / collection / "records.tsv"
    if not path.is_file():
        return None
    return path.read_bytes()


def _diff_records(
    backup_rows: dict[str, dict[str, str]],
    live_rows: dict[str, dict[str, str]],
) -> dict[str, object]:
    """Diff two id->row maps with the semantics "restoring makes live == backup"."""
    added = sorted(set(backup_rows) - set(live_rows))
    removed = sorted(set(live_rows) - set(backup_rows))
    changed = []
    unchanged = 0
    for record_id in sorted(set(backup_rows) & set(live_rows)):
        backup_row = backup_rows[record_id]
        live_row = live_rows[record_id]
        if backup_row == live_row:
            unchanged += 1
            continue
        fields = sorted(
            field
            for field in set(backup_row) | set(live_row)
            if backup_row.get(field) != live_row.get(field)
        )
        changed.append({"id": record_id, "fields": fields})

    diff_hash = hashlib.sha256(
        json.dumps({"added": added, "removed": removed, "changed": changed}, sort_keys=True).encode(
            "utf-8"
        )
    ).hexdigest()

    return {
        "diff_hash": diff_hash,
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": unchanged,
    }


def preview_collection(
    backup_id: str,
    collection: str,
    *,
    data_dir: Path | str | None = None,
) -> dict[str, object]:
    """Diff a collection's records between a backup and the live data dir.

    The semantics are "if you restore, live becomes the backup's version":
    added ids would reappear, removed ids would be dropped, changed ids
    would have the listed fields overwritten. Either side (or both) may be
    an append-mode file, a classic file, or missing; _parse_tsv_by_id folds
    append-mode logs to live records first, so this always diffs live
    logical content regardless of physical storage mode.
    """
    if not object_collections.validate_collection_name(collection):
        raise ValueError(f"Invalid collection name: {collection}")

    archive_path = backup_path(backup_id, data_dir=data_dir)
    member_name = _records_tsv_member(collection)
    archived_raw = object_backup.read_backup_member(archive_path, member_name)
    live_raw = _live_records_tsv(collection, data_dir=data_dir)

    backup_rows = _parse_tsv_by_id(archived_raw)
    live_rows = _parse_tsv_by_id(live_raw)
    diff = _diff_records(backup_rows, live_rows)

    return {
        "target": {"kind": "collection", "name": collection},
        "backup_id": backup_id,
        "present_in_backup": archived_raw is not None,
        "diff_hash": diff["diff_hash"],
        "added": diff["added"],
        "removed": diff["removed"],
        "changed": diff["changed"],
        "unchanged": diff["unchanged"],
    }


def preview_record(
    backup_id: str,
    collection: str,
    record_id: str,
    *,
    data_dir: Path | str | None = None,
) -> dict[str, object]:
    """Return one record's backup vs. live presence, without diffing all rows.

    `record` is the FOLDED record when the source is append-mode: a
    tombstoned id reports record=None/present_in_*=False exactly like an
    id that was never written.
    """
    if not object_collections.validate_collection_name(collection):
        raise ValueError(f"Invalid collection name: {collection}")

    archive_path = backup_path(backup_id, data_dir=data_dir)
    member_name = _records_tsv_member(collection)
    archived_raw = object_backup.read_backup_member(archive_path, member_name)
    live_raw = _live_records_tsv(collection, data_dir=data_dir)

    backup_rows = _parse_tsv_by_id(archived_raw)
    live_rows = _parse_tsv_by_id(live_raw)

    return {
        "collection": collection,
        "id": record_id,
        "record": backup_rows.get(record_id),
        "present_in_backup": record_id in backup_rows,
        "present_in_live": record_id in live_rows,
    }
