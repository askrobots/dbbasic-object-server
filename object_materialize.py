"""Materialize block: declared generation jobs that CREATE new records.

Implements ``plan/vocabulary/61-materialize-spec.md``. Sibling to
``object_rollups.py`` (14-rollup-spec.md): both are declarative,
schema/config-parameterized, ride ``object_daemon.py``'s poll loop as one
more pass, and both write to a collection nothing else writes to. The
difference that makes this a distinct block rather than a rollup variant:
a rollup recompute REPLACES the whole answer every time (naturally
idempotent, disposable output); a materialize pass CREATES new records
other records come to depend on (a journal, a depreciation posting, a
seeded task), so a double-run is not "stale," it is a duplicate --
genuinely destructive. Everything below exists to make "generate this
period's output" idempotent by construction rather than by discipline.

**The three worked shapes, and how one mechanism spans them (61's Purpose
and Coverage):**

1. Recurring journals (``fin_recurring`` -> ``fin_journals`` +
   ``fin_journal_lines``, ``trigger.mode: "scheduled"``): a header row per
   due period, its child lines parsed VERBATIM from the source row's own
   ``child_source_field`` JSON blob (already shaped like the child
   schema's rows) -- see ``_build_child_rows_from_template``.
2. Depreciation (``products`` -> ``fin_journals`` + ``fin_journal_lines``,
   ``trigger.mode: "scheduled_fixed"``): 61's own Open Questions flags
   this as genuinely underspecified ("this spec assumes a
   debit_account_id/credit_account_id pair added to the definition
   record... flagged as an implementation-time field, not fully specified
   here"). This module's resolution, decided at implementation time: when
   a definition declares ``child_collection`` but NO
   ``child_source_field``, that absence IS the signal for the
   "synthesized two-line depreciation shape" -- the definition MUST then
   carry ``debit_account_id``/``credit_account_id`` plus a reserved
   ``mapping.amount`` entry whose op is ``depreciation_amount``; exactly
   two child lines are synthesized (debit the expense account, credit
   accumulated depreciation, same amount both lines) rather than parsed
   from source-row JSON. See ``_build_depreciation_child_rows``. Only
   ``straight_line`` ships; a definition requesting ``declining`` is
   rejected at parse time (``_validate_mapping_entry``), never silently
   mis-computed -- 61's Purpose and Coverage names declining-balance as
   the one worked case that brushes the escalate-to-custom-object line.
3. Template execution / CreateWork (``tasks`` self-referential,
   ``trigger.mode: "event"``, ``output_collection == source_collection``):
   detected structurally (output equals source) rather than by a separate
   flag. This is an UPDATE to the SAME row, gated by the fill-only-if-empty
   rule (61's Events section: "must never clobber a field the record's
   own creator already set explicitly"). 61's own worked example 3
   describes the CreateWork mechanism as a template-relation hop (fetch
   the ``templates`` row a ``template_id``-shaped field points at, parse
   its ``default_values`` JSON, fill empty fields) -- notably NOT
   expressed through this block's ordinary ``mapping`` vocabulary, since
   ``mapping`` is explicitly forbidden from cross-collection joins
   (Out of Scope). This module supports BOTH: an ordinary
   ``mapping``-driven fill (works for any CreateWork-shaped definition,
   generic, no relation lookup) applied first, then an opportunistic
   template-default-values hop (``_apply_template_defaults``, worked
   example 3's exact mechanism) applied second -- both fill-only-if-empty,
   so whichever gets there first for a given field wins and the other is
   a no-op for that field. This is a deliberate interpretation of an
   underspecified corner, documented here rather than by omission.

**Crash-safety -- the honest, non-transactional sequencing (61's Storage
section, read literally, implemented literally):**

    1. Compute the ENTIRE output in memory first, including the balance
       check (exact integer cents, never float), BEFORE any write touches
       disk. Unequal debit/credit sums -> abort, zero rows written, zero
       risk of a half-posted journal (``BalanceCheckFailed``, raised
       before ``_generate_new_record`` writes anything).
    2. Write child rows FIRST. Each child's own id is ALSO deterministic
       (``{header_id}_line_{n}``), so a duplicate-id create from a
       resumed partial run is a harmless, caught, skipped no-op
       (``object_records.DuplicateRecordIdError``), never fatal.
    3. Write the header LAST, at its deterministic id
       (``idempotency_key``). The header's EXISTENCE, not the children's,
       is what "this period is generated" means -- the daemon's due-check
       (``_record_exists``) queries ``output_collection`` (the header
       collection) for that id, never the child collection. A crash
       between the last child write and the header write leaves inert,
       orphaned, headerless child rows -- an accepted, bounded, ACTUAL
       failure mode (not swept under the rug): the next pass sees no
       header, re-attempts, re-writes any still-missing children
       (idempotent by id), and writes the header this time. This module
       does NOT claim cross-collection atomicity, because there isn't
       any -- see 61's Storage section for the full honest accounting of
       why this ordering is what "detectably safe rather than silently
       wrong" actually buys, and what it does not.

**Scheduled due-set, recomputed from scratch every pass, same stance
14-rollup-spec.md takes for its own scheduled recompute:** the daemon
pass does NOT trust a source row's own ``next_run``/``last_run`` (or this
definition's own ``last_run_at``) as the mechanism of truth. Every pass
recomputes which periods SHOULD exist between a row's anchor point and
now, and checks the deterministic header id's existence in
``output_collection`` for each -- missing means generate, present means
skip, never an error. ``last_run_at``/a source row's own
``next_run``/``last_run`` are stamped afterward purely as a freshness
DISPLAY, never read back as the correctness gate.

**generated_from -- built, not left open.** 61's Open Questions flags this
as "arguably should just be built." When ``output_collection``'s schema
declares a plain ``generated_from`` field, every top-level generated
record (and CreateWork's in-place fill, fill-only-if-empty like every
other field) is stamped with a small JSON blob (``definition_id``,
``source_id``, ``period_start``) -- additive, opt-in per output schema
(a schema that hasn't added the field simply never receives it, no
error), first-class provenance rather than only inferable from the
deterministic id's string shape.
"""
from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

import object_collections
import object_records
import object_schemas

