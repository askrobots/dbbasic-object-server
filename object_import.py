"""Generic import/export for TSV-backed collections.

Implements the CLI-scale slice of plan/vocabulary/13-import-export-spec.md
("the import/export block"): point a JSON/CSV/TSV source at a collection,
map its columns, dry-run every row against the target schema before
anything is written, then write via the ordinary record API so schema
rules and universal attribution apply exactly as a hand-typed form would.
The HTTP wizard, saved `import_recipes`/`import_runs` collections, chunked
execute, and the classic-mode bulk-rewrite entry point that spec also
describes are out of scope here -- this is the stdlib CLI half.

RUN CONFIG (a JSON file passed to ``run``): one file describes a whole
import run -- the collections it touches, in dependency order, and how
each collection's source rows map onto its schema.

    {
      "order": ["legacy_crm_orgs", "legacy_crm_contacts"],
      "collections": {
        "legacy_crm_orgs": {
          "input": "orgs.json",
          "mapping": {"OrgId": "id", "OrgName": "name"},
          "constants": {"owner_id": "u_import"}
        },
        "legacy_crm_contacts": {
          "input": "contacts.csv",
          "mapping": {
            "ContactId": "id",
            "FullName": "name",
            "OrgId": "org_id",
            "Urgency": "urgency",
            "LegacyNotes": "extra"
          },
          "extra_fields": ["InternalRefCode"],
          "ignore_fields": ["RowNumber"],
          "value_maps": [{"field": "urgency", "map": {"medium": "normal"}}],
          "constants": {"owner_id": "u_import"}
        }
      }
    }

Per collection:

- ``input`` -- path to the source file (relative paths resolve against the
  run config's own directory). Format is inferred from the extension
  (``.json``/``.csv``/``.tsv``); a JSON source is a list of flat objects.
- ``mapping`` -- ``{source_column: dest_field}``. Every source column must
  be accounted for by ``mapping``, ``extra_fields``, or ``ignore_fields``;
  an unmapped column is a dry-run error ("unknown fields not covered by
  mapping" -- see the module-level note under Spec Conflicts below), not a
  silent drop. Map a column to ``"id"`` to supply the record id (source
  ids are kept exactly -- see ID Preservation below); map one to
  ``"extra"`` to route it into the record's JSON overflow field under its
  own source-column name.
- ``extra_fields`` -- source columns routed into ``extra`` the same way,
  spelled as a plain list instead of repeating ``"extra"`` in ``mapping``
  for every one.
- ``ignore_fields`` -- source columns intentionally dropped (e.g. a
  spreadsheet's own row-number column).
- ``value_maps`` -- ``[{"field": dest_field, "map": {source_value:
  dest_value}}]``, applied to the mapped/renamed value.
- ``constants`` -- ``{dest_field: value}`` stamped onto every row,
  overriding whatever the row itself supplied (e.g. attributing every
  imported record to one operator-chosen owner).

ID PRESERVATION: whatever a row resolves as ``id`` (via ``mapping`` or a
constant) is written exactly as given -- object_import never generates a
fresh id, because other collections in the same run (and existing data)
may already reference it.

CREATED_AT PRESERVATION: a schema's ``created_at`` is ordinarily
``read_only`` -- object_records.create_collection_record rejects a
client-submitted value for it, same as any other read-only field, so a
hand-typed form can never spoof "created two years ago". An import
replaying another system's history needs the opposite: the source row's
own timestamp, not "now". Real-run creates in this module always pass
``create_collection_record(..., preserve_read_only=True)`` -- a new,
narrowly-scoped kwarg added to object_records.py alongside this module
(see its docstring) that allows a *plain* read-only field's submitted
value through while still rejecting a genuinely ``computed`` field. Map a
source column to ``created_at`` (or any other read-only-but-not-computed
field) in the mapping and its value is preserved verbatim; leave it
unmapped and the server stamps "now", exactly as an ordinary create would.
Updates (the ``--update-existing`` duplicate path) never touch read-only
fields, preserved or not -- an existing record's own history shouldn't
shift just because a repeat import touched other fields on it.

SPEC CONFLICTS (13-import-export-spec.md vs. this module, per the task
that produced it -- the task wins, noted here for the record):

- The spec's ``import_recipes.mapping`` auto-routes any source column
  *absent* from the map into ``extra`` by default. This module instead
  requires every source column to be explicitly accounted for (mapping /
  extra_fields / ignore_fields) and reports an unmapped column as a
  dry-run error, per this task's explicit "unknown fields not covered by
  mapping" validation requirement.
- No ``import_recipes``/``import_runs`` collections, no chunked/resumable
  execute, no classic-mode bulk-rewrite entry point, no delimiter/encoding
  sniffing, no ``partial_import_policy`` toggle (a row that fails
  validation is always skipped-and-reported; valid rows in the same file
  are always still written -- there is no "block the whole run" mode
  here). All namable "implementation-session decisions" the spec's Open
  Questions section explicitly deferred; this module is the narrower CLI
  slice the task asked for, not the full block.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Iterable

import object_collections
import object_record_changes
import object_records
import object_schemas
from object_versions import DEFAULT_DATA_DIR

DATA_DIR_ENV = "DBBASIC_DATA_DIR"
EXTRA_ROUTE = "extra"
SOURCE_FORMATS = {"json", "csv", "tsv"}


class ImportConfigError(ValueError):
    """Raised when a run config or a collection's mapping is malformed."""


