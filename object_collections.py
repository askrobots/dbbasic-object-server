"""Read-only collection summaries for object sources.

Collections are a server-side view over object source files and permission
policy. They let tools such as Scroll show business/app groupings without
turning collection membership into a separate database table too early.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import object_files
import object_logs
import object_permission_store
import object_permissions
import object_state
from object_namespace import (
    ObjectSource,
    iter_object_sources,
    parse_user_object_id,
    validate_object_id,
)
from object_versions import DEFAULT_DATA_DIR


class InvalidCollectionNameError(ValueError):
    """Raised when a collection name is not safe for routes or storage."""


class CollectionNotFoundError(LookupError):
    """Raised when a collection has no objects or permission rules."""


@dataclass(frozen=True)
class CollectionObjectSummary:
    object_id: str
    path: str
    owner: str
    kind: str
    state_count: int
    has_logs: bool
    file_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "path": self.path,
            "owner": self.owner,
            "kind": self.kind,
            "state_count": self.state_count,
            "has_logs": self.has_logs,
            "file_count": self.file_count,
        }


def validate_collection_name(collection: str) -> bool:
    """Return True when a collection name is safe to use in a route."""
    return validate_object_id(collection)


def collection_for_object_id(object_id: str) -> str | None:
    """Infer the permission/UI collection for an object id."""
    parsed = parse_user_object_id(object_id)
    if parsed is not None:
        _, name = parsed
        return name.split("_", 1)[0] if name else None

    return object_id.split("_", 1)[0] if object_id else None


def list_collections(
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
) -> list[dict[str, Any]]:
    """Return summaries for all known source and permission collections."""
    sources = iter_object_sources(roots)
    policy = object_permission_store.load_policy(base_dir)
    summaries = _summaries_by_collection(sources, policy, base_dir=base_dir)
    return [summaries[name] for name in sorted(summaries)]


def get_collection(
    collection: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Return one collection summary including object details."""
    if not validate_collection_name(collection):
        raise InvalidCollectionNameError(f"Invalid collection name: {collection}")

    sources = iter_object_sources(roots)
    policy = object_permission_store.load_policy(base_dir)
    summaries = _summaries_by_collection(
        sources,
        policy,
        base_dir=base_dir,
        include_objects=True,
    )
    try:
        return summaries[collection]
    except KeyError as exc:
        raise CollectionNotFoundError(f"Collection not found: {collection}") from exc


def _summaries_by_collection(
    sources: list[ObjectSource],
    policy: object_permissions.PermissionPolicy,
    *,
    base_dir: Path | str,
    include_objects: bool = False,
) -> dict[str, dict[str, Any]]:
    object_groups: dict[str, list[CollectionObjectSummary]] = defaultdict(list)
    for source in sources:
        collection = collection_for_object_id(source.object_id)
        if collection is None:
            continue
        object_groups[collection].append(_object_summary(source, base_dir=base_dir))

    policy_collections = {
        rule.collection
        for rule in policy.rules
        if rule.collection is not None and validate_collection_name(rule.collection)
    }

    collection_names = set(object_groups) | policy_collections
    summaries: dict[str, dict[str, Any]] = {}
    for collection in collection_names:
        objects = sorted(object_groups.get(collection, []), key=lambda item: item.object_id)
        summaries[collection] = _collection_summary(
            collection,
            objects,
            policy,
            include_objects=include_objects,
        )

    return summaries


def _object_summary(
    source: ObjectSource,
    *,
    base_dir: Path | str,
) -> CollectionObjectSummary:
    state = object_state.get_object_state(source.object_id, base_dir=base_dir)
    files = object_files.list_object_files(source.object_id, base_dir=base_dir)

    return CollectionObjectSummary(
        object_id=source.object_id,
        path=source.relative_path.as_posix(),
        owner=_object_owner(source.object_id),
        kind=source.kind,
        state_count=len(state),
        has_logs=_has_logs(source.object_id, base_dir=base_dir),
        file_count=len(files),
    )


def _collection_summary(
    collection: str,
    objects: list[CollectionObjectSummary],
    policy: object_permissions.PermissionPolicy,
    *,
    include_objects: bool,
) -> dict[str, Any]:
    owners = sorted({item.owner for item in objects})
    kinds = Counter(item.kind for item in objects)
    summary = {
        "name": collection,
        "object_count": len(objects),
        "file_count": sum(item.file_count for item in objects),
        "state_object_count": sum(1 for item in objects if item.state_count > 0),
        "log_object_count": sum(1 for item in objects if item.has_logs),
        "owners": owners,
        "kinds": dict(sorted(kinds.items())),
        "permission": _permission_summary(collection, policy),
    }
    if include_objects:
        summary["objects"] = [item.to_dict() for item in objects]
    return summary


def _permission_summary(
    collection: str,
    policy: object_permissions.PermissionPolicy,
) -> dict[str, Any]:
    rules = [rule for rule in policy.rules if rule.collection == collection]
    actions = sorted({action for rule in rules for action in rule.actions})
    principals = sorted({rule.principal for rule in rules})
    return {
        "access_mode": policy.access_mode,
        "rule_count": len(rules),
        "allow_count": sum(1 for rule in rules if rule.effect == "allow"),
        "deny_count": sum(1 for rule in rules if rule.effect == "deny"),
        "actions": actions,
        "principals": principals,
    }


def _object_owner(object_id: str) -> str:
    parsed = parse_user_object_id(object_id)
    if parsed is None:
        return "system"
    user_id, _ = parsed
    return str(user_id)


def _has_logs(object_id: str, *, base_dir: Path | str) -> bool:
    log_dir = object_logs.object_log_dir(object_id, base_dir)
    if not log_dir.exists() or not log_dir.is_dir():
        return False

    if (log_dir / object_logs.LOG_FILE).is_file():
        return True

    return any(path.is_file() for path in log_dir.glob("log-*.tsv*"))