MATERIALIZE_DEFINITIONS_COLLECTION = "materialize_definitions"
FEATURE_FLAGS_COLLECTION = "feature_flags"
MATERIALIZE_ENABLED_FLAG = "materialize_enabled"
DEFAULT_ACTOR = "daemon:materialize"

_GRANULARITIES = frozenset({"daily", "weekly", "monthly", "quarterly", "yearly"})
_TRIGGER_MODES = frozenset({"scheduled", "scheduled_fixed", "event"})
_MAPPING_OP_KEYS = frozenset({"from", "from_period", "literal", "template", "if", "depreciation_amount"})
_TEMPLATE_TOKEN_RE = re.compile(r"\{([a-zA-Z0-9_.]+)\}")

# Safety valve against a malformed/huge span producing an unbounded period
# loop (e.g. an anchor_field decades in the past with a daily granularity).
# Not part of the spec; a defensive bound only.
_MAX_PERIODS_PER_ROW = 100_000


class DefinitionError(ValueError):
    """Raised when a materialize_definitions record cannot be generated as-is.

    Covers malformed JSON sub-fields, violations of 61's own doctrine (a
    filter value that isn't a flat scalar, an unrecognized mapping op,
    ``declining`` depreciation requested), and per-row generation problems
    (unparseable child_source_field JSON, a missing anchor field). The
    daemon pass (object_daemon.process_materializations) and
    materialize_run both catch this per-definition or per-row, matching
    object_rollups.DefinitionError's isolation posture.
    """


class MissingCollectionError(DefinitionError):
    """A definition names a source/output/child collection that doesn't exist.

    Distinguished from a generic DefinitionError so the daemon can log it
    exactly once per process (61's Degradation: "warned-once-per-process,
    matching process_stale_transitions' _WARNED_UNKNOWN_AUTO_TRANSITION_
    COLLECTIONS pattern exactly") rather than re-logging every poll.
    """

    def __init__(self, collection: str, role: str):
        super().__init__(f"{role} collection '{collection}' does not exist")
        self.collection = collection
        self.role = role


class BalanceCheckFailed(DefinitionError):
    """A generation's computed child rows don't balance -- zero rows written.

    Raised BEFORE any write touches disk (see module docstring's
    crash-safety section); the caller's per-row isolation logs this and
    retries next pass, since nothing was written and the deterministic id
    therefore never came to exist.
    """


@dataclass(frozen=True)
class Period:
    """One computed generation period (scheduled/scheduled_fixed only).

    ``index``/``total`` are the 1-based period number and the definition's
    total period cap -- present only for ``scheduled_fixed`` (depreciation's
    "which month is this, out of how many"), None for ``scheduled``.
    """

    start: date
    end: date
    label: str
    index: int | None
    total: int | None


@dataclass(frozen=True)
class MaterializeConfig:
    definition_id: str
    name: str
    source_collection: str
    source_filter: dict[str, Any]
    trigger_mode: str  # "scheduled" | "scheduled_fixed" | "event"
    trigger_interval_seconds: int | None
    # scheduled mode
    anchor_field: str | None
    frequency_field: str | None  # names a FIELD on the source row (e.g. "frequency")
    # scheduled_fixed mode
    start_field: str | None
    granularity: str | None  # a literal fixed granularity (e.g. "monthly")
    periods_field: str | None  # names a FIELD on the source row (e.g. "useful_life_months")
    # output
    output_collection: str
    child_collection: str | None
    child_source_field: str | None
    child_link_field: str | None
    idempotency_key: str
    mapping: dict[str, Any]
    balance_check: dict[str, str] | None
    debit_account_id: str | None
    credit_account_id: str | None
    synthesized_amount_entry: dict[str, Any] | None
    actor: str
    enabled: bool
    block: bool
    stamp_generated_from: bool


# --- Feature flag / due gates -------------------------------------------------

