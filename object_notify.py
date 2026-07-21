"""12 notify -- turn record-change events into notifications, declaratively.

A `notify_rules` row (app-notify) declares an event pattern, an optional flat
match, a recipient rule, and channels; this module turns ONE record-change
entry into the notification writes it implies. The engine is driven by the
daemon (object_daemon.process_notifications) polling the record-change log
(object_record_changes) rather than the synchronous HANDLES dispatch the spec
sketches -- HANDLES is gated behind DBBASIC_ENABLE_EVENT_HANDLERS (off in
prod) and we deliberately don't rewrite installed objects to track dynamic
event sets (see plan/vocabulary/61 removal + memory no-runtime-object-rewriting).
Polling the change log instead makes notify work with no event-handler
dependency, at-least-once (the same delivery bar 01/12 accept), reading the
same before/after/changed_fields/actor the change entry already carries.

v1 ships the `in_app` channel only (append a `notifications` row, which the
nav bell + realtime already render). `email` waits on 01-email-adapter; digest
batching waits too. Both are recognized-and-skipped, not errors.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

import object_collections
import object_identity
import object_records

NOTIFY_RULES_COLLECTION = "notify_rules"
NOTIFICATIONS_COLLECTION = "notifications"
FEATURE_FLAGS_COLLECTION = "feature_flags"
NOTIFY_ENABLED_FLAG = "notify_enabled"
DEFAULT_ACTOR = "daemon:notify"

_ACTIONS = frozenset({"created", "updated", "deleted"})
# A record-change entry stores the action in present tense (create/update/
# delete, object_record_changes); the event grammar (and object_handlers'
# event_name) uses the -ed event suffix. Map one to the other before matching.
_CHANGE_TO_EVENT = {"create": "created", "update": "updated", "delete": "deleted"}


def _event_action(change: Mapping[str, Any]) -> str:
    raw = str(change.get("action") or "")
    return _CHANGE_TO_EVENT.get(raw, raw)


def _json_field(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _truthy(value: Any, *, default: bool) -> bool:
    if value in (None, ""):
        return default
    return str(value).strip().lower() not in {"off", "false", "0", "no"}


def notify_pass_enabled(*, base_dir: Any) -> bool:
    """Block-wide kill switch, `<block>_enabled` convention -- a feature_flags
    row named `notify_enabled`. Default ON (brownout lever, not an adoption
    gate; with zero rules it does nothing anyway). Mirrors
    object_rollups.rollup_pass_enabled exactly."""
    try:
        rows = object_records.read_collection_records(FEATURE_FLAGS_COLLECTION, base_dir=base_dir)
    except (object_collections.CollectionNotFoundError,
            object_collections.InvalidCollectionNameError, OSError, ValueError):
        return True
    for row in rows:
        if row.get("flag") == NOTIFY_ENABLED_FLAG:
            value = (row.get("value") or "").strip().lower()
            return True if not value else value not in {"off", "false", "0", "no"}
    return True


def event_pattern_matches(pattern: str, collection: str, action: str) -> bool:
    """`<collection>.record.<action>` with `*` allowed in the collection or
    action segment (the spec's grammar). Anything malformed never matches."""
    parts = str(pattern or "").split(".")
    if len(parts) != 3 or parts[1] != "record":
        return False
    coll_pat, _, act_pat = parts
    if action not in _ACTIONS:
        return False
    coll_ok = coll_pat == "*" or coll_pat == collection
    act_ok = act_pat == "*" or act_pat == action
    return coll_ok and act_ok


def watched_collections(rules: Iterable[Mapping[str, str]], known: Iterable[str]) -> set[str]:
    """The set of collections any enabled rule's event_pattern could fire on --
    a specific collection, or (for a `*` collection pattern) every known one.
    Lets the daemon poll only the change logs that matter."""
    known = set(known)
    watched: set[str] = set()
    for rule in rules:
        parts = str(rule.get("event_pattern") or "").split(".")
        if len(parts) != 3 or parts[1] != "record":
            continue
        watched |= known if parts[0] == "*" else {parts[0]}
    return watched


def _record_for(change: Mapping[str, Any]) -> dict[str, Any]:
    """The record state a `match`/template/recipient reads: `after` for
    created/updated, `before` for deleted (the row is already gone)."""
    if _event_action(change) == "deleted":
        return dict(change.get("before") or {})
    return dict(change.get("after") or {})


def _match_ok(rule: Mapping[str, Any], change: Mapping[str, Any], record: Mapping[str, Any]) -> bool:
    """Flat {field: value} equality, ANDed. On an UPDATE the condition is
    restricted to fields the write actually CHANGED (change.changed_fields) --
    so "notify when status becomes assigned" fires once on the transition, not
    on every later edit that still happens to satisfy status == assigned."""
    conditions = _json_field(rule.get("match"), {})
    if not isinstance(conditions, dict) or not conditions:
        return True
    changed = set(change.get("changed_fields") or [])
    is_update = _event_action(change) == "updated"
    for field, want in conditions.items():
        if is_update and field not in changed:
            return False
        if str(record.get(field, "")) != str(want):
            return False
    return True


def resolve_recipients(rule: Mapping[str, Any], record: Mapping[str, Any], *, base_dir: Any) -> list[str]:
    """The four recipient modes: owner / field / users / role. A resolved
    value that isn't a usable user id (empty/unknown) is silently skipped."""
    spec = _json_field(rule.get("recipients"), {})
    if not isinstance(spec, dict):
        return []
    mode = spec.get("mode")
    out: list[str] = []
    if mode == "owner":
        out = [record.get(spec.get("owner_field") or "owner_id") or ""]
    elif mode == "field":
        out = [record.get(spec.get("field") or "") or ""]
    elif mode == "users":
        ids = spec.get("user_ids")
        out = [str(u) for u in ids] if isinstance(ids, list) else []
    elif mode == "role":
        want = spec.get("role")
        try:
            users = object_identity.list_users(base_dir=base_dir)
        except (OSError, ValueError, AttributeError):
            users = []
        out = [u.get("user_id") for u in users if want and want in (u.get("roles") or [])]
    return [str(u) for u in out if u]


def render_template(template: str, record: Mapping[str, Any]) -> str:
    """`{field}` substitution from the record -- no expressions, no logic.
    An unknown field renders empty (a missing due_date shouldn't blow up)."""
    class _Blank(dict):
        def __missing__(self, key):
            return ""
    try:
        return str(template or "").format_map(_Blank(record))
    except (ValueError, IndexError):
        return str(template or "")


def notifications_for_change(
    rule: Mapping[str, Any], change: Mapping[str, Any], *, base_dir: Any,
) -> list[dict[str, str]]:
    """Every notification record ONE rule implies for ONE change (v1: in_app
    only). Empty when the rule doesn't match, no recipient resolves, or the
    rule declares no in_app channel."""
    if not _truthy(rule.get("enabled"), default=True):
        return []
    collection = change.get("collection")
    action = _event_action(change)
    if not event_pattern_matches(rule.get("event_pattern"), collection, action):
        return []
    record = _record_for(change)
    if not _match_ok(rule, change, record):
        return []

    recipients = resolve_recipients(rule, record, base_dir=base_dir)
    if _truthy(rule.get("suppress_self"), default=True):
        actor = change.get("actor")
        recipients = [r for r in recipients if r != actor]
    # de-dup while preserving order (a role rule could name the same user twice)
    seen: set[str] = set()
    recipients = [r for r in recipients if not (r in seen or seen.add(r))]
    if not recipients:
        return []

    channels = _json_field(rule.get("channels"), [])
    in_app = next((c for c in channels if isinstance(c, dict) and c.get("channel") == "in_app"), None)
    if not in_app:
        return []  # v1: email/other channels recognized but not delivered yet

    body_template = in_app.get("body_template") or ""
    target = f"{collection}/{change.get('record_id')}"
    body = render_template(body_template, record)
    return [
        {"user_id": uid, "kind": "notify", "body": body, "target": target, "is_read": "false"}
        for uid in recipients
    ]
