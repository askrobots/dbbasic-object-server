"""Append-only change logs for TSV-backed collection records.

Record changes are the durable facts behind generated admin history screens and
record event publication. Events and listener delivery can fail or be retried;
this file records what actually changed.
"""

from __future__ import annotations

import gzip
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import object_collections
import object_ids
import object_records
from object_versions import DEFAULT_DATA_DIR

RECORD_CHANGES_DIR = "record_changes"
CHANGES_FILE = "changes.jsonl"
DEFAULT_CHANGE_LIMIT = 100
MAX_CHANGE_LIMIT = 1000
VALID_ACTIONS = {"create", "update", "delete"}

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class InvalidRecordChangeError(ValueError):
    """Raised when a record change entry is not safe to write or read."""


def append_record_change(
    *,
    collection: str,
    record_id: str,
    action: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    actor: str = "api",
    message: str = "",
    correlation_id: str | None = None,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    """Append one record change and return the stored entry."""
    path = record_changes_file(collection, base_dir=base_dir)
    if not object_records.validate_record_id(record_id):
        raise object_records.InvalidRecordIdError(f"Invalid record id: {record_id}")
    if action not in VALID_ACTIONS:
        raise InvalidRecordChangeError(f"Invalid record change action: {action}")

    before_snapshot = _normalize_snapshot(before)
    after_snapshot = _normalize_snapshot(after)
    if before_snapshot is None and after_snapshot is None:
        raise InvalidRecordChangeError("Record change must include before or after")

    timestamp = _utc_timestamp()
    entry = {
        "change_id": _change_id(timestamp, collection, record_id, action),
        "timestamp": timestamp,
        "collection": collection,
        "record_id": record_id,
        "action": action,
        "actor": _clean_text(actor, default="api"),
        "message": _clean_text(message, default=_default_message(action)),
        "correlation_id": correlation_id or None,
        "changed_fields": _changed_fields(before_snapshot, after_snapshot),
        "before": before_snapshot,
        "after": after_snapshot,
    }

    with _file_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")))
            handle.write("\n")

    return entry


def list_record_changes(
    collection: str,
    *,
    record_id: str | None = None,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    limit: int = DEFAULT_CHANGE_LIMIT,
    offset: int = 0,
    tail_only: bool = False,
) -> dict[str, Any]:
    """Return newest-first record changes for one collection or record.

    `tail_only` reads just enough of the end of the log to satisfy
    limit+offset instead of parsing the whole file. It is opt-in because
    it makes `total` a count of what was READ rather than of what exists,
    and a caller that shows "1 of 4,312" must not be handed "1 of 100"
    without asking. A feed does not care; an audit page does.
    """
    path = record_changes_file(collection, base_dir=base_dir)
    if record_id is not None and not object_records.validate_record_id(record_id):
        raise object_records.InvalidRecordIdError(f"Invalid record id: {record_id}")
    _validate_page(limit=limit, offset=offset)

    changes = _read_changes(
        path, record_id=record_id,
        tail=(limit + offset if tail_only else None),
    )
    total = len(changes)
    window = changes[offset:offset + limit]
    payload: dict[str, Any] = {
        "collection": collection,
        "changes": window,
        "count": len(window),
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(window) < total,
    }
    if record_id is not None:
        payload["record_id"] = record_id
    return payload


def record_changes_file(collection: str, base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    """Return the validated JSONL change-log path for a collection."""
    if not object_collections.validate_collection_name(collection):
        raise object_collections.InvalidCollectionNameError(
            f"Invalid collection name: {collection}"
        )

    root = Path(base_dir) / RECORD_CHANGES_DIR
    path = root / collection / CHANGES_FILE
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)

    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise object_collections.InvalidCollectionNameError(
            f"Record change path escapes change directory: {collection}"
        ) from exc

    return path


# Reading a change log tail-first, in blocks, instead of parsing the whole
# file. A change log only ever grows, and the newest entries are the ones
# anybody asks for, so paying for the whole history to answer "what
# happened lately" is a cost that rises forever while the answer stays the
# same size.
#
# This was not theoretical. One collection's log reached 944MB -- a rollup
# rewriting 1818 derived rows every five minutes, each rewrite appending a
# row per record -- and the activity feed, which reads EVERY collection's
# log, sat parsing a gigabyte of JSON on a single core while the page said
# "loading..." forever. The function's own docstring had predicted it:
# "fine at current scale... future work if/when logs grow large."
_TAIL_BLOCK = 256 * 1024


def _tail_lines(path: Path, wanted: int) -> list[str]:
    """The last `wanted` non-empty lines, read backwards in blocks.

    Falls back to a whole-file read only for a file smaller than one
    block, where seeking would be more code than it saves.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size <= _TAIL_BLOCK:
        return path.read_text(encoding="utf-8").splitlines()

    chunks: list[bytes] = []
    newlines = 0
    with path.open("rb") as handle:
        position = size
        while position > 0 and newlines <= wanted:
            step = min(_TAIL_BLOCK, position)
            position -= step
            handle.seek(position)
            block = handle.read(step)
            chunks.append(block)
            newlines += block.count(b"\n")
    text = b"".join(reversed(chunks)).decode("utf-8", "replace")
    # The first line of the first block read is probably a fragment of a
    # longer line; dropping it is why we read one block past `wanted`.
    lines = text.splitlines()
    return lines[1:] if position > 0 else lines


def _read_changes(
    path: Path, *, record_id: str | None, tail: int | None = None
) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    if tail is not None and record_id is None:
        source = _tail_lines(path, tail)
    else:
        # A record-scoped query still scans: the entries for one record can
        # be anywhere in the file, and answering "show me this invoice's
        # history" with only the recent tail would quietly lose the half
        # that matters.
        source = None

    changes: list[dict[str, Any]] = []
    with _file_lock(path):
        for line in (source if source is not None
                     else path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if record_id is not None and entry.get("record_id") != record_id:
                continue
            changes.append(entry)

    changes.reverse()
    return changes


def _normalize_snapshot(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    if not isinstance(record, dict):
        raise InvalidRecordChangeError("Record snapshot must be an object")
    return {str(key): _json_safe_value(value) for key, value in record.items()}


def _json_safe_value(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _changed_fields(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> list[str]:
    before_values = before or {}
    after_values = after or {}
    names = set(before_values) | set(after_values)
    return sorted(name for name in names if before_values.get(name) != after_values.get(name))


def _validate_page(*, limit: int, offset: int) -> None:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if limit > MAX_CHANGE_LIMIT:
        raise ValueError(f"limit must be at most {MAX_CHANGE_LIMIT}")
    if offset < 0:
        raise ValueError("offset must be at least 0")


def _clean_text(value: str, *, default: str) -> str:
    text = str(value).strip()
    return text or default


def _default_message(action: str) -> str:
    return {
        "create": "Created record",
        "update": "Updated record",
        "delete": "Deleted record",
    }[action]


def _change_id(timestamp: str, collection: str, record_id: str, action: str) -> str:
    return object_ids.new_uuid4()


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_lock(path: Path) -> threading.Lock:
    key = str(path.resolve(strict=False))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


# --- retention -----------------------------------------------------------------
#
# The third unbounded log this server has grown, after page_views and the
# package restore points, and the pattern is now unmistakable: a log
# nobody tells how big it may get is a log that fails on the worst day
# rather than an ordinary one.
#
# A change log is audit evidence, so pruning it is a genuinely different
# act from pruning analytics. Two consequences, both deliberate:
#
#   * Retention is OFF unless an operator turns it on. Deleting audit
#     history by default is not a performance optimisation, it is a
#     decision about accountability, and it is not this module's to make
#     quietly.
#   * The bound is stated in BOTH dimensions, because age alone does not
#     bound anything: a retention window says how far back to keep and
#     nothing at all about how much can arrive inside it.
#
# The rollup churn that motivated this is fixed at the source instead
# (object_rollups writes derived rows with record_changes=False) -- which
# is the better fix, because a log entry never written costs nothing to
# store, nothing to read past, and nothing to decide about later.

RETENTION_DAYS_ENV = "DBBASIC_CHANGE_LOG_RETENTION_DAYS"
MAX_ENTRIES_ENV = "DBBASIC_CHANGE_LOG_MAX_ENTRIES"
ARCHIVE_ENV = "DBBASIC_CHANGE_LOG_ARCHIVE"
KEEP_ARCHIVES_ENV = "DBBASIC_CHANGE_LOG_KEEP_ARCHIVES"
ARCHIVE_DIR = "archive"

# Archival and retention are different questions, and conflating them is
# what made the first version of this uncomfortable.
#
#   ROTATION bounds the file the server reads on every feed render.
#   ARCHIVAL decides whether the entries leaving that file still exist.
#   REMOVAL decides how long the archives themselves are kept.
#
# An audit trail wants all three, in that order. Rotating without
# archiving is deletion wearing a gentler word, and it is why retention
# had to be opt-in and slightly apologetic. Archiving first inverts that:
# pruning becomes cheap to turn on, because nothing is actually lost --
# the entries move out of the hot read path into a compressed segment
# that is still on disk, still greppable, and still handable to an
# auditor. JSONL compresses roughly ten to twenty times, so the 944MB log
# that started all this becomes a segment measured in tens of megabytes.
#
# Removal still has to exist, because an archive nobody ever deletes is
# just the original problem moved one directory down. Hence
# KEEP_ARCHIVES: the same "keep the newest N" shape object_logs already
# uses for rotated server logs, so an operator learns the convention once.
# Its default keeps everything, because the default for evidence should
# never be "throw it away".


def retention_policy(env: Mapping[str, str] | None = None) -> dict[str, int]:
    """{"days", "max_entries"} -- 0 for either means "no bound".

    Absent configuration is no bound at all, not a default window: an
    operator who has never thought about audit retention has not thereby
    consented to losing audit history.
    """
    source = os.environ if env is None else env

    def _int(name: str) -> int:
        try:
            value = int(str(source.get(name, "")).strip())
        except (TypeError, ValueError):
            return 0
        return value if value > 0 else 0

    archive = str(source.get(ARCHIVE_ENV, "")).strip().lower()
    return {
        "days": _int(RETENTION_DAYS_ENV),
        "max_entries": _int(MAX_ENTRIES_ENV),
        # Archiving is the DEFAULT when a policy is set at all. Somebody
        # asking for a smaller hot file has not asked to lose the history.
        "archive": archive not in ("0", "false", "no", "off"),
        "keep_archives": _int(KEEP_ARCHIVES_ENV),
    }


def archive_dir(collection: str, base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    """Where a collection's rotated change-log segments live."""
    return record_changes_file(collection, base_dir=base_dir).parent / ARCHIVE_DIR


def list_archives(collection: str, base_dir: Path | str = DEFAULT_DATA_DIR) -> list[Path]:
    """Archived segments for one collection, oldest first."""
    directory = archive_dir(collection, base_dir=base_dir)
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("changes-*.jsonl.gz") if p.is_file())


