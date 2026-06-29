"""Schema-level field permissions for collection records.

Collection policy decides whether a subject can reach a row. Field permissions
refine that decision so generated forms and APIs can share the same
``edit/read/hidden`` contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

import object_permissions
import object_schemas
from object_versions import DEFAULT_DATA_DIR

EDIT = "edit"
READ = "read"
HIDDEN = "hidden"

_ACCESS_RANK = {
    HIDDEN: 0,
    READ: 1,
    EDIT: 2,
}
_EDIT_ALIASES = {"edit", "editable", "write", "writable", "update"}
_READ_ALIASES = {"read", "readonly", "read_only", "view", "visible"}
_HIDDEN_ALIASES = {"hidden", "hide", "deny", "denied", "none", "forbidden"}
_PRINCIPAL_PREFIXES = ("role:", "user:", "account:", "subscription:")
_SPECIAL_PRINCIPALS = {"public", "registered", "owner"}


def redact_record(
    collection: str,
    record: Mapping[str, Any],
    *,
    subject: object_permissions.PermissionSubject,
    policy: object_permissions.PermissionPolicy,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Return ``record`` with schema-hidden fields removed."""
    fields = _schema_field_map(collection, base_dir=base_dir, roots=roots)
    if not fields:
        return dict(record)

    visible = dict(record)
    for name, field in fields.items():
        if field_access(field, subject=subject, policy=policy, record=record) == HIDDEN:
            visible.pop(name, None)
    return visible


def denied_write_fields(
    collection: str,
    submitted_fields: Iterable[str],
    *,
    subject: object_permissions.PermissionSubject,
    policy: object_permissions.PermissionPolicy,
    record: Mapping[str, Any] | None = None,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
    allow_id: bool = True,
) -> list[str]:
    """Return submitted schema fields that are not editable by ``subject``."""
    fields = _schema_field_map(collection, base_dir=base_dir, roots=roots)
    if not fields:
        return []

    denied: list[str] = []
    for name in submitted_fields:
        if allow_id and name == "id":
            continue
        field = fields.get(name)
        if field is None:
            continue
        access = field_access(field, subject=subject, policy=policy, record=record)
        if access is not None and access != EDIT:
            denied.append(name)
    return sorted(denied)


def field_access(
    field: Mapping[str, Any],
    *,
    subject: object_permissions.PermissionSubject,
    policy: object_permissions.PermissionPolicy,
    record: Mapping[str, Any] | None = None,
) -> str | None:
    """Resolve one schema field's access for a subject.

    Missing permissions mean the field is not constrained by schema metadata.
    When multiple principals match, the most restrictive access wins.
    """
    permissions = field.get("permissions")
    entries, default = _permission_entries(permissions, field_name=str(field.get("name", "")))
    if not entries and default is None:
        return None

    matches = [
        access
        for principal, access in entries
        if object_permissions.principal_matches(principal, subject, policy, record=record)
    ]
    if matches:
        return min(matches, key=lambda access: _ACCESS_RANK[access])
    return default


def _schema_field_map(
    collection: str,
    *,
    base_dir: Path | str,
    roots: Iterable[Path] | None,
) -> dict[str, Mapping[str, Any]]:
    try:
        schema = object_schemas.get_schema(collection, base_dir=base_dir, roots=roots)
    except object_schemas.SchemaNotFoundError:
        return {}

    fields = schema.get("fields", [])
    if not isinstance(fields, list):
        return {}
    return {
        field["name"]: field
        for field in fields
        if isinstance(field, Mapping) and isinstance(field.get("name"), str)
    }


def _permission_entries(
    permissions: Any,
    *,
    field_name: str,
) -> tuple[list[tuple[str, str]], str | None]:
    if permissions is None:
        return [], None
    if isinstance(permissions, str):
        return [], _normalize_access(permissions, field_name=field_name)
    if not isinstance(permissions, Mapping):
        raise ValueError(f"Schema field '{field_name}' permissions must be an object")

    entries: list[tuple[str, str]] = []
    default: str | None = None

    roles = permissions.get("roles")
    if isinstance(roles, Mapping):
        for role, access in roles.items():
            entries.append((_principal_from_key(str(role)), _access_from_value(access, field_name=field_name)))

    for key, value in permissions.items():
        if key == "roles":
            continue
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"Schema field '{field_name}' has an invalid permission principal")
        normalized_key = key.strip()
        if normalized_key == "default":
            default = _access_from_value(value, field_name=field_name)
            continue
        if _access_key(normalized_key) is not None and isinstance(value, (list, tuple, set)):
            access = _access_key(normalized_key)
            assert access is not None
            for principal in value:
                if not isinstance(principal, str) or not principal.strip():
                    raise ValueError(
                        f"Schema field '{field_name}' has an invalid permission principal"
                    )
                entries.append((_principal_from_key(principal), access))
            continue
        entries.append((_principal_from_key(normalized_key), _access_from_value(value, field_name=field_name)))

    return entries, default


def _access_from_value(value: Any, *, field_name: str) -> str:
    if isinstance(value, str):
        return _normalize_access(value, field_name=field_name)
    if isinstance(value, Mapping):
        access = value.get("access", value.get("mode"))
        if isinstance(access, str):
            return _normalize_access(access, field_name=field_name)
    raise ValueError(f"Schema field '{field_name}' permission access must be edit, read, or hidden")


def _normalize_access(value: str, *, field_name: str) -> str:
    text = value.strip().lower().replace("-", "_")
    if text in _EDIT_ALIASES:
        return EDIT
    if text in _READ_ALIASES:
        return READ
    if text in _HIDDEN_ALIASES:
        return HIDDEN
    raise ValueError(f"Schema field '{field_name}' permission access must be edit, read, or hidden")


def _access_key(value: str) -> str | None:
    try:
        return _normalize_access(value, field_name="")
    except ValueError:
        return None


def _principal_from_key(value: str) -> str:
    principal = value.strip()
    lowered = principal.lower()
    if lowered in {"anonymous", "anon"}:
        return "public"
    if lowered in {"authenticated", "signed_in", "signed-in"}:
        return "registered"
    if lowered in _SPECIAL_PRINCIPALS or any(lowered.startswith(prefix) for prefix in _PRINCIPAL_PREFIXES):
        return principal
    return f"role:{principal}"
