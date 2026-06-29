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
PERMISSIONS_POLICY_PATH = "/permissions/policy"
PERMISSIONS_CHECK_PATH = "/permissions/check"

SOURCE_QUERY = {"source": "true", "format": "json"}
STATE_QUERY = {"state": "true"}
METADATA_QUERY = {"metadata": "true"}
LOGS_QUERY = {"logs": "true", "format": "json", "limit": "100"}
VERSIONS_QUERY = {"versions": "true", "limit": "10"}

RESPONSE_FIELDS: dict[str, frozenset[str]] = {
    "object_list": frozenset({"status", "objects", "count"}),
    "create_object": frozenset({"status", "object_id", "message"}),
    "error": frozenset({"status", "error"}),
    "source": frozenset({"status", "object_id", "source"}),
    "update_source": frozenset({"status", "message", "version_id", "object_id"}),
    "state": frozenset({"status", "object_id", "state"}),
    "metadata": frozenset({"status", "object_id", "metadata"}),
    "logs": frozenset({"status", "object_id", "logs", "count"}),
    "versions": frozenset({"status", "object_id", "versions", "count"}),
    "version": frozenset({"status", "object_id", "version"}),
    "rollback": frozenset({"status", "message", "version_id", "object_id"}),
    "destroy_object": frozenset({"status", "message", "object_id"}),
    "permissions_policy": frozenset({"status", "policy"}),
    "permissions_check": frozenset({"status", "decision"}),
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
