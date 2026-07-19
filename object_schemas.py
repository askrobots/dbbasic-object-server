"""Schema metadata for DBBASIC collections.

Schemas describe the fields, validation hints, and relations Scroll can use to
render forms, tables, and diagrams. They are metadata, not the default storage
engine; objects can still read/write TSV files, APIs, SQL, or other backends.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

import object_collections
from object_versions import DEFAULT_DATA_DIR

SCHEMAS_DIR = "schemas"
_FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

# Storage engine opt-in (docs/append-only-storage-design.md). A schema's
# top-level "storage" key selects how object_records.py physically writes
# a collection's records.tsv: "classic" (default, omitted key) rewrites the
# whole file per write, unchanged from the original behavior; "append"
# opts a collection into append-only writes with last-wins-by-id reads.
# Only present in the normalized schema when the source payload set it, so
# a schema that never mentions storage stays byte-identical to before this
# feature existed (see the metadata-key handling in _normalize_schema).
STORAGE_CLASSIC = "classic"
STORAGE_APPEND = "append"
VALID_STORAGE_MODES = frozenset({STORAGE_CLASSIC, STORAGE_APPEND})

# Module-level cache for parsed+normalized manual schemas, keyed by the
# resolved schema file path. Value is ((mtime_ns, size), normalized_schema).
# Every caller of get_schema/_load_manual_schema was audited (object_records,
# object_field_permissions, object_packages, object_server, object_mcp
# argument-building, and the test suite) and none of them mutate the
# returned dict -- they only read from it (json.dumps for hashing, `.get`
# for field lookups, dict/set comprehensions that copy). So cache hits
# return the cached dict directly rather than paying for a defensive copy;
# if a future caller starts mutating it, that would need revisiting.
# Missing files are never cached (see _load_manual_schema): a schema being
# created later must be picked up on the very next call.
_SCHEMA_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


class InvalidSchemaNameError(ValueError):
    """Raised when a schema name is not safe for routes or storage."""


class SchemaNotFoundError(LookupError):
    """Raised when a schema cannot be found or derived."""


def validate_schema_name(schema: str) -> bool:
    """Return True when a schema/collection name is route-safe."""
    return object_collections.validate_collection_name(schema)


def list_schemas(
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
) -> list[dict[str, Any]]:
    """Return summaries for manual schemas and derived collection schemas."""
    base = Path(base_dir)
    collection_names = {item["name"] for item in object_collections.list_collections(base_dir=base, roots=roots)}
    manual_names = set(_iter_manual_schema_names(base))
    schemas = [
        _schema_summary(name, _load_manual_schema(name, base_dir=base), source="manual")
        if name in manual_names
        else _schema_summary(name, _derived_schema(name), source="derived")
        for name in sorted(collection_names | manual_names)
    ]
    return schemas


def get_schema(
    schema: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Return one manual schema, or an empty derived schema for a collection."""
    if not validate_schema_name(schema):
        raise InvalidSchemaNameError(f"Invalid schema name: {schema}")

    base = Path(base_dir)
    manual_schema = _load_manual_schema(schema, base_dir=base)
    if manual_schema is not None:
        return manual_schema

    collection_names = {
        item["name"]
        for item in object_collections.list_collections(base_dir=base, roots=roots)
    }
    if schema in collection_names:
        return _derived_schema(schema)

    raise SchemaNotFoundError(f"Schema not found: {schema}")


