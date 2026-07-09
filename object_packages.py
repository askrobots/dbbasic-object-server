"""DBBASIC package manifest discovery, dry-run planning, and gated installs.

Packages are installable bundles of objects, schemas, permissions, seed data,
and migrations. Installs are intentionally conservative: object and schema files
can be created or replaced, seed files can be created, and permissions/migrations
wait for explicit merge/run semantics.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import object_collections
import object_namespace
import object_package_baselines
import object_permission_store
import object_permissions
import object_reconciles
import object_schemas
import object_source
from object_namespace import get_object_roots, object_id_from_path, resolve_object_id, validate_object_id
from object_versions import DEFAULT_DATA_DIR

PACKAGES_DIR = "packages"
MANIFEST_FILE = "dbbasic-package.json"
PACKAGE_MIGRATIONS_DIR = "package_migrations"

_PACKAGE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_MIGRATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class InvalidPackageIdError(ValueError):
    """Raised when a package id is not safe for routes or storage."""


class PackageNotFoundError(LookupError):
    """Raised when a package directory or manifest is missing."""


class InvalidPackageManifestError(ValueError):
    """Raised when a package manifest is invalid."""


class PackageInstallError(RuntimeError):
    """Raised when a package install would be unsafe or unsupported."""


def validate_package_id(package_id: str) -> bool:
    """Return True when a package id is route-safe."""
    if not isinstance(package_id, str):
        return False
    return bool(_PACKAGE_ID_RE.fullmatch(package_id))


def list_packages(
    *,
    root: Path | str = PACKAGES_DIR,
) -> list[dict[str, Any]]:
    """Return package summaries for all package manifests under root."""
    packages_root = Path(root)
    if not packages_root.exists() or not packages_root.is_dir():
        return []

    packages = []
    for path in sorted(packages_root.iterdir(), key=lambda item: item.name):
        if not path.is_dir() or not validate_package_id(path.name):
            continue
        manifest_path = path / MANIFEST_FILE
        if manifest_path.is_file():
            packages.append(_package_summary(_load_package(path.name, path)))
    return packages


def get_package(
    package_id: str,
    *,
    root: Path | str = PACKAGES_DIR,
) -> dict[str, Any]:
    """Return one normalized package manifest."""
    package_dir = _package_dir(package_id, root)
    return _load_package(package_id, package_dir)


def dry_run_package(
    package_id: str,
    *,
    root: Path | str = PACKAGES_DIR,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    object_roots: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Return a non-mutating package install plan."""
    package_dir = _package_dir(package_id, root)
    package = _load_package(package_id, package_dir)
    base = Path(base_dir)

    warnings: list[str] = []
    objects = [
        _object_change(entry, package_dir=package_dir, object_roots=object_roots, warnings=warnings)
        for entry in package["objects"]
    ]
    schemas = [
        _schema_change(entry, package_dir=package_dir, base_dir=base, warnings=warnings)
        for entry in package["schemas"]
    ]
    permissions = [
        _permission_change(
            entry,
            package_id=package_id,
            package_dir=package_dir,
            base_dir=base,
            warnings=warnings,
        )
        for entry in package["permissions"]
    ]
    seed = [
        _seed_change(entry, package_dir=package_dir, base_dir=base, warnings=warnings)
        for entry in package["seed"]
    ]
    migrations = [
        _migration_change(entry, package=package_id, package_dir=package_dir, base_dir=base, warnings=warnings)
        for entry in package["migrations"]
    ]

    return {
        "package": _package_summary(package),
        "mode": "dry_run",
        "install_enabled": False,
        "safe_to_install": not warnings,
        "objects": objects,
        "schemas": schemas,
        "permissions": permissions,
        "seed": seed,
        "migrations": migrations,
        "warnings": warnings,
    }


