"""Object source version storage.

This keeps the versioning contract from the working prototype:

- data/versions/{object_id}/metadata.tsv
- data/versions/{object_id}/v1.txt
- data/versions/{object_id}/v2.txt

Rollback is non-destructive. It creates a new version containing the older
content, preserving the full history.
"""
from __future__ import annotations

import csv
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

from object_namespace import validate_object_id


DEFAULT_DATA_DIR = "data"
METADATA_FILE = "metadata.tsv"
METADATA_FIELDS = ["version_id", "timestamp", "author", "message", "hash"]


class VersionError(Exception):
    """Base exception for version-related errors."""


class InvalidObjectIdError(VersionError):
    """Raised when an object ID is not safe for version storage."""


class VersionNotFoundError(VersionError):
    """Raised when a requested version does not exist."""


class VersionManager:
    """Manage per-object source versions stored as TSV metadata and text files."""

    def __init__(self, base_dir: Path | str = DEFAULT_DATA_DIR):
        self.base_dir = Path(base_dir)
        self.versions_dir = self.base_dir / "versions"
        self.versions_dir.mkdir(parents=True, exist_ok=True)

    def save_version(
        self,
        object_id: str,
        content: str,
        author: str,
        message: str,
    ) -> int:
        """Save a new version and return its integer version ID."""
        obj_dir = self._object_dir(object_id, create=True)
        version_id = self._get_next_version_id(object_id)
        content_hash = self._compute_hash(content)
        timestamp = datetime.now().isoformat()

        content_file = obj_dir / f"v{version_id}.txt"
        content_file.write_text(content)

        metadata_file = obj_dir / METADATA_FILE
        is_new_file = not metadata_file.exists()
        with metadata_file.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=METADATA_FIELDS, delimiter="\t")
            if is_new_file:
                writer.writeheader()
            writer.writerow(
                {
                    "version_id": version_id,
                    "timestamp": timestamp,
                    "author": author,
                    "message": message,
                    "hash": content_hash,
                }
            )

        return version_id

    def get_version(self, object_id: str, version_id: int | None = None) -> dict[str, Any] | None:
        """Get one version with content. `version_id=None` returns the latest."""
        versions = self._read_metadata(object_id)
        if not versions:
            return None

        if version_id is None:
            target_version = versions[-1]
        else:
            target_version = next((v for v in versions if v["version_id"] == version_id), None)
            if target_version is None:
                return None

        content_file = self._object_dir(object_id) / f"v{target_version['version_id']}.txt"
        if not content_file.exists() or not content_file.is_file():
            return None

        return {
            **target_version,
            "content": content_file.read_text(),
        }

    def get_history(
        self,
        object_id: str,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return metadata history, newest first, without version content."""
        versions = list(reversed(self._read_metadata(object_id)))

        if offset > 0:
            versions = versions[offset:]

        if limit is not None:
            versions = versions[:limit]

        return versions

    def rollback(
        self,
        object_id: str,
        to_version: int,
        author: str,
        message: str,
    ) -> int:
        """Create a new version containing content from an older version."""
        old_version = self.get_version(object_id, to_version)
        if old_version is None:
            raise VersionNotFoundError(f"Version {to_version} not found for object {object_id}")

        return self.save_version(
            object_id=object_id,
            content=old_version["content"],
            author=author,
            message=message,
        )

    def _object_dir(self, object_id: str, *, create: bool = False) -> Path:
        if not validate_object_id(object_id):
            raise InvalidObjectIdError(f"Invalid object ID: {object_id}")

        obj_dir = self.versions_dir / object_id
        versions_root = self.versions_dir.resolve(strict=False)
        resolved_obj_dir = obj_dir.resolve(strict=False)

        try:
            resolved_obj_dir.relative_to(versions_root)
        except ValueError as exc:
            raise InvalidObjectIdError(f"Object ID escapes versions directory: {object_id}") from exc

        if create:
            obj_dir.mkdir(parents=True, exist_ok=True)
        return obj_dir

    def _read_metadata(self, object_id: str) -> list[dict[str, Any]]:
        metadata_file = self._object_dir(object_id) / METADATA_FILE
        if not metadata_file.exists() or not metadata_file.is_file():
            return []

        versions: list[dict[str, Any]] = []
        with metadata_file.open("r", newline="") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                normalized = self._normalize_metadata_row(row)
                if normalized is not None:
                    versions.append(normalized)

        return versions

    def _normalize_metadata_row(self, row: dict[str, str | None]) -> dict[str, Any] | None:
        if any(row.get(field) is None for field in METADATA_FIELDS):
            return None

        try:
            version_id = int(row["version_id"] or "")
        except ValueError:
            return None

        return {
            "version_id": version_id,
            "timestamp": row["timestamp"] or "",
            "author": row["author"] or "",
            "message": row["message"] or "",
            "hash": row["hash"] or "",
        }

    def _get_next_version_id(self, object_id: str) -> int:
        versions = self._read_metadata(object_id)
        if not versions:
            return 1
        return max(version["version_id"] for version in versions) + 1

    def _compute_hash(self, content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()
