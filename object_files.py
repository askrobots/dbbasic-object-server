"""Object-owned file helpers.

The working prototype stores object-owned files under:

    data/files/{object_id}/

The HTTP surface gates writes separately from reads. These helpers keep path
handling local and conservative so higher layers can apply auth, quotas, and
permission policy.
"""

from __future__ import annotations

import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from object_namespace import validate_object_id
from object_versions import DEFAULT_DATA_DIR, InvalidObjectIdError


class InvalidObjectFilenameError(ValueError):
    """Raised when an object file name is unsafe."""


class ObjectFileNotFoundError(FileNotFoundError):
    """Raised when an object-owned file is missing."""


class ObjectFileExistsError(FileExistsError):
    """Raised when an object-owned file already exists."""


class ObjectFileTooLargeError(ValueError):
    """Raised when an object-owned file exceeds the configured size limit."""


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


def write_object_file(
    object_id: str,
    filename: str,
    content: bytes,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    *,
    overwrite: bool = False,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """Write one object-owned file and return metadata."""
    if max_bytes is not None and len(content) > max_bytes:
        raise ObjectFileTooLargeError(
            f"Object file exceeds max size: {len(content)} bytes > {max_bytes} bytes"
        )

    files_dir = object_files_dir(object_id, base_dir)
    path = _safe_write_file_path(files_dir, filename)
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise InvalidObjectFilenameError(f"Invalid filename: {filename!r}")
        if not overwrite:
            raise ObjectFileExistsError(f"File already exists: {filename}")

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as temp_file:
            temp_file.write(content)
            temp_path = Path(temp_file.name)
        temp_path.replace(path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()

    return _metadata(files_dir, path).to_dict()


def delete_object_file(
    object_id: str,
    filename: str,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    """Delete one object-owned file and return its previous metadata."""
    files_dir = object_files_dir(object_id, base_dir)
    path = _safe_write_file_path(files_dir, filename)
    if not path.exists() or not path.is_file() or path.is_symlink():
        raise ObjectFileNotFoundError(f"File not found: {filename}")

    metadata = _metadata(files_dir, path).to_dict()
    path.unlink()
    _prune_empty_dirs(path.parent, files_dir)
    return metadata


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


def _safe_write_file_path(files_dir: Path, filename: str) -> Path:
    if not filename or "\x00" in filename or filename.startswith("/") or ".." in filename:
        raise InvalidObjectFilenameError(f"Invalid filename: {filename!r}")

    relative = Path(filename)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise InvalidObjectFilenameError(f"Invalid filename: {filename!r}")

    root = files_dir.resolve()
    candidate = files_dir / relative
    current = files_dir
    for part in relative.parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise InvalidObjectFilenameError(f"Invalid filename: {filename!r}")

    if candidate.exists() and candidate.is_symlink():
        raise InvalidObjectFilenameError(f"Invalid filename: {filename!r}")

    try:
        candidate.parent.resolve().relative_to(root)
    except ValueError as exc:
        raise InvalidObjectFilenameError(f"Path traversal blocked: {filename!r}") from exc

    return candidate


def _prune_empty_dirs(start: Path, stop: Path) -> None:
    stop = stop.resolve()
    current = start
    while current != stop:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _metadata(files_dir: Path, path: Path) -> ObjectFileMetadata:
    stat = path.stat()
    name = path.relative_to(files_dir).as_posix()
    return ObjectFileMetadata(name=name, size=stat.st_size, modified=stat.st_mtime)
