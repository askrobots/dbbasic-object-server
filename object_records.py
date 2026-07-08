"""TSV-backed collection records.

Collection records are the simple data surface Scroll can point generated
tables and forms at. Records live in
``data/collections/{collection}/records.tsv``.
"""

from __future__ import annotations

import csv
import math
import os
import re
import threading
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

_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
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
    submitted_fields = frozenset(clean)

    path = collection_records_file(collection, base_dir=base_dir)
    with _records_file_lock(path):
        records, fields = _read_records_and_fields(collection, path)
        for index, existing in enumerate(records):
            if existing.get("id") == record_id:
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
