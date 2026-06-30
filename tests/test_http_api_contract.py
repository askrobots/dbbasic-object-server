from pathlib import Path

import pytest

import http_api_contract


def test_contract_keeps_objects_paths_as_public_surface():
    assert http_api_contract.OBJECTS_PATH == "/objects"
    assert http_api_contract.OBJECT_PATH == "/objects/{object_id}"
    assert http_api_contract.OBJECT_STATION_PATH == "/objects/{object_id}@{station_id}"
    assert http_api_contract.COLLECTIONS_PATH == "/collections"
    assert http_api_contract.COLLECTION_PATH == "/collections/{collection}"
    assert http_api_contract.COLLECTION_RECORDS_PATH == "/collections/{collection}/records"
    assert http_api_contract.COLLECTION_RECORD_PATH == "/collections/{collection}/records/{record_id}"
    assert http_api_contract.COLLECTION_CHANGES_PATH == "/collections/{collection}/changes"
    assert (
        http_api_contract.COLLECTION_RECORD_CHANGES_PATH
        == "/collections/{collection}/records/{record_id}/changes"
    )
    assert http_api_contract.SCHEMAS_PATH == "/schemas"
    assert http_api_contract.SCHEMA_PATH == "/schemas/{collection}"
    assert http_api_contract.EVENTS_PATH == "/events"
    assert http_api_contract.EVENT_SUBSCRIPTIONS_PATH == "/events/subscriptions"
    assert http_api_contract.PERMISSIONS_POLICY_PATH == "/permissions/policy"
    assert http_api_contract.PERMISSIONS_CHECK_PATH == "/permissions/check"
    assert http_api_contract.PERMISSIONS_AUDIT_PATH == "/permissions/audit"


def test_contract_keeps_existing_introspection_query_flags():
    assert http_api_contract.SOURCE_QUERY == {"source": "true", "format": "json"}
    assert http_api_contract.STATE_QUERY == {"state": "true"}
    assert http_api_contract.METADATA_QUERY == {"metadata": "true"}
    assert http_api_contract.FILES_QUERY == {"files": "true"}
    assert http_api_contract.FILE_QUERY == {"file": "name"}
    assert http_api_contract.LOGS_QUERY == {
        "logs": "true",
        "format": "json",
        "limit": "100",
    }
    assert http_api_contract.VERSIONS_QUERY == {"versions": "true", "limit": "10"}


