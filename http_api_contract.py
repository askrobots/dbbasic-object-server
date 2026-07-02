"""Compatibility constants for the DBBASIC Object HTTP API.

This is not a router. It records the response fields and paths existing clients
expect so future server code can be tested against the same contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

OBJECTS_PATH = "/objects"
OBJECT_PATH = "/objects/{object_id}"
OBJECT_STATION_PATH = "/objects/{object_id}@{station_id}"
ADMIN_OBJECTS_PATH = "/admin/objects"
ADMIN_OBJECT_PATH = "/admin/objects/{object_id}"
ADMIN_COLLECTIONS_PATH = "/admin/collections"
ADMIN_COLLECTION_PATH = "/admin/collections/{collection}"
ADMIN_COLLECTION_RECORDS_PATH = "/admin/collections/{collection}/records"
ADMIN_COLLECTION_RECORD_PATH = "/admin/collections/{collection}/records/{record_id}"
ADMIN_COLLECTION_CHANGES_PATH = "/admin/collections/{collection}/changes"
ADMIN_COLLECTION_RECORD_CHANGES_PATH = (
    "/admin/collections/{collection}/records/{record_id}/changes"
)
ADMIN_SCHEMAS_PATH = "/admin/schemas"
ADMIN_SCHEMA_PATH = "/admin/schemas/{collection}"
ADMIN_IDENTITY_ACCOUNTS_PATH = "/admin/identity/accounts"
ADMIN_IDENTITY_ACCOUNT_PATH = "/admin/identity/accounts/{account_id}"
ADMIN_IDENTITY_USERS_PATH = "/admin/identity/users"
ADMIN_IDENTITY_USER_PATH = "/admin/identity/users/{user_id}"
ADMIN_IDENTITY_USER_PASSWORD_PATH = "/admin/identity/users/{user_id}/password"
ADMIN_IDENTITY_SESSIONS_PATH = "/admin/identity/sessions"
ADMIN_IDENTITY_SESSION_PATH = "/admin/identity/sessions/{session_id}"
COLLECTIONS_PATH = "/collections"
COLLECTION_PATH = "/collections/{collection}"
COLLECTION_RECORDS_PATH = "/collections/{collection}/records"
COLLECTION_RECORD_PATH = "/collections/{collection}/records/{record_id}"
COLLECTION_CHANGES_PATH = "/collections/{collection}/changes"
COLLECTION_RECORD_CHANGES_PATH = "/collections/{collection}/records/{record_id}/changes"
SCHEMAS_PATH = "/schemas"
SCHEMA_PATH = "/schemas/{collection}"
EVENTS_PATH = "/events"
EVENT_DELIVERIES_PATH = "/events/deliveries"
EVENT_SUBSCRIPTIONS_PATH = "/events/subscriptions"
PACKAGES_PATH = "/packages"
PACKAGE_PATH = "/packages/{package_id}"
PACKAGE_INSTALL_PATH = "/packages/{package_id}/install"
PACKAGE_RESTORE_PATH = "/packages/{package_id}/restore"
PACKAGE_CHANGES_PATH = "/packages/{package_id}/changes"
ADMIN_STATUS_PATH = "/admin/status"
ADMIN_CHANGES_PATH = "/admin/changes"
ADMIN_FILES_PATH = "/admin/files"
ADMIN_OBJECT_FILES_PATH = "/admin/files/{object_id}"
DAEMON_STATUS_PATH = "/daemon/status"
DAEMON_SCHEDULER_TASKS_PATH = "/daemon/scheduler/tasks"
DAEMON_SCHEDULER_TASK_PATH = "/daemon/scheduler/tasks/{task_id}"
DAEMON_QUEUE_MESSAGES_PATH = "/daemon/queue/messages"
DAEMON_QUEUE_MESSAGE_PATH = "/daemon/queue/messages/{message_id}"
IDENTITY_PATH = "/identity"
IDENTITY_ACCOUNTS_PATH = "/identity/accounts"
IDENTITY_ACCOUNT_PATH = "/identity/accounts/{account_id}"
IDENTITY_USERS_PATH = "/identity/users"
IDENTITY_USER_PATH = "/identity/users/{user_id}"
IDENTITY_USER_PASSWORD_PATH = "/identity/users/{user_id}/password"
IDENTITY_SESSIONS_PATH = "/identity/sessions"
IDENTITY_SESSION_PATH = "/identity/sessions/{session_id}"
IDENTITY_CURRENT_SESSION_PATH = "/identity/session"
LOGIN_PATH = "/login"
LOGOUT_PATH = "/logout"
PERMISSIONS_POLICY_PATH = "/permissions/policy"
PERMISSIONS_STATUS_PATH = "/permissions/status"
PERMISSIONS_CHECK_PATH = "/permissions/check"
PERMISSIONS_AUDIT_PATH = "/permissions/audit"

SOURCE_QUERY = {"source": "true", "format": "json"}
STATE_QUERY = {"state": "true"}
METADATA_QUERY = {"metadata": "true"}
FILES_QUERY = {"files": "true"}
FILE_QUERY = {"file": "name"}
LOGS_QUERY = {"logs": "true", "format": "json", "limit": "100"}
SOURCE_CHANGES_QUERY = {"source_changes": "true", "limit": "100"}
CHANGES_QUERY = {"changes": "true", "limit": "100"}
VERSIONS_QUERY = {"versions": "true", "limit": "10"}

RESPONSE_FIELDS: dict[str, frozenset[str]] = {
    "object_list": frozenset({"status", "objects", "count"}),
    "collection_list": frozenset({"status", "collections", "count"}),
    "collection": frozenset({"status", "collection"}),
    "record_list": frozenset({"status", "collection", "records", "count", "total"}),
    "record": frozenset({"status", "collection", "record"}),
    "record_changes": frozenset({"status", "collection", "changes", "count", "total"}),
    "schema_list": frozenset({"status", "schemas", "count"}),
    "schema": frozenset({"status", "schema"}),
    "event_list": frozenset({"status", "events", "count", "total"}),
    "event": frozenset({"status", "event"}),
    "event_retention": frozenset({"status", "retention"}),
    "event_delivery_list": frozenset({"status", "deliveries", "count", "total"}),
    "event_subscription_list": frozenset(
        {"status", "subscriptions", "count", "total"}
    ),
    "event_subscription": frozenset({"status", "subscription"}),
    "package_list": frozenset({"status", "packages", "count"}),
    "package": frozenset({"status", "package"}),
    "package_dry_run": frozenset({"status", "dry_run", "change"}),
    "package_install": frozenset({"status", "install", "changes", "restore_point"}),
    "package_restore": frozenset(
        {"status", "restore", "changes", "restore_point", "from_change"}
    ),
    "package_changes": frozenset({"status", "package_id", "changes", "count", "total"}),
    "admin_changes": frozenset({"status", "changes", "count", "total"}),
    "admin_status": frozenset(
        {"status", "timestamp", "version", "health", "inventory", "capabilities", "packages"}
    ),
    "daemon_status": frozenset(
        {"status", "timestamp", "daemon", "scheduler", "queue", "events", "cleanup"}
    ),
    "daemon_scheduler_task_list": frozenset({"status", "tasks", "count", "total"}),
    "daemon_scheduler_task": frozenset({"status", "task"}),
    "daemon_queue_message_list": frozenset({"status", "messages", "count", "total"}),
    "daemon_queue_message": frozenset({"status", "message"}),
    "identity": frozenset({"status", "subject", "auth", "permissions"}),
    "identity_account_list": frozenset({"status", "accounts", "count"}),
    "identity_account": frozenset({"status", "account"}),
    "identity_user_list": frozenset({"status", "users", "count"}),
    "identity_user": frozenset({"status", "user"}),
    "identity_session_list": frozenset({"status", "sessions", "count"}),
    "identity_session_create": frozenset({"status", "session", "token"}),
    "identity_session": frozenset({"status", "session"}),
    "identity_current_session": frozenset({"status", "session"}),
    "create_object": frozenset({"status", "object_id", "message"}),
    "error": frozenset({"status", "error"}),
    "source": frozenset({"status", "object_id", "source"}),
    "update_source": frozenset({"status", "message", "version_id", "object_id"}),
    "state": frozenset({"status", "object_id", "state"}),
    "metadata": frozenset({"status", "object_id", "metadata"}),
    "files": frozenset({"status", "object_id", "files", "count"}),
    "file_list": frozenset({"status", "files", "count", "total"}),
    "logs": frozenset({"status", "object_id", "logs", "count"}),
    "source_changes": frozenset({"status", "object_id", "changes", "count", "total"}),
    "object_changes": frozenset({"status", "object_id", "changes", "count", "total"}),
    "versions": frozenset({"status", "object_id", "versions", "count"}),
    "version": frozenset({"status", "object_id", "version"}),
    "rollback": frozenset({"status", "message", "version_id", "object_id"}),
    "destroy_object": frozenset({"status", "message", "object_id"}),
    "permissions_policy": frozenset({"status", "policy"}),
    "permissions_status": frozenset(
        {"status", "permissions", "identity", "policy", "coverage", "readiness", "warnings"}
    ),
    "permissions_check": frozenset({"status", "decision"}),
    "permissions_audit": frozenset({"status", "entries", "count"}),
}


def required_response_fields(response_name: str) -> frozenset[str]:
    """Return required top-level fields for a named compatibility response."""
    try:
        return RESPONSE_FIELDS[response_name]
    except KeyError as exc:
        raise ValueError(f"Unknown HTTP API response shape: {response_name}") from exc


def missing_response_fields(response_name: str, payload: Mapping[str, Any]) -> set[str]:
    """Return required compatibility fields missing from a response payload."""
    return set(required_response_fields(response_name) - payload.keys())
