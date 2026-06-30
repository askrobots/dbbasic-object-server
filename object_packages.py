"""DBBASIC package manifest discovery and dry-run planning.

Packages are installable bundles of objects, schemas, permissions, seed data,
and migrations. This module only reads manifests and builds dry-run plans; it
does not mutate a live server.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

import object_collections
from object_namespace import resolve_object_id, validate_object_id
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
        _path_change(entry, package_dir=package_dir, section="permissions", action="merge", warnings=warnings)
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
        "action": "merge" if installed else "create",
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