class ImportRunError(ValueError):
    """Raised when a run itself can't proceed (e.g. no --actor for a real run)."""


# --------------------------------------------------------------------------
# Run config
# --------------------------------------------------------------------------

def load_plan(config_path: Path) -> dict[str, Any]:
    """Load and normalize a run config: ``{"order": [...], "collections": {...}}``."""
    config_path = Path(config_path)
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ImportConfigError(f"Run config not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ImportConfigError(f"Run config is not valid JSON: {config_path} ({exc})") from exc

    if not isinstance(payload, dict):
        raise ImportConfigError("Run config must be a JSON object")

    order = payload.get("order")
    if not isinstance(order, list) or not order or not all(isinstance(item, str) and item for item in order):
        raise ImportConfigError("Run config 'order' must be a non-empty list of collection names")

    collections = payload.get("collections")
    if not isinstance(collections, dict):
        raise ImportConfigError("Run config 'collections' must be an object")

    missing = [name for name in order if name not in collections]
    if missing:
        raise ImportConfigError(f"Run config 'order' names collections with no config: {missing}")

    base = config_path.resolve().parent
    normalized = {
        name: _normalize_collection_config(name, collections[name], base=base) for name in order
    }
    return {"order": list(order), "collections": normalized}


def _normalize_collection_config(name: str, raw: Any, *, base: Path) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ImportConfigError(f"Collection config for {name!r} must be an object")

    input_value = raw.get("input")
    if not isinstance(input_value, str) or not input_value:
        raise ImportConfigError(f"Collection config for {name!r} is missing 'input'")
    input_path = Path(input_value)
    if not input_path.is_absolute():
        input_path = base / input_path

    fmt = raw.get("format")
    if fmt is not None and fmt not in SOURCE_FORMATS:
        raise ImportConfigError(f"Collection config for {name!r} has an unknown 'format': {fmt!r}")

    mapping = raw.get("mapping") or {}
    if not isinstance(mapping, dict) or not all(isinstance(v, str) for v in mapping.values()):
        raise ImportConfigError(f"Collection config for {name!r}: 'mapping' must be an object of strings")

    extra_fields = raw.get("extra_fields") or []
    ignore_fields = raw.get("ignore_fields") or []
    value_maps = raw.get("value_maps") or []
    constants = raw.get("constants") or {}
    if not isinstance(value_maps, list):
        raise ImportConfigError(f"Collection config for {name!r}: 'value_maps' must be a list")
    if not isinstance(constants, dict):
        raise ImportConfigError(f"Collection config for {name!r}: 'constants' must be an object")

    return {
        "collection": name,
        "input": input_path,
        "format": fmt,
        "mapping": dict(mapping),
        "extra_fields": list(extra_fields),
        "ignore_fields": list(ignore_fields),
        "value_maps": list(value_maps),
        "constants": dict(constants),
    }


# --------------------------------------------------------------------------
# Source loading
# --------------------------------------------------------------------------

def load_source_rows(path: Path, fmt: str | None = None) -> list[dict[str, Any]]:
    """Return a source file's rows as plain dicts. JSON: a list of objects. CSV/TSV: DictReader rows."""
    path = Path(path)
    fmt = fmt or _infer_format(path)
    if fmt == "json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ImportConfigError(f"Import source not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ImportConfigError(f"Import source is not valid JSON: {path} ({exc})") from exc
        if not isinstance(payload, list):
            raise ImportConfigError(f"JSON import source must be a list of records: {path}")
        rows = []
        for index, row in enumerate(payload, start=1):
            if not isinstance(row, dict):
                raise ImportConfigError(f"JSON import source row {index} is not an object: {path}")
            rows.append(row)
        return rows

    delimiter = "\t" if fmt == "tsv" else ","
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            return [dict(row) for row in reader]
    except FileNotFoundError as exc:
        raise ImportConfigError(f"Import source not found: {path}") from exc


def _infer_format(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix not in SOURCE_FORMATS:
        raise ImportConfigError(
            f"Cannot infer source format from extension (expected .json/.csv/.tsv): {path}"
        )
    return suffix


# --------------------------------------------------------------------------
# Row mapping
# --------------------------------------------------------------------------

def map_row(source_row: dict[str, Any], cfg: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Apply one collection's rename/extra/value-map/constant rules to a source row.

    Returns ``(dest_record, errors)``. ``dest_record`` is the payload
    object_records would receive; ``errors`` covers mapping-time problems
    (an unmapped column, no resolvable id) -- schema-level checks
    (required/enum/relation) happen later in ``validate_row``, once the
    target schema is known.
    """
    errors: list[str] = []
    mapping: dict[str, str] = cfg["mapping"]
    extra_fields = set(cfg["extra_fields"])
    ignore_fields = set(cfg["ignore_fields"])

    dest: dict[str, Any] = {}
    extra_blob: dict[str, Any] = {}

    for source_col, value in source_row.items():
        if source_col in mapping:
            dest_name = mapping[source_col]
            if dest_name == EXTRA_ROUTE:
                extra_blob[source_col] = value
            else:
                dest[dest_name] = value
        elif source_col in extra_fields:
            extra_blob[source_col] = value
        elif source_col in ignore_fields:
            continue
        else:
            errors.append(
                f"unmapped source column {source_col!r} (add it to mapping, "
                "extra_fields, or ignore_fields)"
            )

    for rule in cfg["value_maps"]:
        if not isinstance(rule, dict):
            continue
        target_field = rule.get("field")
        value_map = rule.get("map") or {}
        if target_field in dest:
            raw = dest[target_field]
            raw_str = "" if raw is None else str(raw)
            if raw_str in value_map:
                dest[target_field] = value_map[raw_str]

    for const_name, const_value in cfg["constants"].items():
        dest[const_name] = const_value

    if dest.get("id") in (None, ""):
        errors.append('row has no resolvable "id" (map a source column to "id" in the mapping)')
    else:
        dest["id"] = str(dest["id"])

    if extra_blob:
        dest["extra"] = extra_blob

    return dest, errors


# --------------------------------------------------------------------------
# Row validation (dry-run and real-run share this)
# --------------------------------------------------------------------------

def _is_computed_field(field: dict[str, Any]) -> bool:
    field_type = str(field.get("type", "")).lower()
    return bool(field_type == "computed" or field.get("computed"))


def _is_empty(value: str) -> bool:
    return value is None or value == ""


def validate_row(
    collection: str,
    dest: dict[str, Any],
    *,
    base_dir: Path,
    roots: Iterable[Path] | None,
    known_ids: dict[str, set[str]],
) -> list[str]:
    """Validate one mapped row against the target schema's field rules.

    Reuses object_records' own private per-field checks (type/enum/
    validation-rule/relation) directly, rather than re-deriving them --
    there is no public dry-run validator yet (13-import-export-spec.md's
    own Open Questions section names this exact gap: "Dry-run validation
    needs a new public entry point"). Reusing the private functions means
    a validation error here is worded identically to what a real write
    would raise, same as the spec wants; a future public
    ``validate_record_payload`` can replace this call site without
    changing behavior.

    Relation checks are widened for "target existence within the run": a
    relation to a row from an earlier (or the same) collection in this run
    that has already validated is accepted even though nothing has been
    written yet -- ``known_ids`` accumulates as the run proceeds, seeded
    per collection with whatever already exists on disk.
    """
    errors: list[str] = []
    record_id = str(dest.get("id", ""))
    if not object_records.validate_record_id(record_id):
        errors.append(f"invalid record id: {record_id!r}")

    fields = object_records._schema_fields(collection, base_dir=base_dir, roots=roots)
    submitted = frozenset(dest)

    for schema_field in fields:
        name = schema_field["name"]
        if name == "id":
            continue
        raw_value = dest.get(name, "")
        value = "" if raw_value is None else str(raw_value)
        read_only = object_records._is_computed_or_read_only(schema_field)
        computed = _is_computed_field(schema_field)

        if name in submitted and read_only:
            if computed:
                errors.append(f"field '{name}' is computed and cannot be imported")
                continue
            # Plain read-only (not computed): allowed here. A real run
            # carries it through via create_collection_record(...,
            # preserve_read_only=True) -- see the module docstring.

        if object_records._field_is_required(schema_field) and not read_only and _is_empty(value):
            errors.append(f"field '{name}' is required")
            continue
        if _is_empty(value):
            continue

        try:
            object_records._validate_field_type(schema_field, value)
            object_records._validate_field_enum(schema_field, value)
            object_records._validate_field_rules(schema_field, value)
        except object_records.InvalidRecordPayloadError as exc:
            errors.append(str(exc))
            continue

        relation_error = _check_relation(schema_field, value, known_ids=known_ids, base_dir=base_dir)
        if relation_error:
            errors.append(relation_error)

    return errors


def _check_relation(
    schema_field: dict[str, Any],
    value: str,
    *,
    known_ids: dict[str, set[str]],
    base_dir: Path,
) -> str | None:
    relation = schema_field.get("relation")
    if relation is None:
        return None
    target = relation.get("collection") if isinstance(relation, dict) else relation
    if isinstance(target, str) and value in known_ids.get(target, ()):
        return None
    try:
        object_records._validate_field_relation(schema_field, value, base_dir=base_dir)
    except object_records.InvalidRecordPayloadError as exc:
        return str(exc)
    return None


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

@dataclass
class RowOutcome:
    row_number: int
    id: str
    status: str  # "ok" | "exists" | "error" -- validation-time classification, never mutated
    reasons: list[str]
    action: str | None = None  # "created" | "updated" | "skipped" -- set only on a real run


@dataclass
class CollectionReport:
    collection: str
    total: int = 0
    ok: int = 0
    exists: int = 0
    errors: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    rows: list[RowOutcome] = dataclass_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection": self.collection,
            "total": self.total,
            "ok": self.ok,
            "exists": self.exists,
            "errors": self.errors,
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "rows": [
                {
                    "row": row.row_number,
                    "id": row.id,
                    "status": row.status,
                    "reasons": row.reasons,
                    "action": row.action,
                }
                for row in self.rows
            ],
        }


# --------------------------------------------------------------------------
# Run orchestration
# --------------------------------------------------------------------------

def run_import(
    plan: dict[str, Any],
    *,
    base_dir: Path,
    roots: Iterable[Path] | None = None,
    dry_run: bool,
    actor: str | None = None,
    update_existing: bool = False,
) -> list[CollectionReport]:
    """Validate (and, unless ``dry_run``, write) every collection in a run's order.

    Nothing is written for a collection until every row in it has been
    mapped and validated; only rows without errors are ever passed to
    create/update. Duplicate ids are checked twice, at different scopes:
    within the source file itself (two rows claiming the same id is
    always an error) and against the target collection's existing ids
    (idempotency -- ``exists``, resolved to skip or update below).
    """
    if not dry_run and not actor:
        raise ImportRunError("actor is required for a real run (pass --actor, or use --dry-run)")

    known_ids: dict[str, set[str]] = {}
    reports: list[CollectionReport] = []

    for collection in plan["order"]:
        cfg = plan["collections"][collection]
        source_rows = load_source_rows(cfg["input"], cfg["format"])

        existing_ids = _existing_ids(collection, base_dir=base_dir, roots=roots)
        known_ids.setdefault(collection, set()).update(existing_ids)

        report = CollectionReport(collection=collection, total=len(source_rows))
        seen_in_file: set[str] = set()
        valid_rows: dict[int, dict[str, Any]] = {}

        for row_number, source_row in enumerate(source_rows, start=1):
            dest, reasons = map_row(source_row, cfg)
            record_id = str(dest.get("id", ""))

            if reasons:
                report.rows.append(RowOutcome(row_number, record_id, "error", reasons))
                report.errors += 1
                continue

            if record_id in seen_in_file:
                report.rows.append(
                    RowOutcome(
                        row_number, record_id, "error",
                        [f"duplicate id within import file: {record_id}"],
                    )
                )
                report.errors += 1
                continue

            row_errors = validate_row(
                collection, dest, base_dir=base_dir, roots=roots, known_ids=known_ids
            )
            if row_errors:
                report.rows.append(RowOutcome(row_number, record_id, "error", row_errors))
                report.errors += 1
                continue

            seen_in_file.add(record_id)
            known_ids[collection].add(record_id)
            status = "exists" if record_id in existing_ids else "ok"
            report.rows.append(RowOutcome(row_number, record_id, status, []))
            if status == "exists":
                report.exists += 1
            else:
                report.ok += 1
            valid_rows[row_number] = dest

        if not dry_run:
            _execute_collection(
                collection,
                report,
                valid_rows,
                existing_ids=existing_ids,
                base_dir=base_dir,
                roots=roots,
                actor=actor,
                update_existing=update_existing,
            )

        reports.append(report)

    return reports


def _existing_ids(collection: str, *, base_dir: Path, roots: Iterable[Path] | None) -> set[str]:
    try:
        rows = object_records.read_collection_records(collection, base_dir=base_dir, roots=roots)
    except (object_collections.CollectionNotFoundError, object_collections.InvalidCollectionNameError):
        return set()
    return {row["id"] for row in rows}


def _execute_collection(
    collection: str,
    report: CollectionReport,
    valid_rows: dict[int, dict[str, Any]],
    *,
    existing_ids: set[str],
    base_dir: Path,
    roots: Iterable[Path] | None,
    actor: str,
    update_existing: bool,
) -> None:
    for outcome in report.rows:
        if outcome.status == "error":
            continue
        dest = valid_rows[outcome.row_number]
        record_id = dest["id"]

        if outcome.status == "exists":
            if update_existing:
                changes = {k: v for k, v in dest.items() if k != "id"}
                changes = _strip_read_only(collection, changes, base_dir=base_dir, roots=roots)
                object_records.update_collection_record(
                    collection, record_id, changes, base_dir=base_dir, roots=roots, actor=actor
                )
                outcome.action = "updated"
                report.updated += 1
            else:
                outcome.action = "skipped"
                report.skipped += 1
            continue

        object_records.create_collection_record(
            collection, dest, base_dir=base_dir, roots=roots, actor=actor, preserve_read_only=True
        )
        outcome.action = "created"
        report.created += 1


def _strip_read_only(
    collection: str, changes: dict[str, Any], *, base_dir: Path, roots: Iterable[Path] | None
) -> dict[str, Any]:
    fields = object_records._schema_fields(collection, base_dir=base_dir, roots=roots)
    read_only_names = {f["name"] for f in fields if object_records._is_computed_or_read_only(f)}
    return {name: value for name, value in changes.items() if name not in read_only_names}


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

def export_collection(
    collection: str, *, base_dir: Path, roots: Iterable[Path] | None = None
) -> list[dict[str, str]]:
    """Return every record in a collection exactly as the read API returns it."""
    return object_records.read_collection_records(collection, base_dir=base_dir, roots=roots)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import records into, or export them out of, TSV-backed collections."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help=f"runtime data directory (default: ${DATA_DIR_ENV} or ./data)",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    run = subcommands.add_parser("run", help="import records per a run config")
    run.add_argument("config", type=Path, help="path to the run config JSON file")
    run.add_argument("--dry-run", action="store_true", help="validate only; write nothing")
    run.add_argument("--actor", help="required for a real run; attributed on every write")
    run.add_argument(
        "--update-existing",
        action="store_true",
        help="upsert rows whose id already exists in the target collection (default: skip them)",
    )
    run.add_argument("--json", action="store_true", help="print the report as JSON")

    export = subcommands.add_parser("export", help="dump one collection to JSON")
    export.add_argument("collection")
    export.add_argument(
        "--output", type=Path, default=None, help="write to this file instead of stdout"
    )

    args = parser.parse_args(argv)
    base_dir = _base_dir(args.data_dir)

    try:
        if args.command == "run":
            plan = load_plan(args.config)
            reports = run_import(
                plan,
                base_dir=base_dir,
                dry_run=args.dry_run,
                actor=args.actor,
                update_existing=args.update_existing,
            )
            _print_run_report(reports, json_output=args.json, dry_run=args.dry_run)
            return 1 if any(report.errors for report in reports) else 0

        if args.command == "export":
            records = export_collection(args.collection, base_dir=base_dir)
            payload = json.dumps(records, indent=2)
            if args.output:
                args.output.write_text(payload + "\n", encoding="utf-8")
                print(f"Exported {len(records)} record(s) from {args.collection} to {args.output}")
            else:
                print(payload)
            return 0
    except (ImportConfigError, ImportRunError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (object_collections.CollectionNotFoundError, object_collections.InvalidCollectionNameError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"Unknown command: {args.command}")
    return 2


def _base_dir(data_dir: Path | None) -> Path:
    if data_dir is not None:
        return data_dir
    return Path(os.environ.get(DATA_DIR_ENV, "data"))


def _print_run_report(reports: list[CollectionReport], *, json_output: bool, dry_run: bool) -> None:
    if json_output:
        print(json.dumps([report.to_dict() for report in reports], indent=2))
        return

    for report in reports:
        summary = f"{report.collection}: {report.ok} ok, {report.exists} exists, {report.errors} errors"
        if not dry_run:
            summary += f" -- {report.created} created, {report.updated} updated, {report.skipped} skipped"
        print(summary)
        for row in report.rows:
            if row.status == "error":
                print(f"  row {row.row_number} (id={row.id or '?'}): " + "; ".join(row.reasons))


if __name__ == "__main__":
    raise SystemExit(main())
