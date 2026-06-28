from pathlib import Path

import pytest

import http_api_contract


def test_contract_keeps_objects_paths_as_public_surface():
    assert http_api_contract.OBJECTS_PATH == "/objects"
    assert http_api_contract.OBJECT_PATH == "/objects/{object_id}"
    assert http_api_contract.OBJECT_STATION_PATH == "/objects/{object_id}@{station_id}"


def test_contract_keeps_existing_introspection_query_flags():
    assert http_api_contract.SOURCE_QUERY == {"source": "true", "format": "json"}
    assert http_api_contract.STATE_QUERY == {"state": "true"}
    assert http_api_contract.METADATA_QUERY == {"metadata": "true"}
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
        "POST /objects",
        "GET /objects/{object_id}",
        "PUT /objects/{object_id}?source=true",
        "POST /objects/{object_id}",
        "DELETE /objects/{object_id}?destroy=true",
        "source=true",
        "state=true",
        "versions=true",
        "action=rollback",
        "version_id",
        "new_version_id",
        "/api/v1",
    ]

    for fragment in required_fragments:
        assert fragment in doc