def materialize_pass_enabled(*, base_dir: Any) -> bool:
    """Block-wide kill switch, ``<block>_enabled`` convention, mirroring
    object_rollups.rollup_pass_enabled exactly (same defaults, same
    reasoning): default ON, a missing/unreadable feature_flags collection
    or a blank value both resolve to "on"; only an explicit off/false/0/no
    value turns the pass off. Unlike 14's posture (stale-but-valid reads
    keep serving), 61's Degradation is explicit that here "off means off,
    full stop, for BOTH the scheduled and manual paths" -- callers (the
    daemon pass, materialize_run, materialize_seed's EVENT handler) are
    each responsible for checking this before doing anything, since there
    is no "serve what's on disk" fallback for a write-side block.
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
        if row.get("flag") == MATERIALIZE_ENABLED_FLAG:
            value = (row.get("value") or "").strip().lower()
            if not value:
                return True
            return value not in {"off", "false", "0", "no"}
    return True


def is_definition_enabled(record: Mapping[str, str]) -> bool:
    """Per-definition pause -- independent of the block-wide flag. Blank
    defaults to enabled, matching object_rollups.is_definition_enabled.
    """
    return _truthy(record.get("enabled"), default=True)


def is_definition_blocked(record: Mapping[str, str]) -> bool:
    """The harder stop (61's Degradation): ``block: true`` refuses even
    ``materialize_run``'s manual path with a distinct error, unlike
    ``enabled: false`` (pause, resume later without thinking).
    """
    return _truthy(record.get("block"), default=False)


def is_definition_due(record: Mapping[str, str], *, now: datetime | None = None) -> bool:
    """SCHEDULED/SCHEDULED_FIXED due gate: ``last_run_at`` +
    ``trigger.interval_seconds``, both read straight off the raw record --
    mirrors object_rollups.is_definition_due's structure exactly. A
    never-run definition (``last_run_at`` blank) is always due. This gate
    only decides whether the daemon PASS attempts a definition this poll;
    it is not the correctness mechanism (that's the deterministic header
    id, see module docstring) -- a due-but-already-generated period is
    simply reported as skipped, not an error.
    """
    now = now or datetime.now(timezone.utc)
    last_run_raw = (record.get("last_run_at") or "").strip()
    if not last_run_raw:
        return True
    trigger = _json_field(record, "trigger", None, definition_id=record.get("id") or "<unknown>")
    interval = _positive_int(trigger.get("interval_seconds")) if isinstance(trigger, dict) else None
    if interval is None:
        return True
    last_run = _parse_iso(last_run_raw)
    if last_run is None:
        return True
    return (now - last_run).total_seconds() >= interval


# --- Definition parsing -------------------------------------------------

def parse_definition(
    record: Mapping[str, str], *, base_dir: Any, roots: Any = None
) -> MaterializeConfig:
    """Validate and normalize one ``materialize_definitions`` row.

    Raises ``DefinitionError``/``MissingCollectionError`` for anything
    malformed or referencing a collection that doesn't exist yet -- the
    daemon (object_daemon.process_materializations) and materialize_run
    both isolate this per-definition, never letting one bad definition
    stop any other (61's Events/Degradation).
    """
    definition_id = (record.get("id") or "").strip()
    if not definition_id:
        raise DefinitionError("materialize definition is missing an id")

    name = (record.get("name") or "").strip() or definition_id

    source_collection = (record.get("source_collection") or "").strip()
    if not source_collection or not object_collections.validate_collection_name(source_collection):
        raise DefinitionError(f"{definition_id}: source_collection is required and must be a valid collection name")
    try:
        object_schemas.get_schema(source_collection, base_dir=base_dir, roots=roots)
    except object_schemas.SchemaNotFoundError:
        raise MissingCollectionError(source_collection, "source") from None

    source_filter = _parse_filter(record, definition_id=definition_id)

    trigger = _json_field(record, "trigger", None, definition_id=definition_id)
    if not isinstance(trigger, dict) or not trigger:
        raise DefinitionError(f"{definition_id}: trigger is required and must be a JSON object")
    trigger_mode = str(trigger.get("mode") or "").strip()
    if trigger_mode not in _TRIGGER_MODES:
        raise DefinitionError(f"{definition_id}: trigger.mode must be one of {sorted(_TRIGGER_MODES)}")

    trigger_interval_seconds: int | None = None
    anchor_field = frequency_field = start_field = granularity = periods_field = None

    if trigger_mode == "scheduled":
        trigger_interval_seconds = _positive_int(trigger.get("interval_seconds"))
        if trigger_interval_seconds is None:
            raise DefinitionError(f"{definition_id}: trigger.interval_seconds is required and must be a positive integer")
        frequency_field = str(trigger.get("period_field") or "").strip()
        if not frequency_field:
            raise DefinitionError(
                f"{definition_id}: trigger.period_field (the source row field naming its own "
                "frequency, e.g. \"frequency\") is required for scheduled mode"
            )
        anchor_field = str(trigger.get("anchor_field") or "").strip()
        if not anchor_field:
            raise DefinitionError(f"{definition_id}: trigger.anchor_field is required for scheduled mode")
    elif trigger_mode == "scheduled_fixed":
        trigger_interval_seconds = _positive_int(trigger.get("interval_seconds"))
        if trigger_interval_seconds is None:
            raise DefinitionError(f"{definition_id}: trigger.interval_seconds is required and must be a positive integer")
        granularity = str(trigger.get("period_field") or "").strip().lower()
        if granularity not in _GRANULARITIES:
            raise DefinitionError(
                f"{definition_id}: trigger.period_field must be a fixed granularity, one of "
                f"{sorted(_GRANULARITIES)}, for scheduled_fixed mode"
            )
        start_field = str(trigger.get("start_field") or "").strip()
        if not start_field:
            raise DefinitionError(f"{definition_id}: trigger.start_field is required for scheduled_fixed mode")
        end_condition = trigger.get("end_condition")
        if not isinstance(end_condition, dict):
            raise DefinitionError(f"{definition_id}: trigger.end_condition is required for scheduled_fixed mode")
        periods_field = str(end_condition.get("periods_field") or "").strip()
        if not periods_field:
            raise DefinitionError(f"{definition_id}: trigger.end_condition.periods_field is required for scheduled_fixed mode")
    else:  # event
        on = str(trigger.get("on") or "").strip()
        if on != "record.created":
            raise DefinitionError(f"{definition_id}: trigger.on must be 'record.created' for event mode")

    output_collection = (record.get("output_collection") or "").strip()
    if not output_collection or not object_collections.validate_collection_name(output_collection):
        raise DefinitionError(f"{definition_id}: output_collection is required and must be a valid collection name")
    try:
        object_schemas.get_schema(output_collection, base_dir=base_dir, roots=roots)
    except object_schemas.SchemaNotFoundError:
        raise MissingCollectionError(output_collection, "output") from None

    child_collection = (record.get("child_collection") or "").strip() or None
    child_source_field = (record.get("child_source_field") or "").strip() or None
    child_link_field = (record.get("child_link_field") or "").strip() or None
    if child_collection:
        if not object_collections.validate_collection_name(child_collection):
            raise DefinitionError(f"{definition_id}: child_collection must be a valid collection name")
        try:
            object_schemas.get_schema(child_collection, base_dir=base_dir, roots=roots)
        except object_schemas.SchemaNotFoundError:
            raise MissingCollectionError(child_collection, "child") from None
        if not child_link_field:
            raise DefinitionError(f"{definition_id}: child_link_field is required when child_collection is set")

    idempotency_key = (record.get("idempotency_key") or "").strip()
    if not idempotency_key:
        raise DefinitionError(f"{definition_id}: idempotency_key is required")

    mapping_raw = _json_field(record, "mapping", {}, definition_id=definition_id)
    if mapping_raw is None:
        mapping_raw = {}
    if not isinstance(mapping_raw, dict):
        raise DefinitionError(f"{definition_id}: mapping must be a JSON object")
    mapping = dict(mapping_raw)
    for field_name, entry in mapping.items():
        _validate_mapping_entry(field_name, entry, definition_id=definition_id)

    balance_check_raw = _json_field(record, "balance_check", None, definition_id=definition_id)
    balance_check: dict[str, str] | None = None
    if balance_check_raw is not None:
        if not isinstance(balance_check_raw, dict):
            raise DefinitionError(f"{definition_id}: balance_check must be a JSON object")
        debit_field = str(balance_check_raw.get("debit_field") or "").strip()
        credit_field = str(balance_check_raw.get("credit_field") or "").strip()
        if not debit_field or not credit_field:
            raise DefinitionError(f"{definition_id}: balance_check requires debit_field and credit_field")
        if not child_collection:
            raise DefinitionError(f"{definition_id}: balance_check requires child_collection")
        balance_check = {"debit_field": debit_field, "credit_field": credit_field}

    debit_account_id = (record.get("debit_account_id") or "").strip() or None
    credit_account_id = (record.get("credit_account_id") or "").strip() or None

    synthesized_amount_entry: dict[str, Any] | None = None
    if child_collection and not child_source_field:
        # Absence of child_source_field alongside a present child_collection
        # is the synthesized depreciation-lines shape's own signal -- see
        # module docstring's worked example 2 discussion.
        amount_entry = mapping.pop("amount", None)
        if not isinstance(amount_entry, dict) or "depreciation_amount" not in amount_entry:
            raise DefinitionError(
                f"{definition_id}: a child_collection with no child_source_field is the "
                "synthesized depreciation-lines shape and requires "
                "mapping.amount = {'depreciation_amount': {...}}"
            )
        if not debit_account_id or not credit_account_id:
            raise DefinitionError(
                f"{definition_id}: the synthesized depreciation-lines shape requires "
                "debit_account_id and credit_account_id"
            )
        if not balance_check:
            raise DefinitionError(
                f"{definition_id}: the synthesized depreciation-lines shape requires "
                "balance_check (its debit_field/credit_field name the two generated "
                "lines' amount fields)"
            )
        synthesized_amount_entry = amount_entry

    actor = (record.get("actor") or "").strip() or DEFAULT_ACTOR
    enabled = _truthy(record.get("enabled"), default=True)
    block = _truthy(record.get("block"), default=False)

    stamp_generated_from = _schema_has_field(output_collection, "generated_from", base_dir=base_dir, roots=roots)

    return MaterializeConfig(
        definition_id=definition_id,
        name=name,
        source_collection=source_collection,
        source_filter=source_filter,
        trigger_mode=trigger_mode,
        trigger_interval_seconds=trigger_interval_seconds,
        anchor_field=anchor_field,
        frequency_field=frequency_field,
        start_field=start_field,
        granularity=granularity,
        periods_field=periods_field,
        output_collection=output_collection,
        child_collection=child_collection,
        child_source_field=child_source_field,
        child_link_field=child_link_field,
        idempotency_key=idempotency_key,
        mapping=mapping,
        balance_check=balance_check,
        debit_account_id=debit_account_id,
        credit_account_id=credit_account_id,
        synthesized_amount_entry=synthesized_amount_entry,
        actor=actor,
        enabled=enabled,
        block=block,
        stamp_generated_from=stamp_generated_from,
    )


def _validate_mapping_entry(field_name: str, entry: Any, *, definition_id: str) -> None:
    if not isinstance(entry, dict):
        raise DefinitionError(f"{definition_id}: mapping.{field_name} must be a JSON object")

    if "if" in entry:
        cond = entry.get("if")
        if not isinstance(cond, dict) or not isinstance(cond.get("source_field"), str) or not cond.get("source_field"):
            raise DefinitionError(f"{definition_id}: mapping.{field_name}.if requires a source_field")
        if "equals" not in cond:
            raise DefinitionError(f"{definition_id}: mapping.{field_name}.if requires 'equals'")
        if "then" not in entry or "else" not in entry:
            raise DefinitionError(f"{definition_id}: mapping.{field_name} with 'if' requires 'then' and 'else'")
        return

    if "depreciation_amount" in entry:
        op = entry["depreciation_amount"]
        if not isinstance(op, dict):
            raise DefinitionError(f"{definition_id}: mapping.{field_name}.depreciation_amount must be a JSON object")
        method = str(op.get("method") or "").strip()
        if method == "declining":
            raise DefinitionError(
                f"{definition_id}: mapping.{field_name} requests depreciation method 'declining', "
                "which is out of scope for v1 (straight_line only) -- see "
                "plan/vocabulary/61-materialize-spec.md's Open Questions"
            )
        if method != "straight_line":
            raise DefinitionError(f"{definition_id}: mapping.{field_name}.depreciation_amount.method must be 'straight_line'")
        for key in ("cost_field", "salvage_field", "life_field"):
            value = op.get(key)
            if not isinstance(value, str) or not value:
                raise DefinitionError(f"{definition_id}: mapping.{field_name}.depreciation_amount.{key} is required")
        return

    if "from" in entry:
        if not isinstance(entry["from"], str) or not entry["from"]:
            raise DefinitionError(f"{definition_id}: mapping.{field_name}.from must name a source field")
        return

    if "from_period" in entry:
        if entry["from_period"] not in ("period_start", "period_end", "period_label"):
            raise DefinitionError(
                f"{definition_id}: mapping.{field_name}.from_period must be one of "
                "period_start/period_end/period_label"
            )
        return

    if "literal" in entry:
        return

    if "template" in entry:
        if not isinstance(entry["template"], str):
            raise DefinitionError(f"{definition_id}: mapping.{field_name}.template must be a string")
        return

    raise DefinitionError(
        f"{definition_id}: mapping.{field_name} must use one of from/from_period/literal/"
        "template/if/depreciation_amount"
    )


def _parse_filter(record: Mapping[str, str], *, definition_id: str) -> dict[str, Any]:
    raw = _json_field(record, "source_filter", {}, definition_id=definition_id)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise DefinitionError(f"{definition_id}: source_filter must be a flat JSON object")
    normalized: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, (dict, list)):
            raise DefinitionError(
                f"{definition_id}: source_filter.{key} must be a scalar -- materialize filters are "
                "flat equality only, ANDed, same as 14-rollup-spec.md's filter language"
            )
        if value is None:
            raise DefinitionError(f"{definition_id}: source_filter.{key} must not be null")
        if isinstance(value, bool):
            value = "true" if value else "false"
        normalized[key] = value
    return normalized


def _schema_has_field(collection: str, field_name: str, *, base_dir: Any, roots: Any = None) -> bool:
    try:
        schema = object_schemas.get_schema(collection, base_dir=base_dir, roots=roots)
    except object_schemas.SchemaNotFoundError:
        return False
    return any(field.get("name") == field_name for field in schema.get("fields", []))


# --- Period computation -------------------------------------------------

def _compute_scheduled_periods(
    source_row: Mapping[str, str], config: MaterializeConfig, *, now: date
) -> list[Period]:
    granularity = str(source_row.get(config.frequency_field) or "").strip().lower()
    if granularity not in _GRANULARITIES:
        raise DefinitionError(
            f"source row has an invalid or missing '{config.frequency_field}' "
            f"(must be one of {sorted(_GRANULARITIES)}); got {granularity!r}"
        )
    anchor_raw = source_row.get(config.anchor_field) or ""
    origin = _parse_date(anchor_raw)
    if origin is None:
        origin = _parse_date(source_row.get("created_at") or "")
    if origin is None:
        raise DefinitionError(
            f"source row has no usable '{config.anchor_field}' (nor created_at) to anchor periods"
        )
    return _step_periods(origin, granularity, now=now, cap=None)


def _compute_scheduled_fixed_periods(
    source_row: Mapping[str, str], config: MaterializeConfig, *, now: date
) -> list[Period]:
    origin = _parse_date(source_row.get(config.start_field) or "")
    if origin is None:
        raise DefinitionError(f"source row has no usable '{config.start_field}' to anchor periods")
    cap = _positive_int(source_row.get(config.periods_field))
    if cap is None:
        raise DefinitionError(f"source row's '{config.periods_field}' must be a positive integer")
    return _step_periods(origin, config.granularity, now=now, cap=cap)


def _step_periods(origin: date, granularity: str, *, now: date, cap: int | None) -> list[Period]:
    periods: list[Period] = []
    cursor = origin
    index = 0
    guard = 0
    while cursor <= now:
        index += 1
        if cap is not None and index > cap:
            break
        period_end = _advance_date(cursor, granularity, 1)
        periods.append(
            Period(
                start=cursor,
                end=period_end,
                label=_period_label(cursor, granularity),
                index=index if cap is not None else None,
                total=cap,
            )
        )
        cursor = period_end
        guard += 1
        if guard >= _MAX_PERIODS_PER_ROW:
            break
    return periods


def _due_periods_for_row(
    config: MaterializeConfig, source_row: Mapping[str, str], *, now: date
) -> list[Period | None]:
    if config.trigger_mode == "event":
        return [None]
    if config.trigger_mode == "scheduled":
        return _compute_scheduled_periods(source_row, config, now=now)
    if config.trigger_mode == "scheduled_fixed":
        return _compute_scheduled_fixed_periods(source_row, config, now=now)
    raise DefinitionError(f"unknown trigger mode: {config.trigger_mode}")  # pragma: no cover -- pre-validated


def _advance_date(d: date, granularity: str, steps: int = 1) -> date:
    if granularity == "daily":
        return d + timedelta(days=steps)
    if granularity == "weekly":
        return d + timedelta(weeks=steps)
    if granularity == "monthly":
        return _add_months(d, steps)
    if granularity == "quarterly":
        return _add_months(d, steps * 3)
    if granularity == "yearly":
        return _add_months(d, steps * 12)
    raise DefinitionError(f"unknown granularity: {granularity}")  # pragma: no cover -- pre-validated


def _add_months(d: date, months: int) -> date:
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _period_label(period_start: date, granularity: str) -> str:
    if granularity == "daily":
        return period_start.isoformat()
    if granularity == "weekly":
        iso_year, iso_week, _ = period_start.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if granularity == "monthly":
        return f"{period_start.year:04d}-{period_start.month:02d}"
    if granularity == "quarterly":
        quarter = (period_start.month - 1) // 3 + 1
        return f"{period_start.year}-Q{quarter}"
    if granularity == "yearly":
        return str(period_start.year)
    raise DefinitionError(f"unknown granularity: {granularity}")  # pragma: no cover -- pre-validated


def _parse_date(value: Any) -> date | None:
    text = (value or "").strip() if isinstance(value, str) else ""
    if not text:
        return None
    try:
        if "T" in text or " " in text:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        return date.fromisoformat(text)
    except ValueError:
        return None


# --- Mapping vocabulary -------------------------------------------------

def _eval_entry(
    field_name: str, entry: Mapping[str, Any], *, source_row: Mapping[str, str],
    period: Period | None, definition_id: str,
) -> str:
    if "from" in entry:
        return _stringify(source_row.get(entry["from"], ""))

    if "from_period" in entry:
        if period is None:
            raise DefinitionError(
                f"{definition_id}: mapping.{field_name} uses from_period but this generation "
                "has no period (event/manual single-shot/CreateWork)"
            )
        key = entry["from_period"]
        if key == "period_start":
            return period.start.isoformat()
        if key == "period_end":
            return period.end.isoformat()
        if key == "period_label":
            return period.label
        raise DefinitionError(  # pragma: no cover -- pre-validated
            f"{definition_id}: mapping.{field_name}.from_period must be period_start/period_end/period_label"
        )

    if "literal" in entry:
        return _stringify(entry["literal"])

    if "template" in entry:
        return _render_template(
            entry["template"], source_row=source_row, period=period,
            definition_id=definition_id, field_name=field_name,
        )

    if "if" in entry:
        cond = entry["if"]
        actual = source_row.get(cond["source_field"])
        matches = _loose_equals(actual, cond["equals"])
        return _stringify(entry["then"] if matches else entry["else"])

    if "depreciation_amount" in entry:
        return _eval_depreciation(
            entry["depreciation_amount"], source_row, period,
            definition_id=definition_id, field_name=field_name,
        )

    raise DefinitionError(f"{definition_id}: mapping.{field_name} uses an unrecognized op")  # pragma: no cover


def _loose_equals(actual: Any, expected: Any) -> bool:
    if isinstance(expected, bool):
        expected_str = "true" if expected else "false"
    else:
        expected_str = str(expected)
    return str(actual if actual is not None else "") == expected_str


def _render_template(
    template: str, *, source_row: Mapping[str, str], period: Period | None,
    definition_id: str, field_name: str,
) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token == "period_label":
            if period is None:
                raise DefinitionError(
                    f"{definition_id}: mapping.{field_name} template uses {{period_label}} but "
                    "this generation has no period"
                )
            return period.label
        if token.startswith("source."):
            field = token[len("source."):]
            return _stringify(source_row.get(field, ""))
        raise DefinitionError(
            f"{definition_id}: mapping.{field_name} template references unknown placeholder '{{{token}}}'"
        )

    return _TEMPLATE_TOKEN_RE.sub(replace, template)


def _eval_depreciation(
    op: Mapping[str, Any], source_row: Mapping[str, str], period: Period | None,
    *, definition_id: str, field_name: str,
) -> str:
    if period is None or period.total is None or period.index is None:
        raise DefinitionError(
            f"{definition_id}: mapping.{field_name} uses depreciation_amount, which requires a "
            "scheduled_fixed trigger with an end_condition.periods_field cap"
        )
    monthly, remainder = _straight_line_split(op, source_row, definition_id=definition_id, field_name=field_name)
    if period.index == period.total:
        return str(monthly + remainder)
    return str(monthly)


def _straight_line_split(
    op: Mapping[str, Any], source_row: Mapping[str, str], *, definition_id: str, field_name: str,
) -> tuple[int, int]:
    cost = _to_cents(source_row.get(op["cost_field"]))
    salvage = _to_cents(source_row.get(op["salvage_field"]))
    life = _positive_int(source_row.get(op["life_field"]))
    if life is None:
        raise DefinitionError(
            f"{definition_id}: mapping.{field_name}.depreciation_amount.life_field must be a "
            "positive integer on the source row"
        )
    depreciable = cost - salvage
    monthly = depreciable // life
    remainder = depreciable - (monthly * life)
    return monthly, remainder


# --- Generation orchestration -------------------------------------------------

def generate_definition(
    record: Mapping[str, str], *, base_dir: Any, roots: Any = None, now: datetime | None = None,
) -> dict[str, Any]:
    """Parse ``record`` and run its full due-set generation.

    The SAME function the daemon's scheduled pass and ``materialize_run``'s
    manual path both call (61's Events: "the exact same idempotent,
    per-source-row function") -- for scheduled/scheduled_fixed this
    iterates every matching source row and every due period; for event
    mode (when called manually, since the daemon's scheduled pass never
    calls this on an event-mode definition -- see object_daemon.
    process_materializations) this iterates every matching source row
    once each, no period. Returns {"definition_id", "checked", "generated",
    "skipped_already_generated", "errors": [{"source_id", "period_start",
    "error"}, ...]}. Raises DefinitionError/MissingCollectionError for a
    malformed definition -- the caller isolates that per-definition; every
    per-row/per-period failure INSIDE a valid definition is instead
    captured in the returned "errors" list, never raised, so one bad row
    never stops any other (61's two-level isolation, mirroring
    process_stale_transitions).
    """
    config = parse_definition(record, base_dir=base_dir, roots=roots)
    return generate_config(config, base_dir=base_dir, roots=roots, now=now)


def generate_config(
    config: MaterializeConfig, *, base_dir: Any, roots: Any = None, now: datetime | None = None,
) -> dict[str, Any]:
    now_dt = now or datetime.now(timezone.utc)
    now_date = now_dt.date()

    source_rows = object_records.read_collection_records(config.source_collection, base_dir=base_dir, roots=roots)
    if config.source_filter:
        source_rows = object_records.filter_records(source_rows, config.source_filter)

    checked = generated = skipped = 0
    errors: list[dict[str, Any]] = []

    for row in source_rows:
        source_id = row.get("id") or "<unknown>"
        try:
            periods = _due_periods_for_row(config, row, now=now_date)
        except Exception as exc:
            errors.append({"source_id": source_id, "period_start": None, "error": str(exc)})
            continue

        for period in periods:
            checked += 1
            try:
                result = generate_one(config, row, period, base_dir=base_dir, roots=roots)
            except Exception as exc:
                errors.append({
                    "source_id": source_id,
                    "period_start": period.start.isoformat() if period is not None else None,
                    "error": str(exc),
                })
                continue

            if result["status"] == "generated":
                generated += 1
            elif result["status"] == "skipped_already_generated":
                skipped += 1
            else:  # pragma: no cover -- defensive, generate_one only returns these two
                errors.append({
                    "source_id": source_id,
                    "period_start": period.start.isoformat() if period is not None else None,
                    "error": f"unexpected status: {result['status']}",
                })

    return {
        "definition_id": config.definition_id,
        "checked": checked,
        "generated": generated,
        "skipped_already_generated": skipped,
        "errors": errors,
    }


def generate_one(
    config: MaterializeConfig, source_row: Mapping[str, str], period: Period | None,
    *, base_dir: Any, roots: Any = None,
) -> dict[str, Any]:
    """Generate (or confirm-already-generated) ONE (source row, period) pair.

    Branches structurally on ``output_collection == source_collection``
    (CreateWork: an in-place update, no new record) vs. every other shape
    (a new header record, optionally with children) -- see module
    docstring.
    """
    if config.output_collection == config.source_collection:
        return _generate_creatework(config, source_row, base_dir=base_dir, roots=roots)
    return _generate_new_record(config, source_row, period, base_dir=base_dir, roots=roots)


def _generate_new_record(
    config: MaterializeConfig, source_row: Mapping[str, str], period: Period | None,
    *, base_dir: Any, roots: Any = None,
) -> dict[str, Any]:
    header_id = _render_idempotency_key(config, source_row=source_row, period=period)

    # The ONLY due-check: does the header already exist? (module docstring)
    if _record_exists(config.output_collection, header_id, base_dir=base_dir, roots=roots):
        return {"status": "skipped_already_generated", "header_id": header_id}

    header_fields: dict[str, Any] = {"id": header_id}
    for field_name, entry in config.mapping.items():
        header_fields[field_name] = _eval_entry(
            field_name, entry, source_row=source_row, period=period, definition_id=config.definition_id,
        )
    if config.stamp_generated_from:
        header_fields["generated_from"] = _generated_from_value(config, source_row=source_row, period=period)

    child_rows = _build_child_rows(config, source_row=source_row, header_id=header_id, period=period)

    if config.balance_check:
        debit_total, credit_total = _sum_balance(config, child_rows)
        if debit_total != credit_total:
            raise BalanceCheckFailed(
                f"{config.definition_id}: unbalanced generation for source "
                f"{source_row.get('id')} period {period.start.isoformat() if period else '-'}: "
                f"debits={debit_total} credits={credit_total} cents -- aborted before any write"
            )

    # Crash-safe ordering: children first (idempotent by id), header last
    # (the commit signal) -- see module docstring's Storage section.
    for child in child_rows:
        try:
            object_records.create_collection_record(
                config.child_collection, child, base_dir=base_dir, roots=roots, actor=config.actor,
            )
        except object_records.DuplicateRecordIdError:
            pass  # a prior partial run already wrote this line -- harmless

    try:
        object_records.create_collection_record(
            config.output_collection, header_fields, base_dir=base_dir, roots=roots, actor=config.actor,
        )
    except object_records.DuplicateRecordIdError:
        # Raced with another pass/process between our existence-check and
        # now -- the other writer's header is the real one; ours is a
        # no-op, not a failure.
        return {"status": "skipped_already_generated", "header_id": header_id}

    return {"status": "generated", "header_id": header_id}


def _generate_creatework(
    config: MaterializeConfig, source_row: Mapping[str, str], *, base_dir: Any, roots: Any = None,
) -> dict[str, Any]:
    """CreateWork shape: fill empty fields on the SAME row, never create.

    Two independent fill mechanisms, both fill-only-if-empty (61's Events
    "correctness rule, not a nicety"), applied in order so a field either
    mechanism has already filled is left alone by the other:
      1. this definition's own ``mapping`` (generic, works for any
         CreateWork-shaped definition).
      2. an opportunistic hop through a ``templates`` relation field, per
         61's worked example 3 exactly (``_apply_template_defaults``).
    The whole ``changes`` dict is built in memory and written in ONE
    ``update_collection_record`` call -- a malformed template blob aborts
    the row cleanly with nothing written, same "compute fully, then write
    once" discipline as the header+children path, at a smaller scale.
    """
    source_id = source_row.get("id") or ""
    if not source_id:
        return {"status": "skipped_already_generated"}

    changes: dict[str, str] = {}
    for field_name, entry in config.mapping.items():
        current = source_row.get(field_name)
        if current not in (None, ""):
            continue  # fill-only-if-empty
        value = _eval_entry(field_name, entry, source_row=source_row, period=None, definition_id=config.definition_id)
        if value in (None, ""):
            continue
        changes[field_name] = value

    _apply_template_defaults(config, source_row, changes, base_dir=base_dir, roots=roots)

    if config.stamp_generated_from and "generated_from" not in changes and (
        source_row.get("generated_from") in (None, "")
    ):
        changes["generated_from"] = _generated_from_value(config, source_row=source_row, period=None)

    if not changes:
        return {"status": "skipped_already_generated", "header_id": source_id}

    object_records.update_collection_record(
        config.output_collection, source_id, changes, base_dir=base_dir, roots=roots, actor=config.actor,
    )
    return {"status": "generated", "header_id": source_id}


def _apply_template_defaults(
    config: MaterializeConfig, source_row: Mapping[str, str], changes: dict[str, str],
    *, base_dir: Any, roots: Any = None,
) -> None:
    """61's worked example 3, literally: find a relation field on the
    OUTPUT schema pointing at ``templates``, follow it, parse that
    template's ``default_values`` JSON, fill-only-if-empty. Best-effort in
    the sense that "no such field," "no id set," "row not found" are all
    quiet no-ops (this definition simply isn't the templates-relation
    shape, or the reference is dangling) -- but a malformed
    ``default_values`` JSON blob is allowed to raise, surfacing as a
    logged per-row error rather than silently doing nothing, since that
    is a real data problem worth an operator's attention.
    """
    try:
        schema = object_schemas.get_schema(config.output_collection, base_dir=base_dir, roots=roots)
    except object_schemas.SchemaNotFoundError:
        return

    template_field = None
    for field in schema.get("fields", []):
        relation = field.get("relation") or {}
        if relation.get("collection") == "templates":
            template_field = field.get("name")
            break
    if not template_field:
        return

    template_id = source_row.get(template_field)
    if not template_id:
        return

    try:
        template_row = object_records.get_collection_record("templates", template_id, base_dir=base_dir, roots=roots)
    except (
        object_records.RecordNotFoundError,
        object_records.InvalidRecordIdError,
        object_collections.CollectionNotFoundError,
        object_collections.InvalidCollectionNameError,
    ):
        return

    raw_defaults = (template_row.get("default_values") or "").strip()
    if not raw_defaults:
        return

    defaults = json.loads(raw_defaults)  # malformed JSON deliberately propagates -- see docstring
    if not isinstance(defaults, dict):
        return

    for field_name, value in defaults.items():
        if field_name in changes:
            continue
        current = source_row.get(field_name)
        if current not in (None, ""):
            continue
        changes[field_name] = _stringify(value)


def _record_exists(collection: str, record_id: str, *, base_dir: Any, roots: Any = None) -> bool:
    try:
        object_records.get_collection_record(collection, record_id, base_dir=base_dir, roots=roots)
        return True
    except object_records.RecordNotFoundError:
        return False


def _build_child_rows(
    config: MaterializeConfig, *, source_row: Mapping[str, str], header_id: str, period: Period | None,
) -> list[dict[str, str]]:
    if not config.child_collection:
        return []
    if config.child_source_field:
        return _build_child_rows_from_template(config, source_row=source_row, header_id=header_id)
    if config.synthesized_amount_entry is not None:
        return _build_depreciation_child_rows(config, source_row=source_row, header_id=header_id, period=period)
    return []  # pragma: no cover -- parse_definition already requires one of the above


def _build_child_rows_from_template(
    config: MaterializeConfig, *, source_row: Mapping[str, str], header_id: str,
) -> list[dict[str, str]]:
    raw = (source_row.get(config.child_source_field) or "").strip()
    if not raw:
        return []
    try:
        lines = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DefinitionError(f"{config.definition_id}: {config.child_source_field} is not valid JSON: {exc}") from exc
    if not isinstance(lines, list):
        raise DefinitionError(f"{config.definition_id}: {config.child_source_field} must be a JSON list of line objects")

    rows: list[dict[str, str]] = []
    for index, line in enumerate(lines):
        if not isinstance(line, dict):
            raise DefinitionError(f"{config.definition_id}: {config.child_source_field}[{index}] must be a JSON object")
        child = {str(key): _stringify(value) for key, value in line.items()}
        child["id"] = f"{header_id}_line_{index}"
        child[config.child_link_field] = header_id
        rows.append(child)
    return rows


def _build_depreciation_child_rows(
    config: MaterializeConfig, *, source_row: Mapping[str, str], header_id: str, period: Period | None,
) -> list[dict[str, str]]:
    amount = _eval_entry(
        "amount", config.synthesized_amount_entry, source_row=source_row, period=period,
        definition_id=config.definition_id,
    )
    debit_field = config.balance_check["debit_field"]
    credit_field = config.balance_check["credit_field"]
    return [
        {
            "id": f"{header_id}_line_0", config.child_link_field: header_id,
            "account_id": config.debit_account_id, debit_field: amount, credit_field: "0",
        },
        {
            "id": f"{header_id}_line_1", config.child_link_field: header_id,
            "account_id": config.credit_account_id, debit_field: "0", credit_field: amount,
        },
    ]


def _sum_balance(config: MaterializeConfig, child_rows: list[dict[str, str]]) -> tuple[int, int]:
    debit_field = config.balance_check["debit_field"]
    credit_field = config.balance_check["credit_field"]
    debit_total = sum(_to_cents(row.get(debit_field)) for row in child_rows)
    credit_total = sum(_to_cents(row.get(credit_field)) for row in child_rows)
    return debit_total, credit_total


def _render_idempotency_key(config: MaterializeConfig, *, source_row: Mapping[str, str], period: Period | None) -> str:
    available: dict[str, str] = {
        "definition_id": config.definition_id,
        "source_id": source_row.get("id") or "",
    }
    if period is not None:
        available["period_start"] = period.start.isoformat()
    try:
        return config.idempotency_key.format(**available)
    except KeyError as exc:
        raise DefinitionError(
            f"{config.definition_id}: idempotency_key references {exc} which is not available "
            "for this generation (period_start only applies to scheduled/scheduled_fixed)"
        ) from exc


def _generated_from_value(config: MaterializeConfig, *, source_row: Mapping[str, str], period: Period | None) -> str:
    payload = {
        "definition_id": config.definition_id,
        "source_id": source_row.get("id") or "",
        "period_start": period.start.isoformat() if period is not None else None,
    }
    return json.dumps(payload, separators=(",", ":"))


# --- Event mode (CreateWork dispatch) -------------------------------------------------

def event_definitions_for_collection(
    collection: str, *, base_dir: Any, roots: Any = None,
) -> list[dict[str, str]]:
    """Return every enabled, non-blocked, event-mode definition whose
    ``source_collection`` is ``collection`` -- the raw records (not
    parsed), cheap enough to call on every dispatch (one collection read,
    one JSON parse per row); the full ``parse_definition`` validation
    happens once ``generate_one_event`` actually attempts a generation.
    """
    try:
        definitions = object_records.read_collection_records(
            MATERIALIZE_DEFINITIONS_COLLECTION, base_dir=base_dir, roots=roots
        )
    except (object_collections.CollectionNotFoundError, object_collections.InvalidCollectionNameError):
        return []

    matches = []
    for definition in definitions:
        if is_definition_blocked(definition) or not is_definition_enabled(definition):
            continue
        if (definition.get("source_collection") or "").strip() != collection:
            continue
        trigger = _json_field(definition, "trigger", None, definition_id=definition.get("id") or "<unknown>")
        if not isinstance(trigger, dict) or trigger.get("mode") != "event":
            continue
        matches.append(definition)
    return matches


def generate_one_event(
    definition_record: Mapping[str, str], source_row: Mapping[str, str], *, base_dir: Any, roots: Any = None,
) -> dict[str, Any]:
    """Apply ONE event-mode definition to ONE just-created source row --
    what ``materialize_seed``'s ``EVENT`` handler calls per dispatched
    record.created event, per definition matching that collection.
    """
    config = parse_definition(definition_record, base_dir=base_dir, roots=roots)
    if config.trigger_mode != "event":
        raise DefinitionError(f"{config.definition_id} is not an event-mode definition")
    return generate_one(config, source_row, None, base_dir=base_dir, roots=roots)


def compute_event_handles(*, base_dir: Any, roots: Any = None) -> list[str]:
    """Return the sorted, de-duplicated ``<collection>.record.created``
    event list every currently enabled, non-blocked, event-mode definition
    implies -- i.e. what ``materialize_seed``'s static ``HANDLES`` literal
    WOULD need to contain for automatic on-create dispatch.

    Pure computation, no side effects. In v1 nothing wires this into the
    object's source at runtime (we do NOT rewrite installed objects under
    the poll loop -- see object_daemon.process_materializations); it exists
    for inspection/tests and as the input a future *deliberate, scheduled*
    HANDLES-sync mechanism (with its own tests and a real performance
    rationale) would use. Event-mode definitions run via the manual path
    (materialize_run) until then.
    """
    try:
        definitions = object_records.read_collection_records(
            MATERIALIZE_DEFINITIONS_COLLECTION, base_dir=base_dir, roots=roots
        )
    except (object_collections.CollectionNotFoundError, object_collections.InvalidCollectionNameError):
        return []

    events: set[str] = set()
    for definition in definitions:
        if is_definition_blocked(definition) or not is_definition_enabled(definition):
            continue
        trigger = _json_field(definition, "trigger", None, definition_id=definition.get("id") or "<unknown>")
        if not isinstance(trigger, dict) or trigger.get("mode") != "event":
            continue
        source_collection = (definition.get("source_collection") or "").strip()
        if source_collection:
            events.add(f"{source_collection}.record.created")
    return sorted(events)


# NOTE: there is deliberately no runtime "sync HANDLES by rewriting the
# installed materialize_seed.py" function here. Rewriting an existing
# object's source under the daemon poll loop is the wrong shape (expensive,
# surprising, self-modifying). materialize_seed ships inert (HANDLES == [])
# and event-mode runs manually (materialize_run). If automatic on-create
# dispatch is ever worth wiring, compute_event_handles above is the input a
# separate, scheduled, tested job would consume -- not this module at import
# or the poll loop at runtime.


# --- Small shared helpers -------------------------------------------------

def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _to_cents(value: Any) -> int:
    text = str(value if value is not None else "").strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError as exc:
        raise DefinitionError(f"expected an integer cents value, got {value!r}") from exc


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


def _json_field(record: Mapping[str, str], name: str, default: Any, *, definition_id: str) -> Any:
    raw = record.get(name)
    if raw is None:
        return default
    if not isinstance(raw, str):
        return raw
    raw = raw.strip()
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DefinitionError(f"{definition_id}: {name} is not valid JSON: {exc}") from exc
