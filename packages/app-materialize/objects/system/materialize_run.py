"""materialize_run -- manual "Run now" for one materialize_definitions row.

Implements plan/vocabulary/61-materialize-spec.md's Surfaces/Events
section: a system object exposed as a slot button (window.dbbasicSlots,
see objects/site/materialize_run_button.py) on the materialize_definitions
detail page. Fires the IDENTICAL per-source-row generation function the
daemon's scheduled pass (object_daemon.process_materializations) and the
event handler (materialize_seed) both call
(object_materialize.generate_definition) -- synchronously, scoped to ONE
definition -- so running it twice is safe by construction, never a
separate "are you sure" code path.

Also the primary interface for trigger.mode: "event" definitions when
DBBASIC_ENABLE_EVENT_HANDLERS is off: generate_definition treats an
event-mode definition as "apply this definition to every currently
matching source row once each," which is exactly the catch-up behavior
an operator wants when dispatch isn't running.

Degradation, per 61's own section: refuses when the block-wide
materialize_enabled flag is off (no bypass of an operator's explicit
kill switch -- "off means off, full stop, for BOTH the scheduled and
manual paths"), when the definition itself is block: true (a distinct
"blocked, not merely paused" error), or enabled: false.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import object_materialize
import object_records

DATA_DIR_ENV = "DBBASIC_DATA_DIR"


def _data_dir() -> str:
    return os.environ.get(DATA_DIR_ENV, object_records.DEFAULT_DATA_DIR)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def POST(request):
    definition_id = str((request or {}).get("definition_id") or "").strip()
    if not definition_id:
        return {"status": "error", "error": "definition_id is required"}

    base_dir = _data_dir()

    if not object_materialize.materialize_pass_enabled(base_dir=base_dir):
        return {
            "status": "error",
            "error": "materialize_enabled flag is off -- manual run refuses to bypass a "
                     "block-wide kill switch an operator explicitly set",
        }

    try:
        definition = object_records.get_collection_record(
            object_materialize.MATERIALIZE_DEFINITIONS_COLLECTION, definition_id, base_dir=base_dir,
        )
    except object_records.RecordNotFoundError:
        return {"status": "error", "error": f"no such materialize definition: {definition_id}"}
    except object_records.InvalidRecordIdError:
        return {"status": "error", "error": f"invalid definition id: {definition_id}"}

    if object_materialize.is_definition_blocked(definition):
        return {
            "status": "error",
            "error": "definition is blocked, not merely paused -- fix and clear block before running",
        }
    if not object_materialize.is_definition_enabled(definition):
        return {"status": "error", "error": "definition is disabled (enabled: false)"}

    try:
        result = object_materialize.generate_definition(definition, base_dir=base_dir)
    except object_materialize.DefinitionError as exc:
        return {"status": "error", "error": str(exc)}

    try:
        object_records.update_collection_record(
            object_materialize.MATERIALIZE_DEFINITIONS_COLLECTION,
            definition_id,
            {"last_run_at": _now_iso()},
            base_dir=base_dir,
            actor=definition.get("actor") or object_materialize.DEFAULT_ACTOR,
            preserve_read_only=True,
        )
    except Exception:
        # The generation itself already succeeded and is live in
        # output_collection; a failed last_run_at stamp only means this
        # definition looks "due" again next scheduled pass -- harmless.
        pass

    return {
        "status": "ok",
        "checked": result["checked"],
        "generated": result["generated"],
        "skipped_already_generated": result["skipped_already_generated"],
        "errors": result["errors"],
    }


def GET(request):
    return {
        "status": "ok",
        "description": "POST {\"definition_id\": \"...\"} to run one materialize_definitions row now.",
    }
