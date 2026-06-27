"""Object source discovery and object ID resolution.

This module is intentionally small and standalone while the public package
layout is being decided. It is the shared namespace contract for the daemon,
future HTTP server, tests, and companion tools.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal


DEFAULT_OBJECTS_DIR = "objects"
OBJECTS_DIR_ENV = "DBBASIC_OBJECTS_DIR"

_OBJECT_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_USER_OBJECT_RE = re.compile(r"^u_(\d+)_([A-Za-z][A-Za-z0-9_]{0,49})$")


@dataclass(frozen=True)
class ObjectSource:
    """A discovered object source file."""

    object_id: str
    path: Path
    relative_path: Path
    kind: Literal["system", "user"]


def get_object_roots() -> list[Path]:
    """Return object source roots in lookup order."""
    configured = os.environ.get(OBJECTS_DIR_ENV)
    if configured:
        return [Path(configured)]
    return [Path(DEFAULT_OBJECTS_DIR)]


def validate_object_id(object_id: str) -> bool:
    """Return True when an object ID is safe to resolve."""
    if not isinstance(object_id, str):
        return False
    return bool(_OBJECT_ID_RE.fullmatch(object_id))


def parse_user_object_id(object_id: str) -> tuple[int, str] | None:
    """Parse `u_{user_id}_{name}` object IDs."""
    if not validate_object_id(object_id):
        return None

    match = _USER_OBJECT_RE.fullmatch(object_id)
    if not match:
        return None
    return int(match.group(1)), match.group(2)


def is_user_object_id(object_id: str) -> bool:
    """Return True when an object ID belongs to a user namespace."""
    return parse_user_object_id(object_id) is not None


def resolve_object_id(object_id: str, roots: Iterable[Path] | None = None) -> Path | None:
    """Resolve an object ID to an existing source file."""
    if not validate_object_id(object_id):
        return None

    search_roots = list(roots) if roots is not None else get_object_roots()

    parsed_user = parse_user_object_id(object_id)
    if parsed_user:
        user_id, name = parsed_user
        for root in search_roots:
            candidate = root / "users" / str(user_id) / f"{name}.py"
            if _is_allowed_source_file(candidate, root):
                return candidate
        return None

    for candidate in _candidate_system_paths(object_id, search_roots):
        root = _candidate_root(candidate, search_roots)
        if root is not None and _is_allowed_source_file(candidate, root):
            return candidate

    for source in iter_object_sources(search_roots):
        if source.object_id == object_id:
            return source.path

    return None


def find_trigger_file(trigger_name: str, roots: Iterable[Path] | None = None) -> Path | None:
    """Find a known trigger object file under `objects/triggers/`."""
    if not _is_safe_source_stem(trigger_name):
        return None

    search_roots = list(roots) if roots is not None else get_object_roots()
    for root in search_roots:
        candidate = root / "triggers" / f"{trigger_name}.py"
        if _is_allowed_source_file(candidate, root):
            return candidate
    return None


def object_id_from_path(path: Path | str, root: Path | str) -> str:
    """Return the object ID represented by a source path under a root."""
    source_path = Path(path)
    source_root = Path(root)
    rel = _relative_to_root(source_path, source_root)

    if not _is_public_source_relative_path(rel):
        raise ValueError(f"Not a public object source: {source_path}")

    parts = rel.parts
    if parts[0] == "users":
        if len(parts) != 3:
            raise ValueError(f"Invalid user object source path: {source_path}")
        user_id = parts[1]
        name = Path(parts[2]).stem
        object_id = f"u_{user_id}_{name}"
    else:
        path_without_suffix = rel.with_suffix("")
        object_id = "_".join(path_without_suffix.parts)

    if not validate_object_id(object_id):
        raise ValueError(f"Invalid object ID derived from path: {object_id}")
    return object_id


def iter_object_sources(roots: Iterable[Path] | None = None) -> list[ObjectSource]:
    """List object source files from configured roots in deterministic order."""
    search_roots = list(roots) if roots is not None else get_object_roots()
    sources: list[ObjectSource] = []

    for root in search_roots:
        if not root.exists() or not root.is_dir():
            continue

        for path in sorted(root.rglob("*.py"), key=lambda p: p.relative_to(root).as_posix()):
            try:
                rel = _relative_to_root(path, root)
                object_id = object_id_from_path(path, root)
            except ValueError:
                continue

            kind: Literal["system", "user"] = "user" if rel.parts[0] == "users" else "system"
            sources.append(
                ObjectSource(
                    object_id=object_id,
                    path=path,
                    relative_path=rel,
                    kind=kind,
                )
            )

    return sources


def _candidate_system_paths(object_id: str, roots: Iterable[Path]) -> list[Path]:
    candidates: list[Path] = []
    for root in roots:
        if "_" in object_id:
            category, name = object_id.split("_", 1)
            candidates.append(root / category / f"{name}.py")
        candidates.append(root / f"{object_id}.py")
    return candidates


def _candidate_root(candidate: Path, roots: Iterable[Path]) -> Path | None:
    for root in roots:
        try:
            _relative_to_root(candidate, root)
            return root
        except ValueError:
            continue
    return None


def _relative_to_root(path: Path, root: Path) -> Path:
    resolved_path = path.resolve(strict=False)
    resolved_root = root.resolve(strict=False)

    try:
        return resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"Path escapes object root: {path}") from exc


def _is_allowed_source_file(path: Path, root: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        rel = _relative_to_root(path, root)
    except ValueError:
        return False
    return _is_public_source_relative_path(rel)


def _is_public_source_relative_path(rel: Path) -> bool:
    parts = rel.parts
    if not parts:
        return False
    if rel.suffix != ".py":
        return False
    if rel.name == "__init__.py":
        return False
    if "__pycache__" in parts:
        return False
    if any(part.startswith(".") or part.startswith("_") for part in parts):
        return False
    if any(part in {"", ".", ".."} for part in parts):
        return False

    if parts[0] == "users":
        if len(parts) != 3:
            return False
        return parts[1].isdigit() and _is_safe_source_stem(Path(parts[2]).stem)

    return all(_is_safe_source_stem(part if i < len(parts) - 1 else Path(part).stem)
               for i, part in enumerate(parts))


def _is_safe_source_stem(stem: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_]{0,49}", stem))
