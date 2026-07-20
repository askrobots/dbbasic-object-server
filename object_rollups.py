"""Rollup block: declared aggregations materialized as ordinary collections.

Implements ``plan/vocabulary/14-rollup-spec.md``. A ``rollup_definitions``
record declares one source collection, an optional flat-equality filter, a
``group_by``, an optional ``time_bucket`` (day/week/month truncation), and
a list of ``metrics`` (count/sum/min/max/avg). ``compute_rollup`` reads the
source outside row-level permissions (daemon posture, same as
``object_records.compact_collection``), computes the full current answer,
and writes it into ``target_collection`` -- a schema-derived, ordinary
collection that renders through the existing generators with zero page
code, the stated payoff (14's Surfaces section).

**Storage is a full rewrite by effect, not by mechanism.** 14's Storage
section is explicit that a SCHEDULED recompute should behave like classic
storage's atomic replace-the-whole-file write -- "the correct operation,
not a workaround." There is no public bulk "replace this entire
collection" primitive in ``object_records`` to call (the module-private
one, ``_write_collection_records``, is reserved for that module's own
internal callers), so this reconciles the target to the newly computed row
set through the ordinary per-row record API instead: create a row for
every new id, update every row that already existed, delete every row
whose group no longer exists. The net effect is identical to a full
rewrite -- a full recompute always produces every row's current answer and
always drops stale groups -- it is just expressed as N create/update/delete
calls rather than one file swap.

**Scheduled only in v1.** 14's Events section describes an INCREMENTAL
create-fast-path (a HANDLES subscriber on the source collection, updating
one target row additively) alongside a reconciling scheduled recompute
that "self-heals any drift the fast path couldn't handle correctly." This
module implements only the reconciling recompute side. A definition
declaring ``refresh_mode: incremental`` is accepted and still recomputes
correctly on its own ``refresh_interval_seconds`` -- 14 is explicit that
"scheduled-only is the v1 default and the recommendation" for most
sources, and that incremental's own interval is required precisely
because it "is not incremental instead of scheduled, it is an additive
create-fast-path on top of a slower scheduled full recompute." Without the
fast path, an incremental definition simply behaves as scheduled-only --
the same graceful-degradation shape 14's own Degradation section
describes for ``DBBASIC_ENABLE_EVENT_HANDLERS`` being unset ("nothing
breaks; freshness just tracks the scheduled interval instead of
near-real-time"). The HANDLES-subscriber fast path is not built here.

**Writing computed fields.** The derived target schema marks every metric
field (and ``computed_at``) ``"computed": true`` per 14's field contract
("a target row's numbers come only from the rollup pass, never from a
form POST"). ``object_records.create_collection_record`` /
``update_collection_record`` reject a write to a genuinely computed field
from ANY caller by default -- there was no existing escape hatch for a
system pass to write its own derived values, so this block adds one
(``allow_computed_submission``, see object_records.py), used only here.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

import object_collections
import object_records
import object_schemas

ROLLUP_DEFINITIONS_COLLECTION = "rollup_definitions"
FEATURE_FLAGS_COLLECTION = "feature_flags"
ROLLUP_ENABLED_FLAG = "rollup_enabled"
ROLLUP_ACTOR = "daemon:rollup"

_TIME_BUCKET_GRANULARITIES = frozenset({"none", "day", "week", "month"})
_METRIC_OPS = frozenset({"count", "sum", "min", "max", "avg"})
_REFRESH_MODES = frozenset({"scheduled", "incremental"})
_RESERVED_TARGET_FIELD_NAMES = frozenset({"id", "bucket_start", "computed_at"})


class DefinitionError(ValueError):
    """Raised when a rollup_definitions record cannot be computed as-is.

    Covers both malformed JSON sub-fields and violations of 14's own
    doctrine (a filter value that isn't a flat scalar, a metric field that
    isn't numeric, and so on). The daemon pass catches this per-definition
    (process_rollups, object_daemon.py), same isolation posture as
    process_compactions: one bad definition is logged and skipped, never
    allowed to stop the rest of the pass.
    """


@dataclass(frozen=True)
class Metric:
    op: str
    field: str | None
    as_name: str
    source_type: str | None  # "integer" | "number" | None (count has none)
    target_type: str  # "integer" | "number"


@dataclass(frozen=True)
class RollupConfig:
    definition_id: str
    name: str
    source_collection: str
    target_collection: str
    filter: dict[str, Any]
    group_by: tuple[str, ...]
    time_bucket_field: str | None
    time_bucket_granularity: str
    time_bucket_source_type: str | None
    metrics: tuple[Metric, ...]
    min_group_size: int | None
    refresh_mode: str
    refresh_interval_seconds: int
    enabled: bool


# --- Feature flag / due gate -------------------------------------------------

def rollup_pass_enabled(*, base_dir: Any) -> bool:
    """Block-wide kill switch, ``<block>_enabled`` convention (00-doctrine-
    and-contract.md), a ``feature_flags`` row named ``rollup_enabled``.

    Default ON -- same reasoning 12-notify-spec.md gives for its own
    block-wide flag defaulting on: this is the brownout kill switch, not
    an adoption gate, and with zero ``rollup_definitions`` rows installed
    it does nothing regardless. A missing row, a missing/unreadable
    ``feature_flags`` collection, and a blank value all resolve to "on";
    only an explicit off/false/0/no value turns the pass off (14's
    Degradation: "existing target collections keep serving their
    last-computed data").
    """
    try:
        rows = object_records.read_collection_records(FEATURE_FLAGS_COLLECTION, base_dir=base_dir)
    except (
        object_collections.CollectionNotFoundError,
        object_collections.InvalidCollectionNameError,
        OSError,
        ValueError,
    ):
        return True
    for row in rows:
        if row.get("flag") == ROLLUP_ENABLED_FLAG:
            value = (row.get("value") or "").strip().lower()
            if not value:
                return True
            return value not in {"off", "false", "0", "no"}
    return True


def is_definition_enabled(record: Mapping[str, str]) -> bool:
    """Per-definition kill switch (14's ``enabled`` field), independent of
    the block-wide ``rollup_enabled`` flag -- "lets an operator pause one
    noisy or broken definition without disabling every rollup on the
    instance." Blank/missing defaults to enabled, same as any schema
    boolean field with ``"default": "true"``.
    """
    return _truthy(record.get("enabled"), default=True)


def is_definition_due(record: Mapping[str, str], *, now: datetime | None = None) -> bool:
    """SCHEDULED-mode due gate: ``last_computed_at`` + ``refresh_interval_
    seconds``, both read straight off the definition record -- 14's
    Storage section is explicit that ``rollup_definitions`` needs no
    separate marker file the way ``process_compactions`` does, "because it
    has no natural per-collection record to hold state on, a rollup
    definition already is that record."

    A never-computed definition (``last_computed_at`` blank) is always
    due. A malformed ``refresh_interval_seconds`` is also treated as due,
    so the definition's real problem surfaces as a logged
    ``compute_rollup`` failure rather than the daemon silently never
    attempting it.
    """
    now = now or datetime.now(timezone.utc)
    last_computed_raw = (record.get("last_computed_at") or "").strip()
    if not last_computed_raw:
        return True
    interval = _positive_int(record.get("refresh_interval_seconds"))
    if interval is None:
        return True
    last_computed = _parse_iso(last_computed_raw)
    if last_computed is None:
        return True
    return (now - last_computed).total_seconds() >= interval


# --- Definition parsing -------------------------------------------------

def parse_definition(
    record: Mapping[str, str], *, base_dir: Any, roots: Any = None
) -> RollupConfig:
    """Validate and normalize one ``rollup_definitions`` row.

    Raises ``DefinitionError`` for anything malformed, and lets
    ``object_schemas.SchemaNotFoundError`` /
    ``object_collections.CollectionNotFoundError`` propagate unchanged
    when ``source_collection`` doesn't exist -- 14's Degradation section
    treats a missing source as its own failure mode ("the recompute for
    that definition fails and is logged; the target collection keeps
    serving its last successful computation"), distinct from a malformed
    definition.
    """
    definition_id = (record.get("id") or "").strip()
    if not definition_id:
        raise DefinitionError("rollup definition is missing an id")

    name = (record.get("name") or "").strip() or definition_id

    source_collection = (record.get("source_collection") or "").strip()
    if not source_collection or not object_collections.validate_collection_name(source_collection):
        raise DefinitionError(f"{definition_id}: source_collection is required and must be a valid collection name")

    target_collection = (record.get("target_collection") or "").strip()
    if not target_collection or not object_collections.validate_collection_name(target_collection):
        raise DefinitionError(f"{definition_id}: target_collection is required and must be a valid collection name")

    # Raises SchemaNotFoundError when source_collection doesn't exist --
    # deliberately not caught here, see docstring.
    source_schema = object_schemas.get_schema(source_collection, base_dir=base_dir, roots=roots)
    source_fields = {field["name"]: field for field in source_schema.get("fields", [])}

    filter_ = _parse_filter(record, definition_id=definition_id)
    group_by = _parse_group_by(record, definition_id=definition_id)

    time_bucket_field, time_bucket_granularity, time_bucket_source_type = _parse_time_bucket(
        record, definition_id=definition_id, source_collection=source_collection, source_fields=source_fields,
    )

    metrics = _parse_metrics(
        record,
        definition_id=definition_id,
        source_collection=source_collection,
        source_fields=source_fields,
        group_by=group_by,
    )

    min_group_size = _parse_min_group_size(record, definition_id=definition_id)

    refresh_mode = (record.get("refresh_mode") or "scheduled").strip().lower()
    if refresh_mode not in _REFRESH_MODES:
        raise DefinitionError(f"{definition_id}: refresh_mode must be one of {sorted(_REFRESH_MODES)}")

    refresh_interval_seconds = _positive_int(record.get("refresh_interval_seconds"))
    if refresh_interval_seconds is None:
        raise DefinitionError(f"{definition_id}: refresh_interval_seconds is required and must be a positive integer")

    enabled = _truthy(record.get("enabled"), default=True)

    return RollupConfig(
        definition_id=definition_id,
        name=name,
        source_collection=source_collection,
        target_collection=target_collection,
        filter=filter_,
        group_by=group_by,
        time_bucket_field=time_bucket_field,
        time_bucket_granularity=time_bucket_granularity,
        time_bucket_source_type=time_bucket_source_type,
        metrics=metrics,
        min_group_size=min_group_size,
        refresh_mode=refresh_mode,
        refresh_interval_seconds=refresh_interval_seconds,
        enabled=enabled,
    )


def _parse_filter(record: Mapping[str, str], *, definition_id: str) -> dict[str, Any]:
    raw = _json_field(record, "filter", {}, definition_id=definition_id)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise DefinitionError(f"{definition_id}: filter must be a flat JSON object")
    normalized: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, (dict, list)):
            raise DefinitionError(
                f"{definition_id}: filter.{key} must be a scalar -- rollup filters are flat "
                "equality only, ANDed (00-doctrine-and-contract.md); escalate to a custom "
                "object for OR/join/subquery/computed conditions"
            )
        if value is None:
            raise DefinitionError(f"{definition_id}: filter.{key} must not be null")
        if isinstance(value, bool):
            value = "true" if value else "false"
        normalized[key] = value
    return normalized


def _parse_group_by(record: Mapping[str, str], *, definition_id: str) -> tuple[str, ...]:
    raw = _json_field(record, "group_by", [], definition_id=definition_id)
    if raw is None:
        raw = []
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        raise DefinitionError(f"{definition_id}: group_by must be a JSON list of field names")
    return tuple(raw)


def _parse_time_bucket(
    record: Mapping[str, str],
    *,
    definition_id: str,
    source_collection: str,
    source_fields: dict[str, dict[str, Any]],
) -> tuple[str | None, str, str | None]:
    raw = _json_field(record, "time_bucket", None, definition_id=definition_id)
    if not raw:
        return None, "none", None
    if not isinstance(raw, dict):
        raise DefinitionError(f"{definition_id}: time_bucket must be a JSON object")

    field_name = (raw.get("field") or "").strip()
    if not field_name:
        raise DefinitionError(f"{definition_id}: time_bucket.field is required when time_bucket is set")

    granularity = (raw.get("granularity") or "none").strip().lower()
    if granularity not in _TIME_BUCKET_GRANULARITIES:
        raise DefinitionError(
            f"{definition_id}: time_bucket.granularity must be one of {sorted(_TIME_BUCKET_GRANULARITIES)}"
        )

    source_field = source_fields.get(field_name)
    source_type = (source_field or {}).get("type")
    if source_type not in ("date", "datetime"):
        raise DefinitionError(
            f"{definition_id}: time_bucket.field '{field_name}' must be a date/datetime field on {source_collection}"
        )
    return field_name, granularity, source_type


def _parse_metrics(
    record: Mapping[str, str],
    *,
    definition_id: str,
    source_collection: str,
    source_fields: dict[str, dict[str, Any]],
    group_by: tuple[str, ...],
) -> tuple[Metric, ...]:
    raw = _json_field(record, "metrics", [], definition_id=definition_id)
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise DefinitionError(f"{definition_id}: metrics must be a JSON list")

    reserved_names = set(_RESERVED_TARGET_FIELD_NAMES) | set(group_by)
    metrics: list[Metric] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise DefinitionError(f"{definition_id}: each metrics entry must be a JSON object")

        op = str(entry.get("op") or "").strip().lower()
        if op not in _METRIC_OPS:
            raise DefinitionError(f"{definition_id}: metric op must be one of {sorted(_METRIC_OPS)}")

        field_name = entry.get("field")
        if op == "count":
            as_name = str(entry.get("as") or "count").strip()
            metric = Metric(op=op, field=None, as_name=as_name, source_type=None, target_type="integer")
        else:
            if not isinstance(field_name, str) or not field_name:
                raise DefinitionError(f"{definition_id}: metric op '{op}' requires a field")
            source_field = source_fields.get(field_name)
            source_type = (source_field or {}).get("type")
            if source_type not in ("integer", "number"):
                raise DefinitionError(
                    f"{definition_id}: metric field '{field_name}' must be numeric "
                    f"(integer/number) on {source_collection}"
                )
            as_name = str(entry.get("as") or f"{field_name}_{op}").strip()
            target_type = "number" if op == "avg" else source_type
            metric = Metric(op=op, field=field_name, as_name=as_name, source_type=source_type, target_type=target_type)

        if not metric.as_name:
            raise DefinitionError(f"{definition_id}: metric 'as' name cannot be blank")
        if metric.as_name in reserved_names:
            raise DefinitionError(
                f"{definition_id}: metric name '{metric.as_name}' collides with id/bucket_start/"
                "computed_at or a group_by field"
            )
        reserved_names.add(metric.as_name)
        metrics.append(metric)

    return tuple(metrics)


def _parse_min_group_size(record: Mapping[str, str], *, definition_id: str) -> int | None:
    raw = (record.get("min_group_size") or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise DefinitionError(f"{definition_id}: min_group_size must be an integer") from exc
    if value < 1:
        raise DefinitionError(f"{definition_id}: min_group_size must be >= 1")
    return value


def _json_field(record: Mapping[str, str], name: str, default: Any, *, definition_id: str) -> Any:
    raw = record.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DefinitionError(f"{definition_id}: {name} is not valid JSON: {exc}") from exc


def _truthy(value: Any, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# --- Derived target schema -------------------------------------------------

def derive_target_schema(config: RollupConfig) -> dict[str, Any]:
    """Return the target collection's schema, generated from ``config``.

    Shape rules per 14's "Derived target schema" section: one plain
    ``text`` field per ``group_by`` entry (a source enum/relation field
    still becomes plain text on the target -- the target stores the
    value, not a live pointer); ``bucket_start`` only when a time bucket
    is declared, typed ``date`` for day/week/month or ``datetime`` when
    the source field is ``datetime`` and granularity is ``none``; one
    ``"computed": true`` field per metric, named by ``as``; and an
    unconditional ``"computed": true`` ``computed_at``, present regardless
    of refresh mode. ``views.list_mode: "table"`` plus ``list_fields``
    covering group-by + bucket + metrics are set automatically -- the
    payoff named in 14's doctrine, a rollup target renders with zero page
    code.
    """
    fields: list[dict[str, Any]] = [{"name": "id"}]
    for name in config.group_by:
        fields.append({"name": name, "type": "text"})

    if config.time_bucket_field:
        bucket_type = "date"
        if config.time_bucket_granularity == "none" and config.time_bucket_source_type == "datetime":
            bucket_type = "datetime"
        fields.append({"name": "bucket_start", "type": bucket_type})

    for metric in config.metrics:
        fields.append({"name": metric.as_name, "type": metric.target_type, "computed": True})

    fields.append({"name": "computed_at", "type": "datetime", "computed": True})

    list_fields = list(config.group_by)
    if config.time_bucket_field:
        list_fields.append("bucket_start")
    list_fields.extend(metric.as_name for metric in config.metrics)

    return {
        "name": config.target_collection,
        "title": config.name,
        "description": (
            f"Rollup target for definition '{config.definition_id}' -- generated, "
            "never hand-edited. See plan/vocabulary/14-rollup-spec.md."
        ),
        "storage": object_schemas.STORAGE_CLASSIC,
        "fields": fields,
        "views": {"list_mode": "table", "list_fields": list_fields},
    }


def _next_schema_version(target_collection: str, *, base_dir: Any) -> int:
    """One more than the target's current schema version, or 1 when there
    isn't one yet -- 14's Upgrade Posture calls the departure from Rule 1
    a "regenerate": "bump the target schema, force an immediate full
    recompute." Every recompute already regenerates and replaces the
    schema (see module docstring); this just makes that regeneration a
    literal version bump rather than a silent same-number overwrite.
    """
    try:
        existing = object_schemas.get_schema(target_collection, base_dir=base_dir)
    except object_schemas.SchemaNotFoundError:
        return 1
    try:
        return int(existing.get("version") or 0) + 1
    except (TypeError, ValueError):
        return 1


# --- Time bucketing -------------------------------------------------

def _truncate_bucket(raw_value: str, granularity: str) -> str | None:
    """Truncate one source field value to its bucket start.

    No timezone conversion -- 14 is explicit that bucketing "truncates
    the field's value to the bucket start ... in the field's own timezone
    convention," matching docs/schema-forms.md's date/datetime types,
    which are stored and compared as given. Returns None for a value that
    doesn't parse as a date/datetime (caller drops that row from grouping
    rather than crashing the whole definition over one bad row).
    """
    if granularity == "none":
        return raw_value

    parsed = _parse_date_or_datetime(raw_value)
    if parsed is None:
        return None

    if granularity == "day":
        bucket = parsed
    elif granularity == "week":
        bucket = parsed - timedelta(days=parsed.weekday())  # ISO week starts Monday
    elif granularity == "month":
        bucket = parsed.replace(day=1)
    else:  # pragma: no cover -- parse_definition already validated this
        raise DefinitionError(f"unknown time_bucket granularity: {granularity}")
    return bucket.isoformat()


def _parse_date_or_datetime(raw_value: str) -> date | None:
    text = (raw_value or "").strip()
    if not text:
        return None
    try:
        if "T" in text or " " in text:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        return date.fromisoformat(text)
    except ValueError:
        return None


# --- Grouping and metrics -------------------------------------------------

def _group_rows(
    rows: list[dict[str, str]], config: RollupConfig
) -> dict[tuple[str | None, tuple[str, ...]], list[dict[str, str]]]:
    """Group already-filtered source rows by (bucket_start, group_key).

    A row whose time_bucket field is blank, or doesn't parse as a
    date/datetime, is dropped from grouping entirely (it can't be
    honestly bucketed) rather than raising and failing the whole
    definition over one bad row -- consistent with this pass's isolation
    posture elsewhere.
    """
    groups: dict[tuple[str | None, tuple[str, ...]], list[dict[str, str]]] = {}
    for row in rows:
        if config.time_bucket_field:
            raw_bucket_value = row.get(config.time_bucket_field)
            if not raw_bucket_value:
                continue
            bucket = _truncate_bucket(raw_bucket_value, config.time_bucket_granularity)
            if bucket is None:
                continue
        else:
            bucket = None
        group_key = tuple(row.get(name, "") for name in config.group_by)
        groups.setdefault((bucket, group_key), []).append(row)
    return groups


def _compute_metric(metric: Metric, rows: list[dict[str, str]]) -> str:
    """Compute one metric's value over one group's rows.

    Integer-exact per 14's float-accumulation note: an integer-typed
    source field is summed/averaged with Python's arbitrary-precision
    ints, never floats, so cents arithmetic never accumulates rounding
    error. ``avg`` is always sum/count from that group's own exact sum
    and count, computed fresh from the full row set every recompute
    (never a running average) -- v1's scheduled-only posture means the
    complete row list is always available, so there is no need to persist
    an intermediate sum/count pair the way an incremental fast path
    eventually would (see module docstring).
    """
    if metric.op == "count":
        return str(len(rows))

    present = [row.get(metric.field) for row in rows if row.get(metric.field) not in (None, "")]

    if metric.op in ("min", "max"):
        if not present:
            return ""
        numbered = [(_parse_metric_number(value, metric.source_type), value) for value in present]
        chosen = min(numbered) if metric.op == "min" else max(numbered)
        return chosen[1]

    if metric.op == "sum":
        if metric.source_type == "integer":
            return str(sum(int(value) for value in present))
        return repr(sum(float(value) for value in present))

    if metric.op == "avg":
        if not present:
            return ""
        if metric.source_type == "integer":
            total: float = sum(int(value) for value in present)
        else:
            total = sum(float(value) for value in present)
        return repr(total / len(present))

    raise DefinitionError(f"unknown metric op: {metric.op}")  # pragma: no cover -- pre-validated


def _parse_metric_number(value: str, source_type: str | None) -> float:
    return float(int(value)) if source_type == "integer" else float(value)


def _target_row_id(definition_id: str, key: tuple[str | None, tuple[str, ...]]) -> str:
    """A deterministic target row id from (definition, group key, bucket).

    14's Storage section requires this so "a recompute or an incremental
    upsert always lands on the same row rather than accumulating
    duplicates." Hashed (not concatenated) because group key values are
    arbitrary source data that may contain characters unsafe in a record
    id (``object_records._RECORD_ID_RE``); the hash is stable across
    recomputes since it is a pure function of the definition id, the
    bucket, and the group key values, in the same field order every time.
    """
    bucket, group_key = key
    canonical = json.dumps([definition_id, bucket, list(group_key)], separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f"rlp_{digest}"


def _build_target_row(
    config: RollupConfig,
    key: tuple[str | None, tuple[str, ...]],
    rows: list[dict[str, str]],
    computed_at: str,
) -> dict[str, str]:
    bucket, group_key = key
    row: dict[str, str] = {"id": _target_row_id(config.definition_id, key)}
    for name, value in zip(config.group_by, group_key, strict=True):
        row[name] = value
    if config.time_bucket_field:
        row["bucket_start"] = bucket or ""
    for metric in config.metrics:
        row[metric.as_name] = _compute_metric(metric, rows)
    row["computed_at"] = computed_at
    return row


# --- Compute -------------------------------------------------

def compute_rollup(
    definition: Mapping[str, str], *, base_dir: Any, roots: Any = None
) -> dict[str, Any]:
    """Full recompute for one ``rollup_definitions`` record.

    Reads ``source_collection`` outside the permission engine's row
    filters -- daemon posture, same as ``object_records.compact_collection``
    (14's Permissions Posture: "it must see every row, across every
    owner, to aggregate correctly into one admin-controlled target").
    Regenerates and replaces the target's derived schema, then reconciles
    the target collection's rows to exactly the newly computed set (see
    module docstring for why "reconcile via the record API" is how this
    codebase expresses "full rewrite" without a bulk-replace primitive).

    Raises ``DefinitionError`` for a malformed definition, and lets
    ``object_schemas.SchemaNotFoundError`` / ``object_collections.
    CollectionNotFoundError`` propagate when ``source_collection`` is
    missing -- the caller (``object_daemon.process_rollups``) isolates
    each definition's failure in its own try/except, one bad definition
    never stopping the rest of the pass (14's Degradation section).
    """
    config = parse_definition(definition, base_dir=base_dir, roots=roots)

    source_records = object_records.read_collection_records(
        config.source_collection, base_dir=base_dir, roots=roots
    )
    filtered = (
        object_records.filter_records(source_records, config.filter)
        if config.filter
        else list(source_records)
    )

    groups = _group_rows(filtered, config)

    computed_at = _now_iso()
    target_rows: list[dict[str, str]] = []
    suppressed = 0
    for key, rows in groups.items():
        if config.min_group_size is not None and len(rows) < config.min_group_size:
            suppressed += 1
            continue
        target_rows.append(_build_target_row(config, key, rows, computed_at))

    schema_payload = derive_target_schema(config)
    schema_payload["version"] = _next_schema_version(config.target_collection, base_dir=base_dir)
    object_schemas.replace_schema(config.target_collection, schema_payload, base_dir=base_dir)

    reconciled = _reconcile_target_rows(
        config.target_collection, target_rows, base_dir=base_dir, roots=roots
    )

    return {
        "definition_id": config.definition_id,
        "target_collection": config.target_collection,
        "groups": len(target_rows),
        "suppressed": suppressed,
        "created": reconciled["created"],
        "updated": reconciled["updated"],
        "deleted": reconciled["deleted"],
        "computed_at": computed_at,
    }


def _reconcile_target_rows(
    collection: str, new_rows: list[dict[str, str]], *, base_dir: Any, roots: Any = None
) -> dict[str, int]:
    """Sync ``collection`` to exactly ``new_rows`` by id: create the new
    ones, update the ones that already existed, delete whatever's left --
    a full recompute's atomic rewrite naturally drops any row whose group
    no longer exists (14: "no explicit 'delete stale rows' step is
    needed, the rewrite IS that step"); this is that step, done per-row
    through the public record API.

    Every row is written unconditionally, including one whose metric
    values are unchanged since the last pass: ``computed_at`` always
    advances, honestly reflecting "just reconfirmed as of now" rather
    than silently reusing a stale timestamp because nothing else moved.
    """
    existing_ids: set[str] = set()
    if object_records.collection_has_records(collection, base_dir=base_dir):
        existing_ids = {
            row["id"]
            for row in object_records.read_collection_records(collection, base_dir=base_dir, roots=roots)
        }

    created = updated = 0
    seen_ids: set[str] = set()
    for row in new_rows:
        seen_ids.add(row["id"])
        if row["id"] in existing_ids:
            object_records.update_collection_record(
                collection, row["id"], row,
                base_dir=base_dir, roots=roots,
                actor=ROLLUP_ACTOR, allow_computed_submission=True,
            )
            updated += 1
        else:
            object_records.create_collection_record(
                collection, row,
                base_dir=base_dir, roots=roots,
                actor=ROLLUP_ACTOR, allow_computed_submission=True,
            )
            created += 1

    deleted = 0
    for stale_id in existing_ids - seen_ids:
        object_records.delete_collection_record(
            collection, stale_id, base_dir=base_dir, roots=roots, actor=ROLLUP_ACTOR,
        )
        deleted += 1

    return {"created": created, "updated": updated, "deleted": deleted}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
