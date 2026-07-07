"""Per-user file storage: bytes on disk, visibility on the metadata record.

Files uploaded by users live under ``data/user_files/{owner_id}/{file_id}``.
The bytes carry no access rules of their own — every read and delete is
authorized against the file's metadata record in the ``files`` collection,
so owner rows, public flags, and project sharing govern downloads exactly
like any other record. Quotas count actual bytes on disk, not metadata.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from object_versions import DEFAULT_DATA_DIR

USER_FILES_DIR = "user_files"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class InvalidUserFileError(ValueError):
    """Raised when a file id, owner, or payload is not usable."""


class UserFileNotFoundError(FileNotFoundError):
    """Raised when stored bytes are missing."""


def user_files_root(base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    return Path(base_dir) / USER_FILES_DIR


def file_path(
    owner_id: str,
    file_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> Path:
    """Return the storage path for one file, refusing unsafe ids."""
    owner = _validated(owner_id, "owner_id")
    name = _validated(file_id, "file_id")
    root = user_files_root(base_dir)
    path = root / owner / name
    resolved_root = root.resolve(strict=False)
    try:
        path.resolve(strict=False).relative_to(resolved_root)
    except ValueError as exc:
        raise InvalidUserFileError("File path escapes the user files directory") from exc
    return path


def save_file(
    owner_id: str,
    file_id: str,
    content: bytes,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> int:
    """Write one file's bytes atomically; return the size stored."""
    if not isinstance(content, (bytes, bytearray)):
        raise InvalidUserFileError("File content must be bytes")
    path = file_path(owner_id, file_id, base_dir=base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_bytes(content)
    os.chmod(temp_path, 0o600)
    temp_path.replace(path)
    return len(content)


def read_file(
    owner_id: str,
    file_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> bytes:
    """Return one file's bytes."""
    path = file_path(owner_id, file_id, base_dir=base_dir)
    if not path.is_file():
        raise UserFileNotFoundError(f"File not found: {file_id}")
    return path.read_bytes()


def delete_file(
    owner_id: str,
    file_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> bool:
    """Delete one file's bytes; return whether they existed."""
    path = file_path(owner_id, file_id, base_dir=base_dir)
    if not path.is_file():
        return False
    path.unlink()
    return True


def delete_all_files(
    owner_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> int:
    """Delete one user's whole file directory (user cleanup); return count."""
    owner = _validated(owner_id, "owner_id")
    directory = user_files_root(base_dir) / owner
    if not directory.is_dir():
        return 0
    count = sum(1 for item in directory.iterdir() if item.is_file())
    shutil.rmtree(directory)
    return count


def usage_bytes(
    owner_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> int:
    """Return one user's total stored bytes — the quota input."""
    owner = _validated(owner_id, "owner_id")
    directory = user_files_root(base_dir) / owner
    if not directory.is_dir():
        return 0
    return sum(item.stat().st_size for item in directory.iterdir() if item.is_file())


def _validated(value: str, name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise InvalidUserFileError(f"Invalid {name}")
    return value
