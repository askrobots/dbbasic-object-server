"""Read-only TSV-backed collection records.

Collection records are the simple data surface Scroll can point generated
tables and forms at. The current public API is read-only: records live in
``data/collections/{collection}/records.tsv`` and writes will come later after
permissions, audit, and migration rules are enforced by the server.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any, Iterable

import object_collections
import object_schemas
from object_versions import DEFAULT_DATA_DIR

COLLECTIONS_DIR = "collections"
RECORDS_FILE = "records.tsv"
DEFAULT_RECORD_LIMIT = 100
MAX_RECORD_LIMIT = 1000

_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class InvalidRecordIdError(ValueError):
    """Raised when a record id is not safe for routes or lookup."""


class RecordNotFoundError(LookupError):
    """Raised when a record cannot be found in a collection."""


def validate_record_id(record_id: str) -> bool:
    """Return True when a record id is route-safe."""
    if not isinstance(record_id, str):
        return False
    return bool(_RECORD_ID_RE.fullmatch(record_id))


def list_collection_records(
    collection: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
    limit: int = DEFAULT_RECORD_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Return a paginated record list for one collection."""
    _ensure_collection_known(collection, base_dir=base_dir, roots=roots)
    _validate_page(limit=limit, offset=offset)

    records = _read_collection_records(collection, base_dir=base_dir)
    total = len(records)
    window = records[offset:offset + limit]
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

    for record in _read_collection_records(collection, base_dir=base_dir):
        if record.get("id") == record_id:
            return record

    raise RecordNotFoundError(f"Record not found: {collection}/{record_id}")


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


def _read_collection_records(
    collection: str,
    *,
    base_dir: Path | str,
) -> list[dict[str, str]]:
    path = collection_records_file(collection, base_dir=base_dir)
    if not path.exists():
        return []
    if not path.is_file():
        raise ValueError(f"Collection records path is not a file: {collection}")

    with path.open(newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        _validate_header(collection, reader.fieldnames)
        records = []
        for row_number, row in enumerate(reader, start=2):
            if None in row:
                raise ValueError(
                    f"Collection records file has extra fields on row {row_number}: {collection}"
                )
            records.append({key: value if value is not None else "" for key, value in row.items()})
        return records


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


def _validate_page(*, limit: int, offset: int) -> None:
    if limit < 1:
        raise ValueError("Record limit must be at least 1")
    if limit > MAX_RECORD_LIMIT:
        raise ValueError(f"Record limit must be at most {MAX_RECORD_LIMIT}")
    if offset < 0:
        raise ValueError("Record offset must be at least 0")
