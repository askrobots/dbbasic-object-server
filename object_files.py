"""Read-only object-owned file helpers.

The working prototype stores object-owned files under:

    data/files/{object_id}/

This public slice intentionally starts with safe list/read helpers. Upload and
delete need stricter size, content, and permission policy before becoming part
of the HTTP surface.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from object_namespace import validate_object_id
from object_versions import DEFAULT_DATA_DIR, InvalidObjectIdError


class InvalidObjectFilenameError(ValueError):
    """Raised when an object file name is unsafe."""


class ObjectFileNotFoundError(FileNotFoundError):
    """Raised when an object-owned file is missing."""


@dataclass(frozen=True)
class ObjectFileMetadata:
    name: str
    size: int
    modified: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def object_files_dir(object_id: str, base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    """Return the object-owned files directory for a validated object id."""
    if not validate_object_id(object_id):
        raise InvalidObjectIdError(f"Invalid object ID: {object_id}")

    return Path(base_dir) / "files" / object_id


def list_object_files(
    object_id: str,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> list[dict[str, Any]]:
    """List object-owned files as metadata dictionaries."""
    files_dir = object_files_dir(object_id, base_dir)
    if not files_dir.exists():
        return []
    if not files_dir.is_dir():
        raise OSError(f"Object files path is not a directory: {files_dir}")

    files = []
    for path in sorted(files_dir.rglob("*")):
        if not path.is_file():
            continue
        relative_name = path.relative_to(files_dir).as_posix()
        try:
            safe_path = _safe_file_path(files_dir, relative_name)
        except InvalidObjectFilenameError:
            continue
        if path.resolve() != safe_path:
            continue

        metadata = _metadata(files_dir, path)
        files.append(metadata.to_dict())

    return files


def list_all_object_files(
    base_dir: Path | str = DEFAULT_DATA_DIR,
    *,
    object_id: str | None = None,
) -> list[dict[str, Any]]:
    """List object-owned files across the local file store."""
    if object_id is not None:
        return [
            {"object_id": object_id, **metadata}
            for metadata in list_object_files(object_id, base_dir=base_dir)
        ]

    files_root = Path(base_dir) / "files"
    if not files_root.exists():
        return []
    if not files_root.is_dir():
        raise OSError(f"Object files root is not a directory: {files_root}")

    files = []
    for object_dir in sorted(files_root.iterdir()):
        if not object_dir.is_dir() or not validate_object_id(object_dir.name):
            continue
        files.extend(
            {"object_id": object_dir.name, **metadata}
            for metadata in list_object_files(object_dir.name, base_dir=base_dir)
        )

    files.sort(key=lambda item: (-float(item["modified"]), item["object_id"], item["name"]))
    return files


def read_object_file(
    object_id: str,
    filename: str,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> tuple[bytes, dict[str, Any]]:
    """Read one object-owned file and return bytes plus metadata."""
    files_dir = object_files_dir(object_id, base_dir)
    path = _safe_file_path(files_dir, filename)
    if not path.exists() or not path.is_file():
        raise ObjectFileNotFoundError(f"File not found: {filename}")

    return path.read_bytes(), _metadata(files_dir, path).to_dict()


def _safe_file_path(files_dir: Path, filename: str) -> Path:
    if not filename or "\x00" in filename or filename.startswith("/") or ".." in filename:
        raise InvalidObjectFilenameError(f"Invalid filename: {filename!r}")

    root = files_dir.resolve()
    resolved = (files_dir / filename).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise InvalidObjectFilenameError(f"Path traversal blocked: {filename!r}") from exc

    return resolved


def _metadata(files_dir: Path, path: Path) -> ObjectFileMetadata:
    stat = path.stat()
    name = path.relative_to(files_dir).as_posix()
    return ObjectFileMetadata(name=name, size=stat.st_size, modified=stat.st_mtime)
