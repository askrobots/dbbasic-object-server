"""File-backed permission policy storage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import object_permissions
from object_versions import DEFAULT_DATA_DIR

PERMISSIONS_DIR = "permissions"
POLICY_FILE = "policy.json"
DEFAULT_POLICY = object_permissions.PermissionPolicy(access_mode="role_based")


def policy_path(base_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    """Return the permission policy file path."""
    root = Path(base_dir) / PERMISSIONS_DIR
    path = root / POLICY_FILE
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)

    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("Permission policy path escapes permissions directory") from exc

    return path


def load_policy(base_dir: Path | str = DEFAULT_DATA_DIR) -> object_permissions.PermissionPolicy:
    """Load the persisted policy, or return the conservative default."""
    path = policy_path(base_dir)
    if not path.exists():
        return DEFAULT_POLICY

    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError("Permission policy file contains invalid JSON") from exc

    return object_permissions.policy_from_dict(payload)


def save_policy(
    policy: object_permissions.PermissionPolicy,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> Path:
    """Atomically save the permission policy."""
    path = policy_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    payload = object_permissions.policy_to_dict(policy)

    temp_path.write_text(_json_dump(payload))
    temp_path.replace(path)
    return path


def replace_policy(
    payload: dict[str, Any],
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> object_permissions.PermissionPolicy:
    """Validate and persist a JSON-compatible policy payload."""
    policy = object_permissions.policy_from_dict(payload)
    save_policy(policy, base_dir=base_dir)
    return policy


def starter_policy_payload() -> dict[str, Any]:
    """Return the documented starter policy for a fresh deployment.

    Role-based, with the smallest grants that keep a public staging site
    working once enforcement is on: anonymous visitors can run the public
    pages and read the probe demo records; signed-in users can run objects
    and write probe records. Admin-role subjects bypass rules entirely.
    Deployments should edit this per app rather than widen it in place.
    """
    return {
        "access_mode": "role_based",
        "rules": [
            {
                "effect": "allow",
                "principal": "public",
                "actions": ["execute"],
                "object_id": "site_home",
                "reason": "public home page",
            },
            {
                "effect": "allow",
                "principal": "public",
                "actions": ["execute"],
                "object_id": "system_dashboard",
                "reason": "public staging dashboard",
            },
            {
                "effect": "allow",
                "principal": "public",
                "actions": ["execute"],
                "object_id": "system_write_probe",
                "reason": "public write probe page",
            },
            {
                "effect": "allow",
                "principal": "public",
                "actions": ["read"],
                "collection": "dbbasic_probe",
                "reason": "probe records are public demo data",
            },
            {
                "effect": "allow",
                "principal": "registered",
                "actions": ["read", "execute"],
                "reason": "signed-in users can read and run objects",
            },
            {
                "effect": "allow",
                "principal": "registered",
                "actions": ["create", "update", "delete"],
                "collection": "dbbasic_probe",
                "reason": "signed-in users can write probe records",
            },
        ],
    }


def _json_dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
