"""Permission rollout status summaries for DBBASIC operators and Scroll."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import object_identity
import object_permission_store


def build_permissions_status(
    *,
    base_dir: Path | str,
    permissions: Mapping[str, Any],
    require_known_identity_users_env: str,
) -> dict[str, Any]:
    """Return a read-only summary of permission rollout readiness."""
    identity = identity_status(base_dir=base_dir)
    policy = policy_status(base_dir=base_dir)
    readiness = readiness_status(policy, identity=identity, permissions=permissions)
    warnings = status_warnings(
        permissions=permissions,
        identity=identity,
        policy=policy,
        require_known_identity_users_env=require_known_identity_users_env,
    )

    return {
        "status": "ok" if policy["valid"] and identity["valid"] else "degraded",
        "permissions": dict(permissions),
        "identity": identity,
        "policy": policy,
        "coverage": coverage_status(),
        "readiness": readiness,
        "warnings": warnings,
    }


def identity_status(*, base_dir: Path | str) -> dict[str, Any]:
    """Return counts for the local identity store."""
    try:
        accounts = object_identity.list_accounts(base_dir=base_dir)
        users = object_identity.list_users(base_dir=base_dir)
        sessions = object_identity.list_sessions(base_dir=base_dir)
    except (OSError, ValueError) as exc:
        return {
            "valid": False,
            "error": str(exc),
            "accounts": {"count": 0, "active": 0, "disabled": 0},
            "users": {"count": 0, "active": 0, "disabled": 0},
            "sessions": {"count": 0, "active": 0, "revoked": 0},
        }

    return {
        "valid": True,
        "accounts": {
            "count": len(accounts),
            "active": _count_status(accounts, "active"),
            "disabled": _count_status(accounts, "disabled"),
        },
        "users": {
            "count": len(users),
            "active": _count_status(users, "active"),
            "disabled": _count_status(users, "disabled"),
        },
        "sessions": {
            "count": len(sessions),
            "active": sum(1 for session in sessions if session.get("active") is True),
            "revoked": sum(1 for session in sessions if session.get("revoked_at")),
        },
    }


def policy_status(*, base_dir: Path | str) -> dict[str, Any]:
    """Return a compact summary of the active permission policy."""
    try:
        policy_file_exists = object_permission_store.policy_path(base_dir).exists()
    except ValueError as exc:
        return _empty_policy_status(error=str(exc), policy_file_exists=False)

    try:
        policy = object_permission_store.load_policy(base_dir)
    except ValueError as exc:
        return _empty_policy_status(
            error=f"Permission policy is invalid: {exc}",
            policy_file_exists=policy_file_exists,
        )

    rules = policy.rules
    return {
        "valid": True,
        "policy_file_exists": policy_file_exists,
        "access_mode": policy.access_mode,
        "rules_count": len(rules),
        "allow_rules": sum(1 for rule in rules if rule.effect == "allow"),
        "deny_rules": sum(1 for rule in rules if rule.effect == "deny"),
        "roles_count": len(policy.roles),
        "user_roles_count": len(policy.user_roles),
        "admin_roles": list(policy.admin_roles),
        "principals": sorted({rule.principal for rule in rules}),
        "actions": sorted({action for rule in rules for action in rule.actions}),
        "collections": sorted({rule.collection for rule in rules if rule.collection is not None}),
        "objects": sorted({rule.object_id for rule in rules if rule.object_id is not None}),
        "temporary_rules": sum(
            1 for rule in rules if rule.valid_from is not None or rule.expires_at is not None
        ),
    }


def enforcement_readiness(
    *,
    base_dir: Path | str,
    permissions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the same readiness gate used by the HTTP server."""
    identity = identity_status(base_dir=base_dir)
    policy = policy_status(base_dir=base_dir)
    return readiness_status(policy, identity=identity, permissions=permissions)


