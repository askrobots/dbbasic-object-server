"""Pending-reconcile records for DBBASIC package upgrades.

Phase 1 (object_package_baselines) makes "is this customized?" a computable
question. Phase 2 uses that answer on upgrade: per object/schema, a
three-way compare of the old baseline, the live artifact, and the newly
shipped artifact decides whether to fast-forward, keep the customization, or
park a conflict. This module is the park: a JSON-file store of
pending-reconcile records that hold both versions until a human (or script)
picks "keep mine" or "take theirs". See docs/upgrade-and-customization.md
(Rule 1: Reconcile, Don't Replace).

The engine that produces these records lives in object_packages.py
(install_package); this module only stores and resolves them.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

import object_package_baselines
import object_schemas
import object_source
import object_versions
from object_versions import DEFAULT_DATA_DIR

RECONCILES_DIR = "reconciles"
RECONCILE_CHOICES = ("keep_mine", "take_theirs")

_RECONCILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _new_id() -> str:
    return uuid.uuid4().hex


def validate_reconcile_id(reconcile_id: Any) -> bool:
    """Return True when reconcile_id is a safe, traversal-proof identifier."""
    if not isinstance(reconcile_id, str):
        return False
    return bool(_RECONCILE_ID_RE.fullmatch(reconcile_id))


def reconcile_path(reconcile_id: str, *, base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    """Return the path to a reconcile record file, or raise on an invalid id."""
    if not validate_reconcile_id(reconcile_id):
        raise ValueError(f"Invalid reconcile id: {reconcile_id}")
    return Path(base_dir) / RECONCILES_DIR / f"{reconcile_id}.json"


def create_reconcile(
    *,
    package: str,
    target_version: str,
    baseline_version: str | None,
    artifact: Mapping[str, Any],
    mine: str,
    theirs: str,
    base_sha: str | None,
    created_at: str | None = None,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    """Park a conflict: build, atomic-write, and return a pending-reconcile record."""
    record = {
        "id": _new_id(),
        "package": package,
        "target_version": target_version,
        "baseline_version": baseline_version,
        "artifact": dict(artifact),
        "created_at": created_at,
        "status": "pending",
        "resolution": None,
        "base_sha": base_sha,
        "mine": mine,
        "theirs": theirs,
    }
    _write_reconcile(record, reconcile_path(record["id"], base_dir=base_dir))
    return record


def get_reconcile(reconcile_id: str, *, base_dir: Path | str = DEFAULT_DATA_DIR) -> dict[str, Any] | None:
    """Return one reconcile record, or None if absent/invalid/unreadable."""
    if not validate_reconcile_id(reconcile_id):
        return None
    path = reconcile_path(reconcile_id, base_dir=base_dir)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def list_reconciles(
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    status: str | None = None,
    package: str | None = None,
) -> list[dict[str, Any]]:
    """Return reconcile records, optionally filtered, sorted by created_at then id."""
    root = Path(base_dir) / RECONCILES_DIR
    if not root.is_dir():
        return []

    records: list[dict[str, Any]] = []
    for path in root.glob("*.json"):
        if not validate_reconcile_id(path.stem):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if status is not None and record.get("status") != status:
            continue
        if package is not None and record.get("package") != package:
            continue
        records.append(record)

    records.sort(key=lambda record: (record.get("created_at") or "", record.get("id") or ""))
    return records


def count_pending(*, base_dir: Path | str = DEFAULT_DATA_DIR, package: str | None = None) -> int:
    """Return the number of pending reconciles, optionally scoped to a package."""
    return len(list_reconciles(base_dir=base_dir, status="pending", package=package))


def resolve_reconcile(
    reconcile_id: str,
    choice: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    object_roots: Iterable[Path] | None = None,
    resolved_at: str | None = None,
) -> dict[str, Any]:
    """Resolve a pending reconcile by keeping the live artifact or taking the shipped one."""
    if choice not in RECONCILE_CHOICES:
        raise ValueError(f"Invalid reconcile choice: {choice}")

    record = get_reconcile(reconcile_id, base_dir=base_dir)
    if record is None:
        raise ValueError(f"Reconcile not found: {reconcile_id}")
    if record.get("status") != "pending":
        raise ValueError(f"Reconcile already resolved: {reconcile_id}")

    base = Path(base_dir)
    artifact = record["artifact"]
    kind = artifact.get("kind")

    if kind == "object":
        sha_theirs = object_package_baselines.sha256_text(record["theirs"])
    elif kind == "schema":
        sha_theirs = object_package_baselines.canonical_schema_hash(json.loads(record["theirs"]))
    else:
        raise ValueError(f"Unknown reconcile artifact kind: {kind}")

    if choice == "take_theirs":
        if kind == "object":
            object_source.update_object_source(
                object_id=artifact["id"],
                new_code=record["theirs"],
                author="reconcile",
                message=f"take-theirs from {record['package']} {record['target_version']}",
                roots=object_roots,
                version_manager=object_versions.VersionManager(base),
            )
        else:
            object_schemas.replace_schema(
                artifact["collection"],
                json.loads(record["theirs"]),
                base_dir=base,
            )

    key = artifact["id"] if kind == "object" else artifact["collection"]
    object_package_baselines.update_artifact(
        record["package"],
        kind=kind,
        key=key,
        sha=sha_theirs,
        version=record["target_version"],
        base_dir=base,
    )

    resolved = dict(record)
    resolved["status"] = "resolved"
    resolved["resolution"] = {"choice": choice, "resolved_at": resolved_at}
    _write_reconcile(resolved, reconcile_path(reconcile_id, base_dir=base_dir))
    return resolved


def _write_reconcile(record: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
        os.replace(tmp_name, path)
    finally:
        try:
            Path(tmp_name).unlink()
        except FileNotFoundError:
            pass