def replace_schema(
    schema: str,
    payload: Mapping[str, Any],
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    """Validate and atomically replace a manual schema file."""
    if not validate_schema_name(schema):
        raise InvalidSchemaNameError(f"Invalid schema name: {schema}")

    base = Path(base_dir)
    normalized = normalize_schema(schema, payload, source="manual")
    root = _schema_root(base)
    root.mkdir(parents=True, exist_ok=True)
    path = _schema_path(schema, base)

    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=root,
            prefix=f".{schema}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_name = tmp.name
            json.dump(normalized, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    finally:
        if tmp_name:
            try:
                Path(tmp_name).unlink()
            except FileNotFoundError:
                pass

    # Explicit invalidate rather than relying solely on the next read's stat
    # mismatch: mtime_ns has nanosecond resolution on APFS/ext4, but some
    # filesystems (and clocks) are coarser, so a write that lands in the
    # same tick as the cached signature could otherwise look unchanged.
    _SCHEMA_CACHE.pop(str(path.resolve(strict=False)), None)

    return normalized


def merge_schema_fields(
    base: Mapping[str, Any],
    mine: Mapping[str, Any],
    theirs: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Three-way merge a schema's field list; return (merged_schema, collisions).

    Schemas are additive (docs/upgrade-and-customization.md, Rule 3: Data
    Fields That Survive Schema Upgrades): an operator-added field and a
    package-added field should both survive an upgrade rather than forcing a
    conflict just because *something* changed on both sides. `base` is the
    schema as last recorded in the package baseline, `mine` is the live
    (possibly customized) schema, and `theirs` is the newly shipped schema.

    The result is `dict(theirs)` with `"fields"` replaced by the merged list.
    Field order is theirs' order first, then any mine-only fields appended in
    mine's order. `collisions` names fields that changed incompatibly on both
    sides (same field, no shared ancestor value, and mine != theirs); those
    fields keep the operator's version in `merged_schema`, pending a human
    decision -- callers should treat any non-empty `collisions` as "do not
    apply, park a conflict instead."

    Pure function: no I/O, no validation of the inputs or output.
    """
    base_by = {field["name"]: field for field in (base or {}).get("fields", [])}
    mine_by = {field["name"]: field for field in (mine or {}).get("fields", [])}
    theirs_by = {field["name"]: field for field in (theirs or {}).get("fields", [])}

    theirs_names = [field["name"] for field in (theirs or {}).get("fields", [])]
    mine_names = [field["name"] for field in (mine or {}).get("fields", [])]
    mine_only_names = [name for name in mine_names if name not in theirs_by]
    ordered_names = theirs_names + mine_only_names

    collisions: list[str] = []
    merged_fields: list[dict[str, Any]] = []

    for name in ordered_names:
        b = base_by.get(name)
        m = mine_by.get(name)
        t = theirs_by.get(name)

        if m is not None and t is not None:
            if m == t:
                merged_fields.append(m)
            elif b is not None and m == b:
                # Operator didn't touch it; package changed it.
                merged_fields.append(t)
            elif b is not None and t == b:
                # Package didn't touch it; operator changed it.
                merged_fields.append(m)
            else:
                # Both changed it (or there's no shared base to compare to):
                # a genuine collision. Keep the operator's version pending
                # a resolution, but flag it.
                collisions.append(name)
                merged_fields.append(m)
        elif m is not None:
            # Operator has it, package doesn't (never shipped it, or
            # removed it). Additive-safe: never silently drop a field the
            # operator kept.
            merged_fields.append(m)
        else:
            # Package has it, operator doesn't.
            if b is None:
                # Newly added by the package.
                merged_fields.append(t)
            elif t == b:
                # Operator removed it, package left it unchanged: respect
                # the removal.
                continue
            else:
                # Operator removed it, but the package changed it:
                # collision -- surface it rather than silently resurrect
                # a field the operator deliberately dropped.
                collisions.append(name)
                merged_fields.append(t)

    merged = dict(theirs)
    merged["fields"] = merged_fields
    return merged, collisions


def normalize_schema(
    schema: str,
    payload: Mapping[str, Any],
    *,
    source: str = "manual",
) -> dict[str, Any]:
    """Validate schema metadata and return canonical JSON-compatible data."""
    if not validate_schema_name(schema):
        raise InvalidSchemaNameError(f"Invalid schema name: {schema}")
    return _normalize_schema(schema, payload, source=source)


def _iter_manual_schema_names(base_dir: Path) -> list[str]:
    root = _schema_root(base_dir)
    if not root.exists() or not root.is_dir():
        return []

    names = []
    for path in sorted(root.glob("*.json"), key=lambda item: item.name):
        name = path.stem
        if validate_schema_name(name):
            names.append(name)
    return names


def _load_manual_schema(schema: str, *, base_dir: Path) -> dict[str, Any] | None:
    path = _schema_path(schema, base_dir)
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"Schema path is not a file: {schema}")

    cache_key = str(path.resolve(strict=False))
    try:
        stat_result = path.stat()
    except OSError:
        stat_result = None

    if stat_result is not None:
        signature = (stat_result.st_mtime_ns, stat_result.st_size)
        cached = _SCHEMA_CACHE.get(cache_key)
        if cached is not None and cached[0] == signature:
            return cached[1]

    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Schema file contains invalid JSON: {schema}") from exc

    normalized = _normalize_schema(schema, payload, source="manual")
    if stat_result is not None:
        _SCHEMA_CACHE[cache_key] = (signature, normalized)
    return normalized


def _derived_schema(schema: str) -> dict[str, Any]:
    return {
        "name": schema,
        "title": _title_from_name(schema),
        "source": "derived",
        "version": 1,
        "fields": [],
        "field_count": 0,
    }


def _schema_summary(
    schema: str,
    payload: dict[str, Any] | None,
    *,
    source: str,
) -> dict[str, Any]:
    active = payload or _derived_schema(schema)
    return {
        "name": active["name"],
        "title": active["title"],
        "source": source,
        "version": active["version"],
        "field_count": active["field_count"],
    }


def _normalize_schema(
    schema: str,
    payload: Any,
    *,
    source: str,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError(f"Schema file must contain an object: {schema}")

    name = _optional_string(payload.get("name")) or _optional_string(payload.get("collection")) or schema
    if name != schema:
        raise ValueError(f"Schema file name does not match schema collection: {schema}")
    if not validate_schema_name(name):
        raise InvalidSchemaNameError(f"Invalid schema name: {name}")

    fields_payload = payload.get("fields", [])
    if not isinstance(fields_payload, list):
        raise ValueError(f"Schema fields must be a list: {schema}")

    fields = [_normalize_field(field, schema=schema) for field in fields_payload]
    normalized = {
        "name": name,
        "title": _optional_string(payload.get("title")) or _title_from_name(name),
        "source": source,
        "version": _optional_int(payload.get("version")) or 1,
        "fields": fields,
        "field_count": len(fields),
    }
    if "storage" in payload:
        storage = payload.get("storage")
        if storage not in VALID_STORAGE_MODES:
            raise ValueError(
                f"Schema storage must be one of {sorted(VALID_STORAGE_MODES)}: {schema}"
            )
        normalized["storage"] = storage
    metadata_keys = (
        "description",
        "permissions",
        "ui",
        "layout",
        "views",
        "forms",
        "search",
        "table",
        "diagram",
        "validation",
    )
    for key in metadata_keys:
        if key in payload:
            normalized[key] = _json_compatible(payload[key], field_name=f"{schema}.{key}")
    return normalized


def _normalize_field(payload: Any, *, schema: str) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError(f"Schema field must be an object: {schema}")

    name = _optional_string(payload.get("name"))
    if name is None or not _FIELD_NAME_RE.fullmatch(name):
        raise ValueError(f"Schema field has invalid name: {schema}")
    if name == "_op":
        # Reserved for the append-only storage engine's internal op column
        # (docs/append-only-storage-design.md) -- never a real schema
        # field, so a schema field literally named "_op" can never collide
        # with it once a collection opts into "storage": "append".
        raise ValueError(f"Schema field name is reserved: {schema}._op")

    field: dict[str, Any] = {
        "name": name,
        "type": _optional_string(payload.get("type")) or "text",
        "required": bool(payload.get("required", False)),
    }

    metadata_keys = (
        "label",
        "description",
        "relation",
        "validation",
        "default",
        "enum",
        "transitions",
        "computed",
        "read_only",
        "readonly",
        "readOnly",
        "ui",
        "layout",
        "permissions",
        "placeholder",
        "help",
        "store",
    )
    for key in metadata_keys:
        if key in payload:
            field[key] = _json_compatible(payload[key], field_name=f"{schema}.{name}.{key}")

    return field


def _schema_root(base_dir: Path) -> Path:
    return base_dir / SCHEMAS_DIR


def _schema_path(schema: str, base_dir: Path) -> Path:
    if not validate_schema_name(schema):
        raise InvalidSchemaNameError(f"Invalid schema name: {schema}")

    root = _schema_root(base_dir)
    path = root / f"{schema}.json"
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)

    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise InvalidSchemaNameError(f"Schema path escapes schema directory: {schema}") from exc

    return path


def _title_from_name(name: str) -> str:
    return name.replace("_", " ").title()


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Schema string fields must be strings")
    text = value.strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Schema version must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Schema version must be an integer") from exc
    if number < 1:
        raise ValueError("Schema version must be at least 1")
    return number


def _json_compatible(value: Any, *, field_name: str) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_compatible(item, field_name=field_name) for item in value]
    if isinstance(value, Mapping):
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"Schema field '{field_name}' contains a non-string key")
            normalized[key] = _json_compatible(item, field_name=field_name)
        return normalized
    raise ValueError(f"Schema field '{field_name}' is not JSON-compatible")