def readiness_status(
    policy: Mapping[str, Any],
    *,
    identity: Mapping[str, Any] | None = None,
    permissions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return blockers that make permission enforcement unsafe to enable."""
    blockers: list[str] = []
    runtime = dict(permissions or {})

    if runtime and not runtime.get("admin_token_configured", False):
        blockers.append(
            "Admin recovery token must be configured before enforcement rollout."
        )

    if identity is not None and not identity["valid"]:
        blockers.append("Identity store must be readable before enforcement rollout.")

    if not policy["valid"]:
        blockers.append("Permission policy must be valid before enforcement rollout.")
    else:
        access_mode = policy["access_mode"]
        if access_mode == "password":
            blockers.append(
                "Password access mode needs a password verifier before enforcement rollout."
            )
        if access_mode == "role_based" and policy["allow_rules"] == 0:
            blockers.append(
                "Role-based policy has no allow grants; non-admin traffic will be denied."
            )
        if (
            identity is not None
            and access_mode in {"registered", "subscription", "role_based", "private"}
            and not _has_non_admin_identity_path(identity=identity, permissions=runtime)
        ):
            blockers.append(
                "No non-admin identity path is available; enable trusted headers, "
                "guarded session login, password login, or create an active session "
                "before enforcement."
            )

    return {
        "can_enable_enforcement": not blockers,
        "blockers": blockers,
    }


def status_warnings(
    *,
    permissions: Mapping[str, Any],
    identity: Mapping[str, Any],
    policy: Mapping[str, Any],
    require_known_identity_users_env: str,
) -> list[str]:
    """Return non-blocking warnings for the active permission setup."""
    warnings: list[str] = []
    enforcement_requested = bool(
        permissions.get("enforcement_requested", permissions.get("enforcement_enabled", False))
    )
    enforcement_enabled = bool(permissions.get("enforcement_enabled", False))
    if permissions.get("enforcement_blocked"):
        warnings.append(
            "Permission enforcement was requested but readiness checks blocked rollout."
        )
    elif not enforcement_requested and not enforcement_enabled:
        warnings.append("Permission enforcement is off.")
    if permissions.get("allow_unready_enforcement"):
        warnings.append(
            "Unready permission enforcement override is enabled; use only for manual recovery or tests."
        )
    if not permissions["audit_enabled"]:
        warnings.append("Permission audit is off; enable audit mode before enforcement rollout.")
    if permissions["trusted_headers_enabled"]:
        warnings.append(
            "Trusted identity headers are enabled; the reverse proxy must strip client-supplied identity headers."
        )
    elif (
        identity["valid"]
        and identity["sessions"]["active"] == 0
        and not _session_login_available(identity=identity, permissions=permissions)
        and not _password_login_available(identity=identity, permissions=permissions)
    ):
        warnings.append(
            "Trusted headers are off and no active DBBASIC sessions exist; non-admin requests will be anonymous."
        )
    if not permissions["require_known_identity_users"]:
        warnings.append(
            f"{require_known_identity_users_env} is off; admin code can mint sessions for unregistered users."
        )
    if identity["valid"] and identity["users"]["count"] == 0:
        warnings.append("No identity users are registered yet.")
    if policy["valid"] and policy["access_mode"] == "public":
        warnings.append("Public access mode allows read/execute without identity.")
    return warnings


def _has_non_admin_identity_path(
    *,
    identity: Mapping[str, Any],
    permissions: Mapping[str, Any],
) -> bool:
    if permissions.get("trusted_headers_enabled"):
        return True
    if _session_login_available(identity=identity, permissions=permissions):
        return True
    if _password_login_available(identity=identity, permissions=permissions):
        return True
    return bool(identity["sessions"]["active"])


def _session_login_available(
    *,
    identity: Mapping[str, Any],
    permissions: Mapping[str, Any],
) -> bool:
    return bool(
        permissions.get("session_login_enabled")
        and permissions.get("session_login_token_configured")
        and identity["users"]["active"] > 0
    )


def _password_login_available(
    *,
    identity: Mapping[str, Any],
    permissions: Mapping[str, Any],
) -> bool:
    return bool(
        permissions.get("password_login_enabled")
        and identity["users"]["active"] > 0
    )


def coverage_status() -> dict[str, list[str]]:
    """Return route groups covered by permission policy checks."""
    return {
        "policy_checked": [
            "object execution and mutation routes",
            "object source, state, logs, files, metadata, and versions",
            "collection record read, create, update, and delete routes",
        ],
        "admin_gated": [
            "permissions policy, status, check, and audit routes",
            "identity account, user, and session routes",
            "schema metadata routes",
            "package install and restore routes",
            "event publish, retention, and subscription routes",
        ],
    }


def _empty_policy_status(*, error: str, policy_file_exists: bool) -> dict[str, Any]:
    return {
        "valid": False,
        "error": error,
        "policy_file_exists": policy_file_exists,
        "access_mode": None,
        "rules_count": 0,
        "allow_rules": 0,
        "deny_rules": 0,
        "roles_count": 0,
        "user_roles_count": 0,
        "admin_roles": [],
        "principals": [],
        "actions": [],
        "collections": [],
        "objects": [],
        "temporary_rules": 0,
    }


def _count_status(items: list[dict[str, Any]], status: str) -> int:
    return sum(1 for item in items if item.get("status") == status)