@pytest.mark.parametrize(
    ("response_name", "payload"),
    [
        (
            "object_list",
            {"status": "ok", "objects": [], "count": 0},
        ),
        (
            "collection_list",
            {"status": "ok", "collections": [], "count": 0},
        ),
        (
            "collection",
            {"status": "ok", "collection": {"name": "site", "object_count": 1}},
        ),
        (
            "record_list",
            {
                "status": "ok",
                "collection": "contacts",
                "records": [],
                "count": 0,
                "total": 0,
            },
        ),
        (
            "record",
            {"status": "ok", "collection": "contacts", "record": {"id": "c1"}},
        ),
        (
            "record_changes",
            {"status": "ok", "collection": "contacts", "changes": [], "count": 0, "total": 0},
        ),
        (
            "schema_list",
            {"status": "ok", "schemas": [], "count": 0},
        ),
        (
            "schema",
            {"status": "ok", "schema": {"name": "invoices", "fields": []}},
        ),
        (
            "event_list",
            {"status": "ok", "events": [], "count": 0, "total": 0},
        ),
        (
            "event",
            {"status": "ok", "event": {"id": "evt_1", "event_type": "invoice.created"}},
        ),
        (
            "event_retention",
            {"status": "ok", "retention": {"deleted": 0, "kept": 0}},
        ),
        (
            "event_subscription_list",
            {"status": "ok", "subscriptions": [], "count": 0, "total": 0},
        ),
        (
            "event_subscription",
            {"status": "ok", "subscription": {"id": "scroll"}},
        ),
        (
            "create_object",
            {"status": "ok", "object_id": "u_42_deals", "message": "created"},
        ),
        (
            "error",
            {"status": "error", "error": "Execution failed: boom"},
        ),
        (
            "source",
            {"status": "ok", "object_id": "basics_counter", "source": "def GET(request): ..."},
        ),
        (
            "update_source",
            {
                "status": "ok",
                "message": "Code updated to version 2",
                "version_id": 2,
                "object_id": "u_42_deals",
            },
        ),
        (
            "state",
            {"status": "ok", "object_id": "basics_counter", "state": {"count": "3"}},
        ),
        (
            "metadata",
            {
                "status": "ok",
                "object_id": "basics_counter",
                "metadata": {"version_count": 2},
            },
        ),
        (
            "files",
            {"status": "ok", "object_id": "basics_counter", "files": [], "count": 0},
        ),
        (
            "logs",
            {"status": "ok", "object_id": "basics_counter", "logs": [], "count": 0},
        ),
        (
            "versions",
            {"status": "ok", "object_id": "basics_counter", "versions": [], "count": 0},
        ),
        (
            "version",
            {"status": "ok", "object_id": "basics_counter", "version": {"version_id": 2}},
        ),
        (
            "rollback",
            {
                "status": "ok",
                "message": "Rolled back to version 1",
                "version_id": 1,
                "object_id": "u_42_deals",
            },
        ),
        (
            "destroy_object",
            {"status": "ok", "message": "Object destroyed: u_42_deals", "object_id": "u_42_deals"},
        ),
        (
            "permissions_policy",
            {"status": "ok", "policy": {"access_mode": "role_based"}},
        ),
        (
            "permissions_check",
            {"status": "ok", "decision": {"allowed": True}},
        ),
        (
            "permissions_audit",
            {"status": "ok", "entries": [], "count": 0},
        ),
    ],
)
def test_existing_client_response_shapes_have_required_fields(response_name, payload):
    assert http_api_contract.missing_response_fields(response_name, payload) == set()


def test_missing_response_fields_reports_contract_breaks():
    payload = {"status": "ok", "object_id": "basics_counter"}

    assert http_api_contract.missing_response_fields("source", payload) == {"source"}


def test_unknown_response_shape_is_rejected():
    with pytest.raises(ValueError, match="Unknown HTTP API response shape"):
        http_api_contract.required_response_fields("new_parallel_api")


def test_rollback_keeps_legacy_version_id_even_if_new_version_id_is_added():
    payload = {
        "status": "ok",
        "message": "Rolled back to version 1",
        "version_id": 1,
        "new_version_id": 3,
        "object_id": "u_42_deals",
    }

    assert http_api_contract.missing_response_fields("rollback", payload) == set()


def test_http_contract_doc_mentions_required_compatibility_surface():
    doc = Path("docs/http-api-contract.md").read_text()

    required_fragments = [
        "GET /objects?format=json",
        "GET /collections",
        "GET /collections/{collection}",
        "GET /collections/{collection}/records",
        "POST /collections/{collection}/records",
        "GET /collections/{collection}/records/{record_id}",
        "PUT /collections/{collection}/records/{record_id}",
        "DELETE /collections/{collection}/records/{record_id}",
        "GET /collections/{collection}/changes",
        "GET /collections/{collection}/records/{record_id}/changes",
        "GET /schemas",
        "GET /schemas/{collection}",
        "GET /schemas/{collection}?versions=true&limit=10",
        "GET /schemas/{collection}?version=1",
        "PUT /schemas/{collection}",
        "POST /schemas/{collection}",
        "GET /events",
        "POST /events",
        "DELETE /events",
        "GET /events/subscriptions",
        "POST /events/subscriptions",
        "DELETE /events/subscriptions",
        "POST /objects",
        "GET /objects/{object_id}",
        "PUT /objects/{object_id}?source=true",
        "POST /objects/{object_id}",
        "DELETE /objects/{object_id}?destroy=true",
        "GET /permissions/policy",
        "PUT /permissions/policy",
        "POST /permissions/check",
        "GET /permissions/audit",
        "source=true",
        "state=true",
        "metadata=true",
        "files=true",
        "file=report.txt",
        "versions=true",
        "action=rollback",
        "version_id",
        "new_version_id",
        "/api/v1",
    ]

    for fragment in required_fragments:
        assert fragment in doc
