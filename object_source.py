"""Source file operations for DBBASIC objects.

This module connects object ID resolution with source version storage. It does
not reload runtime objects or enforce HTTP permissions; those belong in the
server/runtime layers that call this code.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable

HTTP_METHOD_NAMES = ("GET", "POST", "PUT", "DELETE")

from object_namespace import (
    get_object_roots,
    object_id_from_path,
    parse_user_object_id,
    resolve_object_id,
    validate_object_id,
)
from object_versions import InvalidObjectIdError, VersionManager, VersionNotFoundError


class ObjectSourceError(Exception):
    """Base exception for object source operations."""


class ObjectSourceNotFoundError(ObjectSourceError):
    """Raised when an object ID does not resolve to a source file."""


class ObjectSourceExistsError(ObjectSourceError):
    """Raised when an object source already exists."""


def get_object_source(object_id: str, roots: Iterable[Path] | None = None) -> str:
    """Read source text for an existing object."""
    source_path = _resolve_existing_source(object_id, roots)
    return source_path.read_text()


def source_method_report(code: str) -> tuple[list[str], list[str]]:
    """Return (executable HTTP methods, authoring warnings) for object source.

    Detection is static (AST), so it runs safely at write time and lets the
    create/update responses tell authors — human or AI — that a saved object
    cannot execute, instead of leaving that discovery to the first request.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [], [f"Source has a Python syntax error: {exc.msg} (line {exc.lineno})"]

    methods = sorted(
        {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in HTTP_METHOD_NAMES
        }
    )
    if not methods:
        return [], [
            "Source defines no HTTP methods; the object cannot execute. "
            "Define GET(request), POST(request), PUT(request), or DELETE(request) "
            "at module top level (see docs/object-authoring.md)."
        ]
    return methods, []


def create_object_source(
    object_id: str,
    code: str,
    author: str,
    message: str,
    roots: Iterable[Path] | None = None,
    version_manager: VersionManager | None = None,
    correlation_id: str | None = None,
) -> int:
    """Create a new source file, save its first version, and return its version ID."""
    source_path = _resolve_new_source_path(object_id, roots)
    manager = version_manager or VersionManager()

    version_id = manager.save_version(
        object_id=object_id,
        content=code,
        author=author,
        message=message,
        correlation_id=correlation_id,
    )
    source_path.parent.mkdir(parents=True, exist_ok=True)
    _write_source(source_path, code)
    return version_id


def update_object_source(
    object_id: str,
    new_code: str,
    author: str,
    message: str,
    roots: Iterable[Path] | None = None,
    version_manager: VersionManager | None = None,
    correlation_id: str | None = None,
) -> int:
    """Save a new source version and write it to the object file."""
    source_path = _resolve_existing_source(object_id, roots)
    manager = version_manager or VersionManager()

    version_id = manager.save_version(
        object_id=object_id,
        content=new_code,
        author=author,
        message=message,
        correlation_id=correlation_id,
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
    correlation_id: str | None = None,
) -> int:
    """Rollback source by creating a new version and writing its content."""
    source_path = _resolve_existing_source(object_id, roots)
    manager = version_manager or VersionManager()

    new_version_id = manager.rollback(
        object_id=object_id,
        to_version=to_version,
        author=author,
        message=message,
        correlation_id=correlation_id,
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


def _resolve_new_source_path(object_id: str, roots: Iterable[Path] | None) -> Path:
    if not validate_object_id(object_id):
        raise InvalidObjectIdError(f"Invalid object ID: {object_id}")

    search_roots = list(roots) if roots is not None else get_object_roots()
    if not search_roots:
        raise InvalidObjectIdError("No object source root configured")

    if resolve_object_id(object_id, roots=search_roots) is not None:
        raise ObjectSourceExistsError(f"Object source already exists: {object_id}")

    root = search_roots[0]
    parsed_user = parse_user_object_id(object_id)
    if parsed_user:
        user_id, name = parsed_user
        source_path = root / "users" / str(user_id) / f"{name}.py"
    elif "_" in object_id:
        category, name = object_id.split("_", 1)
        source_path = root / category / f"{name}.py"
    else:
        source_path = root / f"{object_id}.py"

    try:
        derived_object_id = object_id_from_path(source_path, root)
    except ValueError as exc:
        raise InvalidObjectIdError(f"Invalid object ID: {object_id}")
    if derived_object_id != object_id:
        raise InvalidObjectIdError(f"Invalid object ID: {object_id}")

    return source_path


def _write_source(source_path: Path, content: str) -> None:
    temp_path = source_path.with_name(f".{source_path.name}.tmp")
    temp_path.write_text(content)
    temp_path.replace(source_path)
