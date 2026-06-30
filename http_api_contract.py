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
COLLECTIONS_PATH = "/collections"
COLLECTION_PATH = "/collections/{collection}"
COLLECTION_RECORDS_PATH = "/collections/{collection}/records"
COLLECTION_RECORD_PATH = "/collections/{collection}/records/{record_id}"
COLLECTION_CHANGES_PATH = "/collections/{collection}/changes"
COLLECTION_RECORD_CHANGES_PATH = "/collections/{collection}/records/{record_id}/changes"
SCHEMAS_PATH = "/schemas"
SCHEMA_PATH = "/schemas/{collection}"
EVENTS_PATH = "/events"
EVENT_SUBSCRIPTIONS_PATH = "/events/subscriptions"
PACKAGES_PATH = "/packages"
PACKAGE_PATH = "/packages/{package_id}"
PERMISSIONS_POLICY_PATH = "/permissions/policy"
PERMISSIONS_CHECK_PATH = "/permissions/check"
PERMISSIONS_AUDIT_PATH = "/permissions/audit"

SOURCE_QUERY = {"source": "true", "format": "json"}
STATE_QUERY = {"state": "true"}
METADATA_QUERY = {"metadata": "true"}
FILES_QUERY = {"files": "true"}
FILE_QUERY = {"file": "name"}
LOGS_QUERY = {"logs": "true", "format": "json", "limit": "100"}
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
    "event_subscription_list": frozenset(
        {"status", "subscriptions", "count", "total"}
    ),
    "event_subscription": frozenset({"status", "subscription"}),
    "package_list": frozenset({"status", "packages", "count"}),
    "package": frozenset({"status", "package"}),
    "package_dry_run": frozenset({"status", "dry_run"}),
    "create_object": frozenset({"status", "object_id", "message"}),
    "error": frozenset({"status", "error"}),
    "source": frozenset({"status", "object_id", "source"}),
    "update_source": frozenset({"status", "message", "version_id", "object_id"}),
    "state": frozenset({"status", "object_id", "state"}),
    "metadata": frozenset({"status", "object_id", "metadata"}),
    "files": frozenset({"status", "object_id", "files", "count"}),
    "logs": frozenset({"status", "object_id", "logs", "count"}),
    "versions": frozenset({"status", "object_id", "versions", "count"}),
    "version": frozenset({"status", "object_id", "version"}),
    "rollback": frozenset({"status", "message", "version_id", "object_id"}),
    "destroy_object": frozenset({"status", "message", "object_id"}),
    "permissions_policy": frozenset({"status", "policy"}),
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
