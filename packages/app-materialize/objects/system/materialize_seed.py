"""materialize_seed -- event-mode (CreateWork) dispatch handler.

Implements plan/vocabulary/61-materialize-spec.md's Events section 2:
declares HANDLES for every distinct source_collection value across
enabled, non-blocked, trigger.mode == "event" materialize_definitions
rows. Registered as a Phase-5a event handler (object_handlers.py,
docs/event-hooks-decisions.md); the platform's post-commit dispatcher
executes this object's EVENT method whenever a client-facing write
commits to a HANDLES-listed collection, only when the operator has
DBBASIC_ENABLE_EVENT_HANDLERS set. With the flag unset (the default),
event-mode definitions simply never receive dispatches -- the record is
created unseeded, same as pre-materialize behavior exactly (61's
Degradation); materialize_run (manual) still works and can be fired by
hand on that record's id.

HANDLES below is a placeholder, static list literal -- this codebase's
HANDLES mechanism (object_handlers.extract_handles) is a pure AST parse
of a module-level list literal, with no dynamic/computed form. Keeping it
correct as the definition set changes is object_materialize.
sync_materialize_seed_handles's job: it rewrites THIS list, right here,
in the installed copy of this file, then calls object_handlers.
invalidate() and re-stamps this package's baseline hash for this object
(so a future package upgrade doesn't mistake the rewrite for an operator
customization -- see that function's own docstring for the full
reasoning). object_daemon.process_materializations calls it once per
materialize pass, so HANDLES tracks the definition set with the same
"recompute from scratch every pass" discipline the rest of this block
uses, regardless of whether DBBASIC_ENABLE_EVENT_HANDLERS is currently
on -- the list is kept correct so dispatch is right the moment an
operator flips that flag.
"""
from __future__ import annotations

import os

import object_materialize
import object_records

HANDLES = []

DATA_DIR_ENV = "DBBASIC_DATA_DIR"


def _data_dir() -> str:
    return os.environ.get(DATA_DIR_ENV, object_records.DEFAULT_DATA_DIR)


def EVENT(request):
    """Dispatch entry point. request = {"event", "collection", "record_id", "action"}.

    Post-commit, best-effort: every exception is caught here so a bad row
    or a race with a concurrent delete never surfaces as a failed write
    on the triggering collection -- object_server's own dispatcher already
    wraps this call in a try/except for the same reason; this is defense
    in depth, not redundant.
    """
    collection = str((request or {}).get("collection") or "")
    record_id = str((request or {}).get("record_id") or "")
    action = str((request or {}).get("action") or "")
    if not collection or not record_id or action != "created":
        return {"ok": True, "skipped": "not a record.created event"}

    base_dir = _data_dir()

    if not object_materialize.materialize_pass_enabled(base_dir=base_dir):
        return {"ok": True, "skipped": "materialize_enabled flag is off"}

    try:
        definitions = object_materialize.event_definitions_for_collection(collection, base_dir=base_dir)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    if not definitions:
        return {"ok": True, "skipped": f"no enabled event-mode definition for {collection}"}

    try:
        source_row = object_records.get_collection_record(collection, record_id, base_dir=base_dir)
    except (object_records.RecordNotFoundError, object_records.InvalidRecordIdError):
        return {"ok": True, "skipped": "record no longer exists"}

    results = []
    for definition in definitions:
        definition_id = definition.get("id") or "<unknown>"
        try:
            result = object_materialize.generate_one_event(definition, source_row, base_dir=base_dir)
            results.append({"definition_id": definition_id, "ok": True, **result})
        except Exception as exc:
            results.append({"definition_id": definition_id, "ok": False, "error": str(exc)})

    return {"ok": True, "results": results}
