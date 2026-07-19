"""Activity feed -- a fold over the record-change ledger, not a new event system.

Every create/update/delete already flows through
``object_record_changes.append_record_change`` (called from
object_records.py's create/update/delete paths), which durably appends
actor, action, collection, record_id, and before/after snapshots to one
JSONL log per collection: ``data/record_changes/{collection}/changes.jsonl``.

``recent_activity`` reads those logs across every collection that has one,
merges them newest-first, and derives a human title from each change's
snapshot. "actor ACTION collection title" is exactly the shape of a classic
content activity feed, and it required no new storage and no new write path
-- universal attribution already recorded everything a feed needs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import object_collections
import object_record_changes
from object_versions import DEFAULT_DATA_DIR

# Internal/system collections that would drown out real user activity in a
# feed meant to answer "what did people do": shell scrollback and AI usage/
# billing counters today, and (as they show up) any rate-limit- or
# ops-bookkeeping-style collection whose writes aren't something a person
# did. Extend this set rather than filtering ad hoc per call site.
EXCLUDED_COLLECTIONS = frozenset({
    "shell_commands",
    "ai_usage",
})

# First present of these fields on the change's snapshot becomes the feed
# row's title; record_id is the final fallback so every entry always has
# something to show.
_TITLE_FIELDS = ("title", "name", "number", "subject")


def recent_activity(
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    actor: str | None = None,
    limit: int = 50,
    roots: Iterable[Path] | None = None,
) -> list[dict[str, Any]]:
    """Return up to `limit` record-change entries across collections, newest first.

    Each entry is shaped ``{actor, action, collection, record_id, title,
    timestamp}`` -- never the full before/after snapshot. A feed is a
    signal, not a data dump.

    Scoping (v1): when `actor` is given, only entries whose actor matches
    are returned -- the classic "your activity" feed (e.g. "dan CREATED
    NOTE ... 2m ago" for dan's own changes across every collection). Every
    entry here is guaranteed visible to that actor because they made the
    change, so this sidesteps per-entry permission checks entirely.

    Deferred: a broader "activity on anything I can see" feed -- changes by
    *other* actors, filtered per record against the viewer's read access --
    needs the permission engine's row filters threaded through here (see
    plan/vocabulary/23-activity-spec.md's per-collection/per-record feeds).
    That is real work (a read-policy check per candidate entry) and is not
    attempted by this function.

    `roots` mirrors object_collections.list_collections's signature for
    symmetry; it is unused here because record-change logs live entirely
    under `base_dir` and don't depend on which object source roots are
    mounted.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")

    entries: list[dict[str, Any]] = []
    for collection in _collections_with_changes(base_dir=base_dir):
        if collection in EXCLUDED_COLLECTIONS:
            continue
        # Reads each collection's whole change log (list_record_changes
        # loads the full JSONL regardless of `limit`) -- fine at current
        # scale, where these logs are small. A real tail-read (seek to the
        # last N lines instead of parsing the whole file) is future work
        # if/when logs grow large; requesting the module's own max here
        # avoids clipping an actor's older entries out of an otherwise
        # chatty collection before the actor filter below gets to see them.
        payload = object_record_changes.list_record_changes(
            collection,
            base_dir=base_dir,
            limit=object_record_changes.MAX_CHANGE_LIMIT,
        )
        for change in payload["changes"]:
            if actor is not None and change.get("actor") != actor:
                continue
            entries.append(_feed_entry(change))

    entries.sort(key=lambda entry: _sort_key(entry["timestamp"]), reverse=True)
    return entries[:limit]


def _feed_entry(change: dict[str, Any]) -> dict[str, Any]:
    return {
        "actor": change.get("actor"),
        "action": change.get("action"),
        "collection": change.get("collection"),
        "record_id": change.get("record_id"),
        "title": _derive_title(change),
        "timestamp": change.get("timestamp"),
    }


def _derive_title(change: dict[str, Any]) -> str:
    # Deletes have no `after`; fall back to the last-known `before`.
    snapshot = change.get("after") or change.get("before") or {}
    for field in _TITLE_FIELDS:
        value = snapshot.get(field)
        if value:
            return str(value)
    return str(change.get("record_id") or "")


def _sort_key(timestamp: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(timestamp))
    except ValueError:
        # Malformed timestamp: sort it oldest rather than crash the whole
        # feed over one bad entry.
        return datetime.min.replace(tzinfo=timezone.utc)


def _collections_with_changes(*, base_dir: Path | str) -> list[str]:
    """Return every collection name with a record_changes log on disk."""
    root = Path(base_dir) / object_record_changes.RECORD_CHANGES_DIR
    if not root.exists() or not root.is_dir():
        return []

    names = []
    for path in sorted(root.glob(f"*/{object_record_changes.CHANGES_FILE}")):
        name = path.parent.name
        if object_collections.validate_collection_name(name):
            names.append(name)
    return names