def install_package(
    package_id: str,
    *,
    root: Path | str = PACKAGES_DIR,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    object_roots: Iterable[Path] | None = None,
    allow_replace: bool = False,
    force: bool = False,
    before_write: Callable[[Mapping[str, Any]], Mapping[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Install a package using the conservative public write contract.

    On an upgrade (an object/schema that already exists), writes are no
    longer blind: each artifact is three-way compared against its recorded
    baseline (see object_package_baselines) and its live content. Pristine
    artifacts fast-forward, customized-but-unchanged artifacts are kept, and
    genuine conflicts are parked as pending-reconcile records (never
    overwritten) unless force=True. See docs/upgrade-and-customization.md
    (Rule 1: Reconcile, Don't Replace).
    """
    # force implies allow_replace: forcing only makes sense when you may
    # touch existing artifacts, so the replace-without-allow_replace blocker
    # still applies unless force is also set.
    allow_replace = allow_replace or force
    roots = list(object_roots) if object_roots is not None else get_object_roots()
    if not roots:
        raise PackageInstallError("Package installs require at least one object root")

    package_dir = _package_dir(package_id, root)
    package = _load_package(package_id, package_dir)
    base = Path(base_dir)
    plan = dry_run_package(package_id, root=root, base_dir=base, object_roots=roots)

    blockers = _install_blockers(plan, package=package, allow_replace=allow_replace)
    if blockers:
        raise PackageInstallError("; ".join(blockers))

    object_writes = []
    for entry, planned in zip(package["objects"], plan["objects"], strict=True):
        source = _package_file(package_dir, entry["path"])
        existing = resolve_object_id(entry["id"], roots)
        destination_root = _root_for_path(existing, roots) if existing is not None else roots[0]
        if destination_root is None:
            raise PackageInstallError(f"Existing object is outside configured object roots: {entry['id']}")
        destination = existing or _object_destination(entry, destination_root)
        _ensure_inside(destination, destination_root, label="object")
        try:
            mapped_id = object_id_from_path(destination, destination_root)
        except ValueError as exc:
            raise PackageInstallError(
                f"Package object path is not a valid object destination: {entry['path']}"
            ) from exc
        if mapped_id != entry["id"]:
            raise PackageInstallError(
                f"Package object path does not map to object id {entry['id']}: {entry['path']}"
            )
        object_writes.append((planned, destination, destination_root, source.read_bytes()))

    schema_writes = []
    for entry, planned in zip(package["schemas"], plan["schemas"], strict=True):
        source = _package_file(package_dir, entry["path"])
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PackageInstallError(f"Package schema contains invalid JSON: {entry['path']}") from exc
        try:
            normalized = object_schemas.normalize_schema(entry["collection"], payload, source="manual")
        except ValueError as exc:
            raise PackageInstallError(f"Package schema is invalid: {entry['path']}") from exc
        schema_writes.append((entry, planned, normalized))

    seed_writes = []
    for entry, planned in zip(package["seed"], plan["seed"], strict=True):
        source = _package_file(package_dir, entry["path"])
        destination = base / "collections" / entry["collection"] / "records.tsv"
        _ensure_inside(destination, base / "collections", label="seed")
        seed_writes.append((entry, planned, destination, source.read_bytes()))

    permission_writes = []
    for entry, planned in zip(package["permissions"], plan["permissions"], strict=True):
        rules = _load_permission_rules(package_dir, entry, package_id=package_id)
        permission_writes.append((planned, rules))

    restore_point = before_write(plan) if before_write is not None else None

    existing_baseline = object_package_baselines.load_baseline(package_id, base_dir=base) or {}
    base_objects = existing_baseline.get("objects") or {}
    base_schemas = existing_baseline.get("schemas") or {}
    baseline_version = existing_baseline.get("version")

    reconciles: list[str] = []
    new_baseline_objects: dict[str, str] = {}
    new_baseline_schemas: dict[str, str] = {}

    installed_objects = []
    for planned, destination, destination_root, content in object_writes:
        object_id = planned["id"]
        extra: dict[str, Any] = {}

        if planned["action"] == "create":
            _write_file_atomic_bytes(destination, content)
            status = "written"
            new_baseline_objects[object_id] = object_package_baselines.sha256_text(content.decode("utf-8"))
        else:
            new_text = content.decode("utf-8")
            new_sha = object_package_baselines.sha256_text(new_text)
            live_text = object_source.get_object_source(object_id, roots)
            live_sha = object_package_baselines.sha256_text(live_text)
            old_sha = base_objects.get(object_id)

            if live_sha == new_sha:
                status = "unchanged"
                new_baseline_objects[object_id] = new_sha
            elif old_sha is not None and live_sha == old_sha:
                _write_file_atomic_bytes(destination, content)
                status = "updated"
                new_baseline_objects[object_id] = new_sha
            elif old_sha is not None and new_sha == old_sha:
                status = "kept"
                new_baseline_objects[object_id] = old_sha
            elif force:
                _write_file_atomic_bytes(destination, content)
                status = "forced"
                new_baseline_objects[object_id] = new_sha
            else:
                rec = object_reconciles.create_reconcile(
                    package=package_id,
                    target_version=package["version"],
                    baseline_version=baseline_version,
                    artifact={"kind": "object", "id": object_id},
                    mine=live_text,
                    theirs=new_text,
                    base_sha=old_sha,
                    base_dir=base,
                )
                reconciles.append(rec["id"])
                status = "conflict"
                extra["reconcile_id"] = rec["id"]
                new_baseline_objects[object_id] = old_sha if old_sha is not None else live_sha

        installed_objects.append(
            {
                **planned,
                "status": status,
                "destination": _relative_display_path(destination, destination_root),
                **extra,
            }
        )

    installed_schemas = []
    for entry, planned, normalized in schema_writes:
        collection = entry["collection"]
        new_sha = object_package_baselines.canonical_schema_hash(normalized)
        extra: dict[str, Any] = {}

        live = None
        if planned["action"] == "replace":
            try:
                live = object_schemas.get_schema(collection, base_dir=base, roots=roots)
            except object_schemas.SchemaNotFoundError:
                live = None

        if live is None:
            object_schemas.replace_schema(collection, normalized, base_dir=base)
            status = "written"
            new_baseline_schemas[collection] = new_sha
        else:
            live_sha = object_package_baselines.canonical_schema_hash(live)
            old_sha = base_schemas.get(collection)

            if live_sha == new_sha:
                status = "unchanged"
                new_baseline_schemas[collection] = new_sha
            elif old_sha is not None and live_sha == old_sha:
                object_schemas.replace_schema(collection, normalized, base_dir=base)
                status = "updated"
                new_baseline_schemas[collection] = new_sha
            elif old_sha is not None and new_sha == old_sha:
                status = "kept"
                new_baseline_schemas[collection] = old_sha
            elif force:
                object_schemas.replace_schema(collection, normalized, base_dir=base)
                status = "forced"
                new_baseline_schemas[collection] = new_sha
            else:
                rec = object_reconciles.create_reconcile(
                    package=package_id,
                    target_version=package["version"],
                    baseline_version=baseline_version,
                    artifact={"kind": "schema", "collection": collection},
                    mine=json.dumps(live, indent=2, sort_keys=True),
                    theirs=json.dumps(normalized, indent=2, sort_keys=True),
                    base_sha=old_sha,
                    base_dir=base,
                )
                reconciles.append(rec["id"])
                status = "conflict"
                extra["reconcile_id"] = rec["id"]
                new_baseline_schemas[collection] = old_sha if old_sha is not None else live_sha

        installed_schemas.append(
            {
                **planned,
                "status": status,
                "destination": f"schemas/{collection}.json",
                **extra,
            }
        )

    installed_seed = []
    for entry, planned, destination, content in seed_writes:
        # Seed is install-once. If the collection already holds records, an
        # upgrade must preserve them, so skip the seed write rather than
        # clobber live data. Fresh installs (no records.tsv yet) still seed.
        if planned.get("installed"):
            installed_seed.append(
                {
                    **planned,
                    "status": "skipped",
                    "reason": "collection already has data; seed preserved",
                    "destination": f"collections/{entry['collection']}/records.tsv",
                }
            )
            continue
        _write_file_atomic_bytes(destination, content)
        installed_seed.append(
            {
                **planned,
                "status": "written",
                "destination": f"collections/{entry['collection']}/records.tsv",
            }
        )

    installed_permissions = []
    for planned, rules in permission_writes:
        total, added = _merge_permission_rules(rules, base_dir=base)
        installed_permissions.append(
            {
                **planned,
                "status": "merged",
                "rules": total,
                "new_rules": added,
            }
        )

    object_package_baselines.record_baseline(
        package_id,
        version=package["version"],
        objects=new_baseline_objects,
        schemas=new_baseline_schemas,
        base_dir=base,
    )

    result = {
        "package": plan["package"],
        "mode": "install",
        "install_enabled": True,
        "allow_replace": allow_replace,
        "safe_to_install": True,
        "objects": installed_objects,
        "schemas": installed_schemas,
        "permissions": installed_permissions,
        "seed": installed_seed,
        "migrations": plan["migrations"],
        "reconciles": reconciles,
        "warnings": [],
    }
    if restore_point is not None:
        result["restore_point"] = dict(restore_point)
    return result


def package_status(
    package_id: str,
    *,
    root: Path | str = PACKAGES_DIR,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    object_roots: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Return install/customization status for a package's baselined artifacts."""
    roots = list(object_roots) if object_roots is not None else get_object_roots()
    base = Path(base_dir)

    package = _load_package(package_id, _package_dir(package_id, root))
    summary = _package_summary(package)
    summary["pending_reconciles"] = object_reconciles.count_pending(base_dir=base, package=package_id)

    baseline = object_package_baselines.load_baseline(package_id, base_dir=base)
    if baseline is None:
        summary["installed"] = False
        summary["customized"] = False
        summary["artifacts"] = []
        return summary

    artifacts: list[dict[str, Any]] = []
    any_customized = False

    for object_id, base_sha in (baseline.get("objects") or {}).items():
        try:
            live = object_source.get_object_source(object_id, roots)
        except (FileNotFoundError, LookupError, OSError, ValueError, object_source.ObjectSourceError):
            state = "removed"
        else:
            state = "pristine" if object_package_baselines.sha256_text(live) == base_sha else "customized"
        if state == "customized":
            any_customized = True
        artifacts.append(
            {
                "kind": "object",
                "id": object_id,
                "state": state,
                "overridden": object_namespace.has_override(object_id),
            }
        )

    for collection, base_sha in (baseline.get("schemas") or {}).items():
        try:
            live = object_schemas.get_schema(collection, base_dir=base, roots=roots)
        except (object_schemas.SchemaNotFoundError, LookupError, OSError, ValueError):
            state = "removed"
        else:
            state = (
                "pristine"
                if object_package_baselines.canonical_schema_hash(live) == base_sha
                else "customized"
            )
        if state == "customized":
            any_customized = True
        artifacts.append({"kind": "schema", "collection": collection, "state": state})

    summary["installed"] = True
    summary["installed_version"] = baseline.get("version")
    summary["customized"] = any_customized
    summary["artifacts"] = artifacts
    return summary


def _package_dir(package_id: str, root: Path | str) -> Path:
    if not validate_package_id(package_id):
        raise InvalidPackageIdError(f"Invalid package id: {package_id}")

    package_dir = Path(root) / package_id
    manifest_path = package_dir / MANIFEST_FILE
    if not package_dir.is_dir() or not manifest_path.is_file():
        raise PackageNotFoundError(f"Package not found: {package_id}")
    return package_dir


def _load_package(package_id: str, package_dir: Path) -> dict[str, Any]:
    manifest_path = package_dir / MANIFEST_FILE
    try:
        payload = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        raise InvalidPackageManifestError(f"Package manifest contains invalid JSON: {package_id}") from exc

    return _normalize_manifest(package_id, payload)


def _normalize_manifest(package_id: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise InvalidPackageManifestError(f"Package manifest must contain an object: {package_id}")

    manifest_id = _required_text(payload, "id", package_id=package_id)
    if manifest_id != package_id:
        raise InvalidPackageManifestError(
            f"Package manifest id does not match directory: {package_id}"
        )
    if not validate_package_id(manifest_id):
        raise InvalidPackageIdError(f"Invalid package id: {manifest_id}")

    version = _required_text(payload, "version", package_id=package_id)
    if not _VERSION_RE.fullmatch(version):
        raise InvalidPackageManifestError(f"Invalid package version: {package_id}")

    return {
        "id": manifest_id,
        "name": _required_text(payload, "name", package_id=package_id),
        "version": version,
        "description": _optional_text(payload.get("description")),
        "compatibility": _mapping_field(payload.get("compatibility"), package_id=package_id),
        "dependencies": _normalize_dependencies(payload.get("dependencies", []), package_id=package_id),
        "objects": _normalize_objects(payload.get("objects", []), package_id=package_id),
        "schemas": _normalize_collection_paths(
            payload.get("schemas", []),
            package_id=package_id,
            collection_key="collection",
            section="schemas",
        ),
        "permissions": _normalize_path_entries(
            payload.get("permissions", []),
            package_id=package_id,
            section="permissions",
        ),
        "seed": _normalize_collection_paths(
            payload.get("seed", []),
            package_id=package_id,
            collection_key="collection",
            section="seed",
        ),
        "migrations": _normalize_migrations(payload.get("migrations", []), package_id=package_id),
    }


def _package_summary(package: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": package["id"],
        "name": package["name"],
        "version": package["version"],
        "description": package.get("description"),
        "status": "available",
        "object_count": len(package["objects"]),
        "schema_count": len(package["schemas"]),
        "permission_count": len(package["permissions"]),
        "seed_count": len(package["seed"]),
        "migration_count": len(package["migrations"]),
        "dependency_count": len(package["dependencies"]),
    }


def _normalize_objects(payload: Any, *, package_id: str) -> list[dict[str, str]]:
    entries = _list_field(payload, package_id=package_id, section="objects")
    normalized = []
    for entry in entries:
        mapping = _entry_mapping(entry, package_id=package_id, section="objects")
        object_id = _required_text(mapping, "id", package_id=package_id)
        if not validate_object_id(object_id):
            raise InvalidPackageManifestError(f"Invalid package object id: {object_id}")
        normalized.append(
            {
                "id": object_id,
                "path": _safe_relative_path(
                    _required_text(mapping, "path", package_id=package_id),
                    package_id=package_id,
                    section="objects",
                ),
            }
        )
    return normalized


def _normalize_collection_paths(
    payload: Any,
    *,
    package_id: str,
    collection_key: str,
    section: str,
) -> list[dict[str, str]]:
    entries = _list_field(payload, package_id=package_id, section=section)
    normalized = []
    for entry in entries:
        mapping = _entry_mapping(entry, package_id=package_id, section=section)
        collection = _required_text(mapping, collection_key, package_id=package_id)
        if not object_collections.validate_collection_name(collection):
            raise InvalidPackageManifestError(f"Invalid package collection: {collection}")
        normalized.append(
            {
                collection_key: collection,
                "path": _safe_relative_path(
                    _required_text(mapping, "path", package_id=package_id),
                    package_id=package_id,
                    section=section,
                ),
            }
        )
    return normalized


def _normalize_path_entries(
    payload: Any,
    *,
    package_id: str,
    section: str,
) -> list[dict[str, str]]:
    entries = _list_field(payload, package_id=package_id, section=section)
    normalized = []
    for entry in entries:
        mapping = _entry_mapping(entry, package_id=package_id, section=section)
        normalized.append(
            {
                "path": _safe_relative_path(
                    _required_text(mapping, "path", package_id=package_id),
                    package_id=package_id,
                    section=section,
                )
            }
        )
    return normalized


def _normalize_migrations(payload: Any, *, package_id: str) -> list[dict[str, str]]:
    entries = _list_field(payload, package_id=package_id, section="migrations")
    normalized = []
    for entry in entries:
        mapping = _entry_mapping(entry, package_id=package_id, section="migrations")
        migration_id = _required_text(mapping, "id", package_id=package_id)
        if not _MIGRATION_ID_RE.fullmatch(migration_id):
            raise InvalidPackageManifestError(f"Invalid package migration id: {migration_id}")
        normalized.append(
            {
                "id": migration_id,
                "path": _safe_relative_path(
                    _required_text(mapping, "path", package_id=package_id),
                    package_id=package_id,
                    section="migrations",
                ),
            }
        )
    return normalized


def _normalize_dependencies(payload: Any, *, package_id: str) -> list[dict[str, str | None]]:
    entries = _list_field(payload, package_id=package_id, section="dependencies")
    normalized = []
    for entry in entries:
        if isinstance(entry, str):
            dependency_id = entry
            version = None
        else:
            mapping = _entry_mapping(entry, package_id=package_id, section="dependencies")
            dependency_id = _required_text(mapping, "id", package_id=package_id)
            version = _optional_text(mapping.get("version"))
        if not validate_package_id(dependency_id):
            raise InvalidPackageManifestError(f"Invalid package dependency id: {dependency_id}")
        normalized.append({"id": dependency_id, "version": version})
    return normalized


def _object_change(
    entry: Mapping[str, str],
    *,
    package_dir: Path,
    object_roots: Iterable[Path] | None,
    warnings: list[str],
) -> dict[str, Any]:
    file_status = _package_file_status(package_dir, entry["path"])
    if not file_status["exists"]:
        warnings.append(f"Missing package object file: {entry['path']}")
    installed = resolve_object_id(entry["id"], object_roots) is not None
    return {
        "id": entry["id"],
        "path": entry["path"],
        "exists": file_status["exists"],
        "action": "replace" if installed else "create",
        "installed": installed,
    }


def _schema_change(
    entry: Mapping[str, str],
    *,
    package_dir: Path,
    base_dir: Path,
    warnings: list[str],
) -> dict[str, Any]:
    file_status = _package_file_status(package_dir, entry["path"])
    if not file_status["exists"]:
        warnings.append(f"Missing package schema file: {entry['path']}")
    installed = (base_dir / "schemas" / f"{entry['collection']}.json").is_file()
    return {
        "collection": entry["collection"],
        "path": entry["path"],
        "exists": file_status["exists"],
        "action": "replace" if installed else "create",
        "installed": installed,
    }


def _seed_change(
    entry: Mapping[str, str],
    *,
    package_dir: Path,
    base_dir: Path,
    warnings: list[str],
) -> dict[str, Any]:
    file_status = _package_file_status(package_dir, entry["path"])
    if not file_status["exists"]:
        warnings.append(f"Missing package seed file: {entry['path']}")
    installed = (base_dir / "collections" / entry["collection"] / "records.tsv").is_file()
    return {
        "collection": entry["collection"],
        "path": entry["path"],
        "exists": file_status["exists"],
        # Seed is install-once: on an upgrade where the collection already has
        # records, seeding is skipped so live data is preserved (not merged).
        "action": "skip" if installed else "create",
        "installed": installed,
    }


def _migration_change(
    entry: Mapping[str, str],
    *,
    package: str,
    package_dir: Path,
    base_dir: Path,
    warnings: list[str],
) -> dict[str, Any]:
    file_status = _package_file_status(package_dir, entry["path"])
    if not file_status["exists"]:
        warnings.append(f"Missing package migration file: {entry['path']}")
    marker = base_dir / PACKAGE_MIGRATIONS_DIR / package / f"{entry['id']}.json"
    applied = marker.is_file()
    return {
        "id": entry["id"],
        "path": entry["path"],
        "exists": file_status["exists"],
        "action": "skip" if applied else "apply",
        "applied": applied,
    }


def _permission_change(
    entry: Mapping[str, str],
    *,
    package_id: str,
    package_dir: Path,
    base_dir: Path,
    warnings: list[str],
) -> dict[str, Any]:
    change = {
        "path": entry["path"],
        "action": "merge",
        "exists": False,
        "rules": 0,
        "new_rules": 0,
    }
    file_status = _package_file_status(package_dir, entry["path"])
    if not file_status["exists"]:
        warnings.append(f"Missing package permissions file: {entry['path']}")
        return change
    change["exists"] = True

    try:
        rules = _load_permission_rules(package_dir, entry, package_id=package_id)
    except PackageInstallError as exc:
        warnings.append(str(exc))
        return change

    change["rules"] = len(rules)
    try:
        policy = object_permission_store.load_policy(base_dir)
        existing_keys = {_rule_merge_key(rule) for rule in policy.rules}
    except ValueError:
        existing_keys = set()
    change["new_rules"] = sum(1 for rule in rules if _rule_merge_key(rule) not in existing_keys)
    return change


def _load_permission_rules(
    package_dir: Path,
    entry: Mapping[str, str],
    *,
    package_id: str,
) -> list[object_permissions.PermissionRule]:
    source = _package_file(package_dir, entry["path"])
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageInstallError(
            f"Package permissions file contains invalid JSON: {entry['path']}"
        ) from exc

    rules_payload = payload.get("rules") if isinstance(payload, dict) else payload
    if not isinstance(rules_payload, list):
        raise PackageInstallError(
            f"Package permissions file must contain a rules list: {entry['path']}"
        )

    rules = []
    for rule_payload in rules_payload:
        if not isinstance(rule_payload, dict):
            raise PackageInstallError(
                f"Package permission rules must be objects: {entry['path']}"
            )
        merged_payload = {**rule_payload, "package": package_id}
        try:
            rules.append(object_permissions.rule_from_dict(merged_payload))
        except ValueError as exc:
            raise PackageInstallError(
                f"Package permission rule is invalid in {entry['path']}: {exc}"
            ) from exc
    return rules


def _rule_merge_key(rule: object_permissions.PermissionRule) -> str:
    payload = object_permissions.rule_to_dict(rule)
    payload.pop("reason", None)
    payload.pop("package", None)
    return json.dumps(payload, sort_keys=True)


def _merge_permission_rules(
    rules: list[object_permissions.PermissionRule],
    *,
    base_dir: Path,
) -> tuple[int, int]:
    """Append new package rules to the policy; return (total, newly added)."""
    policy = object_permission_store.load_policy(base_dir)
    existing_keys = {_rule_merge_key(rule) for rule in policy.rules}

    added = []
    for rule in rules:
        if _rule_merge_key(rule) in existing_keys:
            continue
        added.append(rule)
        existing_keys.add(_rule_merge_key(rule))

    if added:
        merged = object_permissions.PermissionPolicy(
            access_mode=policy.access_mode,
            rules=tuple(policy.rules) + tuple(added),
            roles=policy.roles,
            user_roles=policy.user_roles,
            admin_roles=policy.admin_roles,
        )
        object_permission_store.save_policy(merged, base_dir=base_dir)

    return len(rules), len(added)


def _path_change(
    entry: Mapping[str, str],
    *,
    package_dir: Path,
    section: str,
    action: str,
    warnings: list[str],
) -> dict[str, Any]:
    file_status = _package_file_status(package_dir, entry["path"])
    if not file_status["exists"]:
        warnings.append(f"Missing package {section} file: {entry['path']}")
    return {
        "path": entry["path"],
        "exists": file_status["exists"],
        "action": action,
    }


def _package_file_status(package_dir: Path, relative_path: str) -> dict[str, bool]:
    package_root = package_dir.resolve()
    candidate = package_dir / relative_path
    resolved = candidate.resolve(strict=False)
    inside = resolved == package_root or package_root in resolved.parents
    return {
        "exists": inside and candidate.is_file(),
        "inside_package": inside,
    }


def _install_blockers(
    plan: Mapping[str, Any],
    *,
    package: Mapping[str, Any],
    allow_replace: bool,
) -> list[str]:
    blockers = [str(warning) for warning in plan.get("warnings", [])]

    if package["migrations"]:
        blockers.append("Package migration execution is not implemented yet")

    if not allow_replace:
        for entry in plan["objects"]:
            if entry["action"] == "replace":
                blockers.append(f"Object already exists; set allow_replace=true: {entry['id']}")
        for entry in plan["schemas"]:
            if entry["action"] == "replace":
                blockers.append(
                    f"Schema already exists; set allow_replace=true: {entry['collection']}"
                )

    # Existing seed data is NOT a blocker: seed is install-once, so an upgrade
    # of an already-installed package skips seeding and preserves live records
    # (see the seed apply step). This is what makes "upgrade the app, keep the
    # data" work — the package ships code + schema + a seed template, while the
    # records live outside the package in the server's data dir.

    return blockers


def _package_file(package_dir: Path, relative_path: str) -> Path:
    status = _package_file_status(package_dir, relative_path)
    if not status["inside_package"]:
        raise PackageInstallError(f"Package file escapes package directory: {relative_path}")
    if not status["exists"]:
        raise PackageInstallError(f"Package file does not exist: {relative_path}")
    return package_dir / relative_path


def _object_destination(entry: Mapping[str, str], object_root: Path) -> Path:
    relative = Path(entry["path"])
    if relative.parts and relative.parts[0] == "objects":
        relative = Path(*relative.parts[1:])
    if not relative.parts or relative.suffix != ".py":
        raise PackageInstallError(f"Package object path must point to a Python file: {entry['path']}")
    destination = object_root / relative
    _ensure_inside(destination, object_root, label="object")
    return destination


def _ensure_inside(path: Path, root: Path, *, label: str) -> None:
    resolved_path = path.resolve(strict=False)
    resolved_root = root.resolve(strict=False)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise PackageInstallError(f"Package {label} destination escapes its root: {path}") from exc


def _root_for_path(path: Path | None, roots: Iterable[Path]) -> Path | None:
    if path is None:
        return None
    for root in roots:
        try:
            _ensure_inside(path, root, label="object")
        except PackageInstallError:
            continue
        return root
    return None


def _write_file_atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_name = tmp.name
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    finally:
        if tmp_name:
            try:
                Path(tmp_name).unlink()
            except FileNotFoundError:
                pass


def _relative_display_path(path: Path, root: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return path.name


def _safe_relative_path(value: str, *, package_id: str, section: str) -> str:
    if "\x00" in value:
        raise InvalidPackageManifestError(f"Package {section} path contains a null byte: {package_id}")
    path = Path(value)
    if path.is_absolute() or not path.parts:
        raise InvalidPackageManifestError(f"Package {section} path must be relative: {package_id}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise InvalidPackageManifestError(f"Package {section} path is not safe: {package_id}")
    return path.as_posix()


def _required_text(payload: Mapping[str, Any], key: str, *, package_id: str) -> str:
    value = payload.get(key)
    text = _optional_text(value)
    if text is None:
        raise InvalidPackageManifestError(f"Package manifest requires '{key}': {package_id}")
    return text


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _mapping_field(value: Any, *, package_id: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise InvalidPackageManifestError(f"Package compatibility must be an object: {package_id}")
    return dict(value)


def _list_field(value: Any, *, package_id: str, section: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidPackageManifestError(f"Package {section} must be a list: {package_id}")
    return value


def _entry_mapping(value: Any, *, package_id: str, section: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidPackageManifestError(f"Package {section} entries must be objects: {package_id}")
    return value
