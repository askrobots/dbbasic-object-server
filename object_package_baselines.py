"""Package provenance baselines for DBBASIC.

Records what a package shipped — a content hash per object and per schema —
at install time, so "is this customized?" becomes a computable question:
hash the live source/schema and compare to the stamp. Without this stamp a
human's edit cannot be distinguished from the shipped file, and every
upgrade is a coin flip. See docs/upgrade-and-customization.md (Rule 0:
Provenance Baselines), which this module implements.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from object_versions import DEFAULT_DATA_DIR

BASELINES_DIR = "package_baselines"


def sha256_text(text: str) -> str:
    """Return the hex sha256 digest of text, encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_schema_hash(schema: Mapping[str, Any]) -> str:
    """Return a stable hash for a schema dict regardless of key order."""
    return sha256_text(json.dumps(schema, sort_keys=True, separators=(",", ":")))


def baseline_path(package_id: str, *, base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    """Return the path to the baseline file for a package."""
    return Path(base_dir) / BASELINES_DIR / f"{package_id}.json"


def load_baseline(package_id: str, *, base_dir: Path | str = DEFAULT_DATA_DIR) -> dict[str, Any] | None:
    """Return the recorded baseline for a package, or None if absent/unreadable."""
    path = baseline_path(package_id, base_dir=base_dir)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def record_baseline(
    package_id: str,
    *,
    version: str,
    objects: Mapping[str, str],
    schemas: Mapping[str, str],
    installed_at: str | None = None,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    """Stamp a package's baseline (shipped object/schema hashes) and return it."""
    baseline = {
        "package": package_id,
        "version": version,
        "installed_at": installed_at,
        "objects": dict(objects),
        "schemas": dict(schemas),
    }
    path = baseline_path(package_id, base_dir=base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(baseline, handle, indent=2, sort_keys=True)
        os.replace(tmp_name, path)
    finally:
        try:
            Path(tmp_name).unlink()
        except FileNotFoundError:
            pass

    return baseline