def _archive_entries(collection: str, lines: list[str], *, base_dir: Path | str) -> Path:
    """Write expired entries to a new compressed segment and return it.

    A new segment per rotation rather than appending to one growing
    archive: a gzip member you keep appending to is a file you can only
    ever read whole, and the point of moving these out was to stop paying
    for the whole history. Segments are timestamped and immutable, so an
    auditor can take the three that cover a quarter and leave the rest.
    """
    directory = archive_dir(collection, base_dir=base_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    path = directory / f"changes-{stamp}.jsonl.gz"
    temp_path = path.with_name(f".{path.name}.tmp")
    with gzip.open(temp_path, "wt", encoding="utf-8", compresslevel=6) as handle:
        for line in lines:
            handle.write(line)
            handle.write("\n")
    temp_path.replace(path)
    return path


def _remove_stale_archives(
    collection: str, *, keep: int, base_dir: Path | str
) -> list[str]:
    """Delete all but the newest `keep` segments; 0 keeps everything.

    This is the only place in this module that destroys anything, and it
    is deliberately the last step of the chain rather than the first: by
    the time a segment is dropped it has already been rotated out of the
    hot file and compressed, so an operator who never sets this keeps
    every entry the server ever wrote.
    """
    if keep <= 0:
        return []
    segments = list_archives(collection, base_dir=base_dir)
    stale = segments[:-keep] if len(segments) > keep else []
    for path in stale:
        path.unlink(missing_ok=True)
    return [path.name for path in stale]


def prune_record_changes(
    collection: str,
    *,
    keep_newer_than: str | None = None,
    keep_last: int | None = None,
    archive: bool = True,
    keep_archives: int = 0,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    """Trim one collection's change log to a time window and/or a count.

    Rewrites the file in one pass under the same lock every other reader
    and writer takes, keeping entries newer than ``keep_newer_than``
    (ISO-8601) and then the newest ``keep_last`` of whatever survives. An
    entry with a missing or unparseable timestamp is KEPT -- never delete
    evidence we cannot date.

    Returns {"entries_before", "entries_after", "removed", "pruned"}.
    Both bounds absent is an honest no-op rather than an error, so a
    caller can pass an unconfigured policy straight through.
    """
    path = record_changes_file(collection, base_dir=base_dir)
    if not path.exists():
        return {"entries_before": 0, "entries_after": 0, "removed": 0, "pruned": False}
    if not keep_newer_than and not keep_last:
        return {"entries_before": 0, "entries_after": 0, "removed": 0, "pruned": False}

    with _file_lock(path):
        lines = path.read_text(encoding="utf-8").splitlines()
        kept: list[str] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)      # unreadable is not the same as expired
                continue
            stamp = str(entry.get("timestamp") or "")
            if keep_newer_than and stamp and stamp < str(keep_newer_than):
                continue
            kept.append(line)

        if keep_last and len(kept) > keep_last:
            # The log is append-ordered, so the tail is the newest.
            kept = kept[len(kept) - keep_last:]

        before = len([line for line in lines if line.strip()])
        removed = before - len(kept)
        if removed <= 0:
            return {"entries_before": before, "entries_after": before,
                    "removed": 0, "pruned": False, "archived": None}

        archived_to = None
        if archive:
            # Difference by COUNT, not by identity or membership: two
            # identical entries are two facts, and a set would archive one
            # of them while the file lost both.
            kept_counts: dict[str, int] = {}
            for line in kept:
                kept_counts[line] = kept_counts.get(line, 0) + 1
            expired = []
            for line in lines:
                if not line.strip():
                    continue
                if kept_counts.get(line):
                    kept_counts[line] -= 1
                    continue
                expired.append(line)
            if expired:
                archived_to = _archive_entries(
                    collection, expired, base_dir=base_dir).name

        temp_path = path.with_name(f".{path.name}.tmp")
        temp_path.write_text(
            "".join(f"{line}\n" for line in kept), encoding="utf-8")
        temp_path.replace(path)

    dropped = _remove_stale_archives(
        collection, keep=keep_archives, base_dir=base_dir)
    return {"entries_before": before, "entries_after": len(kept),
            "removed": removed, "pruned": True,
            "archived": archived_to, "archives_removed": dropped}


def prune_all_record_changes(
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    days: int = 0,
    max_entries: int = 0,
    archive: bool = True,
    keep_archives: int = 0,
) -> dict[str, Any]:
    """Apply one retention policy across every collection's change log.

    One bad or unreadable log never stops the sweep: this runs unattended
    on a timer, and a single corrupt file must not mean every other log
    grows forever.
    """
    if not days and not max_entries:
        return {"collections": 0, "removed": 0, "pruned": [], "skipped": "no policy set"}

    cutoff = None
    if days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)
                  ).isoformat().replace("+00:00", "Z")

    root = Path(base_dir) / RECORD_CHANGES_DIR
    if not root.is_dir():
        return {"collections": 0, "removed": 0, "pruned": []}

    removed = 0
    pruned: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        try:
            result = prune_record_changes(
                entry.name, keep_newer_than=cutoff,
                keep_last=(max_entries or None),
                archive=archive, keep_archives=keep_archives,
                base_dir=base_dir,
            )
        except Exception:  # noqa: BLE001 -- one bad log must not stop the sweep
            continue
        if result.get("pruned"):
            removed += result["removed"]
            pruned.append({"collection": entry.name, **result})
    return {"collections": len(pruned), "removed": removed, "pruned": pruned}
