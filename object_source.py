"""Source file operations for DBBASIC objects.

This module connects object ID resolution with source version storage. It does
not reload runtime objects or enforce HTTP permissions; those belong in the
server/runtime layers that call this code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from object_namespace import resolve_object_id, validate_object_id
from object_versions import InvalidObjectIdError, VersionManager, VersionNotFoundError


class ObjectSourceError(Exception):
    """Base exception for object source operations."""


class ObjectSourceNotFoundError(ObjectSourceError):
    """Raised when an object ID does not resolve to a source file."""


def get_object_source(object_id: str, roots: Iterable[Path] | None = None) -> str:
    """Read source text for an existing object."""
    source_path = _resolve_existing_source(object_id, roots)
    return source_path.read_text()


def update_object_source(
    object_id: str,
    new_code: str,
    author: str,
    message: str,
    roots: Iterable[Path] | None = None,
    version_manager: VersionManager | None = None,
) -> int:
    """Save a new source version and write it to the object file."""
    source_path = _resolve_existing_source(object_id, roots)
    manager = version_manager or VersionManager()

    version_id = manager.save_version(
        object_id=object_id,
        content=new_code,
        author=author,
        message=message,
    )
    _write_source(source_path, new_code)
    return version_id


def rollback_object_source(
    object_id: str,
    to_version: int,
    author: str,
    message: str,
    roots: Iterable[Path] | None = None,
    version_manager: VersionManager | None = None,
) -> int:
    """Rollback source by creating a new version and writing its content."""
    source_path = _resolve_existing_source(object_id, roots)
    manager = version_manager or VersionManager()

    new_version_id = manager.rollback(
        object_id=object_id,
        to_version=to_version,
        author=author,
        message=message,
    )
    new_version = manager.get_version(object_id, new_version_id)
    if new_version is None:
        raise VersionNotFoundError(f"Version {new_version_id} not found for object {object_id}")

    _write_source(source_path, new_version["content"])
    return new_version_id


def _resolve_existing_source(object_id: str, roots: Iterable[Path] | None) -> Path:
    if not validate_object_id(object_id):
        raise InvalidObjectIdError(f"Invalid object ID: {object_id}")

    source_path = resolve_object_id(object_id, roots=roots)
    if source_path is None:
        raise ObjectSourceNotFoundError(f"Object source not found: {object_id}")
    return source_path


def _write_source(source_path: Path, content: str) -> None:
    temp_path = source_path.with_name(f".{source_path.name}.tmp")
    temp_path.write_text(content)
    temp_path.replace(source_path)
