"""Schema metadata version storage.

Schema changes need the same operational trail as object source changes:

- data/schema_versions/{collection}/metadata.tsv
- data/schema_versions/{collection}/v1.json
- data/schema_versions/{collection}/v2.json

Rollback is non-destructive. It creates a new version containing the older
schema JSON, preserving who changed what and when.
"""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import object_schemas
from object_versions import DEFAULT_DATA_DIR

SCHEMA_VERSIONS_DIR = "schema_versions"
METADATA_FILE = "metadata.tsv"
METADATA_FIELDS = ["version_id", "timestamp", "author", "message", "hash"]


class SchemaVersionError(Exception):
    """Base exception for schema version errors."""


class InvalidSchemaNameError(SchemaVersionError):
    """Raised when a schema name is not safe for version storage."""


class SchemaVersionNotFoundError(SchemaVersionError):
    """Raised when a requested schema version does not exist."""


class SchemaVersionManager:
    """Manage per-schema JSON versions stored as TSV metadata and files."""

    def __init__(self, base_dir: Path | str = DEFAULT_DATA_DIR):
        self.base_dir = Path(base_dir)
        self.versions_dir = self.base_dir / SCHEMA_VERSIONS_DIR
        self.versions_dir.mkdir(parents=True, exist_ok=True)

    def save_version(
        self,
        schema: str,
        content: str,
        author: str,
        message: str,
    ) -> int:
        """Save a new schema version and return its integer version ID."""
        schema_dir = self._schema_dir(schema, create=True)
        version_id = self._get_next_version_id(schema)
        content_hash = self._compute_hash(content)
        timestamp = datetime.now().isoformat()

        content_file = schema_dir / f"v{version_id}.json"
        content_file.write_text(content)

        metadata_file = schema_dir / METADATA_FILE
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

    def get_version(self, schema: str, version_id: int | None = None) -> dict[str, Any] | None:
        """Get one schema version with content. `version_id=None` returns latest."""
        versions = self._read_metadata(schema)
        if not versions:
            return None

        if version_id is None:
            target_version = versions[-1]
        else:
            target_version = next((v for v in versions if v["version_id"] == version_id), None)
            if target_version is None:
                return None

        content_file = self._schema_dir(schema) / f"v{target_version['version_id']}.json"
        if not content_file.exists() or not content_file.is_file():
            return None

        return {
            **target_version,
            "content": content_file.read_text(),
        }

    def get_history(
        self,
        schema: str,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return schema metadata history, newest first, without content."""
        versions = list(reversed(self._read_metadata(schema)))

        if offset > 0:
            versions = versions[offset:]

        if limit is not None:
            versions = versions[:limit]

        return versions

    def rollback(
        self,
        schema: str,
        to_version: int,
        author: str,
        message: str,
    ) -> int:
        """Create a new version containing schema JSON from an older version."""
        old_version = self.get_version(schema, to_version)
        if old_version is None:
            raise SchemaVersionNotFoundError(f"Version {to_version} not found for schema {schema}")

        return self.save_version(
            schema=schema,
            content=old_version["content"],
            author=author,
            message=message,
        )

    def _schema_dir(self, schema: str, *, create: bool = False) -> Path:
        if not object_schemas.validate_schema_name(schema):
            raise InvalidSchemaNameError(f"Invalid schema name: {schema}")

        schema_dir = self.versions_dir / schema
        versions_root = self.versions_dir.resolve(strict=False)
        resolved_schema_dir = schema_dir.resolve(strict=False)

        try:
            resolved_schema_dir.relative_to(versions_root)
        except ValueError as exc:
            raise InvalidSchemaNameError(f"Schema name escapes versions directory: {schema}") from exc

        if create:
            schema_dir.mkdir(parents=True, exist_ok=True)
        return schema_dir

    def _read_metadata(self, schema: str) -> list[dict[str, Any]]:
        metadata_file = self._schema_dir(schema) / METADATA_FILE
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

    def _get_next_version_id(self, schema: str) -> int:
        versions = self._read_metadata(schema)
        if not versions:
            return 1
        return max(version["version_id"] for version in versions) + 1

    def _compute_hash(self, content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()


def schema_version_content(schema: dict[str, Any]) -> str:
    """Return canonical JSON text for schema version files."""
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"
