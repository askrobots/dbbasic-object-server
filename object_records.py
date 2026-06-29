"""TSV-backed collection records.

Collection records are the simple data surface Scroll can point generated
tables and forms at. Records live in
``data/collections/{collection}/records.tsv``.
"""

from __future__ import annotations

import csv
import os
import re
import threading
from contextlib import contextmanager
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
    """Return a paginated record list for one collection."""
    _ensure_collection_known(collection, base_dir=base_dir, roots=roots)
    _validate_page(limit=limit, offset=offset)

    records = read_collection_records(collection, base_dir=base_dir, roots=roots)
    return collection_records_payload(collection, records, limit=limit, offset=offset)


def read_collection_records(
    collection: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
) -> list[dict[str, str]]:
    """Return all records for one known collection."""
    _ensure_collection_known(collection, base_dir=base_dir, roots=roots)
    return _read_collection_records(collection, base_dir=base_dir)


def collection_records_payload(
    collection: str,
    records: list[dict[str, str]],
    *,
    limit: int = DEFAULT_RECORD_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Return the standard paginated record-list payload shape."""
    _validate_page(limit=limit, offset=offset)
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


def create_collection_record(
    collection: str,
    record: dict[str, Any],
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
) -> dict[str, str]:
    """Append one record to a collection TSV and return the stored row."""
    _ensure_collection_known(collection, base_dir=base_dir, roots=roots)
    clean = _normalize_record_payload(record, require_id=True)
    record_id = clean["id"]
    if not validate_record_id(record_id):
        raise InvalidRecordIdError(f"Invalid record id: {record_id}")

    path = collection_records_file(collection, base_dir=base_dir)
    with _records_file_lock(path):
        records, fields = _read_records_and_fields(collection, path)
        if any(row.get("id") == record_id for row in records):
            raise DuplicateRecordIdError(f"Record already exists: {collection}/{record_id}")

        merged_fields = _merge_fields(fields, clean)
        records.append(clean)
        _write_collection_records(collection, path, merged_fields, records)
        return _project_record(clean, merged_fields)


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

    clean = _normalize_record_payload(changes, require_id=False)
    if "id" in clean and clean["id"] != record_id:
        raise InvalidRecordPayloadError("Record id cannot be changed")

    path = collection_records_file(collection, base_dir=base_dir)
    with _records_file_lock(path):
        records, fields = _read_records_and_fields(collection, path)
        for index, existing in enumerate(records):
            if existing.get("id") == record_id:
                updated = dict(existing)
                updated.update(clean)
                updated["id"] = record_id
                merged_fields = _merge_fields(fields, updated)
                records[index] = updated
                _write_collection_records(collection, path, merged_fields, records)
                return _project_record(updated, merged_fields)

    raise RecordNotFoundError(f"Record not found: {collection}/{record_id}")


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
        records, fields = _read_records_and_fields(collection, path)
        for index, existing in enumerate(records):
            if existing.get("id") == record_id:
                removed = records.pop(index)
                _write_collection_records(collection, path, fields, records)
                return _project_record(removed, fields)

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


def _read_records_and_fields(
    collection: str,
    path: Path,
) -> tuple[list[dict[str, str]], list[str]]:
    if not path.exists():
        return [], ["id"]

    if not path.is_file():
        raise ValueError(f"Collection records path is not a file: {collection}")

    with path.open(newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        _validate_header(collection, reader.fieldnames)
        fields = list(reader.fieldnames or [])
        records = []
        for row_number, row in enumerate(reader, start=2):
            if None in row:
                raise ValueError(
                    f"Collection records file has extra fields on row {row_number}: {collection}"
                )
            records.append({key: value if value is not None else "" for key, value in row.items()})
        return records, fields


def _write_collection_records(
    collection: str,
    path: Path,
    fields: list[str],
    records: list[dict[str, str]],
) -> None:
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
    if require_id and "id" not in payload:
        raise InvalidRecordPayloadError("Record payload must include an id")

    clean: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not key:
            raise InvalidRecordPayloadError("Record field names must be non-empty strings")
        if key.strip() != key:
            raise InvalidRecordPayloadError(f"Record field name has whitespace: {key!r}")
        if "\t" in key or "\n" in key or "\r" in key:
            raise InvalidRecordPayloadError(f"Record field name is not TSV-safe: {key!r}")
        if value is None:
            clean[key] = ""
        elif isinstance(value, (str, int, float, bool)):
            clean[key] = str(value)
        else:
            raise InvalidRecordPayloadError(
                f"Record field value must be scalar or null: {key}"
            )

    return clean


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
