"""Tests for schema-driven cross-collection search matching and the endpoint."""

import json

import pytest

import object_search
import object_server

from test_object_server import (
    auth_headers,
    enable_admin_token,
    request,
    save_permission_policy,
    write_records,
)


TASKS_SCHEMA = {
    "name": "tasks",
    "search": {"fields": ["title", "instructions"]},
    "views": {"list_fields": ["title", "urgency"]},
}


def test_search_config_missing_returns_none():
    assert object_search.search_config({"name": "tasks"}) is None


def test_search_config_normalizes_fields_and_result_fields():
    config = object_search.search_config(TASKS_SCHEMA)
    assert config == {
        "fields": ["title", "instructions"],
        "result_fields": ["id", "title", "urgency"],
    }


def test_search_config_defaults_result_fields_to_search_fields():
    config = object_search.search_config({"search": {"fields": ["content"]}})
    assert config == {"fields": ["content"], "result_fields": ["id", "content"]}


def test_search_config_respects_explicit_result_fields():
    config = object_search.search_config(
        {"search": {"fields": ["title"], "result_fields": ["id", "status"]}}
    )
    assert config["result_fields"] == ["id", "status"]


@pytest.mark.parametrize(
    "section",
    [
        "title",
        {"fields": []},
        {"fields": "title"},
        {"fields": [1]},
        {"fields": ["title"], "result_fields": "id"},
        {"fields": ["title"], "result_fields": [None]},
    ],
)
def test_search_config_rejects_malformed_sections(section):
    with pytest.raises(object_search.InvalidSearchConfigError):
        object_search.search_config({"search": section})


def test_terms_are_anded_and_fields_are_ored():
    record = {"id": "t1", "title": "Fix flywheel", "instructions": "growth loop"}
    assert object_search.record_matches(record, "flywheel growth", ["title", "instructions"])
    assert object_search.record_matches(record, "FLYWHEEL", ["title", "instructions"])
    assert not object_search.record_matches(record, "flywheel missing", ["title", "instructions"])
    assert not object_search.record_matches(record, "flywheel", ["instructions"])


def test_blank_query_matches_nothing():
    record = {"id": "t1", "title": "anything"}
    assert not object_search.record_matches(record, "   ", ["title"])


def test_id_prefix_queries_match_record_ids():
    record = {"id": "9F3A2B10-demo", "title": "unrelated"}
    assert object_search.looks_like_id_prefix("9f3a")
    assert not object_search.looks_like_id_prefix("9f3")
    assert not object_search.looks_like_id_prefix("fix flywheel")
    assert object_search.record_matches(record, "9f3a", ["title"])
    assert not object_search.record_matches(record, "aaaa", ["title"])


def test_search_records_trims_to_result_fields_and_limit():
    config = object_search.search_config(TASKS_SCHEMA)
    records = [
        {"id": f"t{index}", "title": "flywheel", "urgency": "high", "instructions": "secret"}
        for index in range(5)
    ]
    matches = object_search.search_records(records, "flywheel", config, limit=3)
    assert len(matches) == 3
    assert matches[0] == {"id": "t0", "title": "flywheel", "urgency": "high"}
    assert all("instructions" not in match for match in matches)


def test_search_records_rejects_bad_limit():
    config = object_search.search_config(TASKS_SCHEMA)
    with pytest.raises(ValueError):
        object_search.search_records([], "query", config, limit=0)


def write_schema(data_dir, name, payload):
    schema_file = data_dir / "schemas" / f"{name}.json"
    schema_file.parent.mkdir(parents=True, exist_ok=True)
    schema_file.write_text(json.dumps(payload))


def searchable_fixture(data_dir):
    write_schema(
        data_dir,
        "notes",
        {"fields": [{"name": "id"}, {"name": "content"}], "search": {"fields": ["content"]}},
    )
    write_schema(
        data_dir,
        "tasks",
        {
            "fields": [{"name": "id"}, {"name": "title"}, {"name": "urgency"}],
            "search": {"fields": ["title"]},
            "views": {"list_fields": ["title", "urgency"]},
        },
    )
    write_records(
        data_dir,
        "notes",
        "id\tcontent\towner_id\nn1\tflywheel growth loop\t7\nn2\tunrelated memo\t8\n",
    )
    write_records(
        data_dir,
        "tasks",
        "id\ttitle\turgency\nt1\tSpin the flywheel\thigh\nt2\tOther work\tlow\n",
    )
    write_records(data_dir, "plain", "id\tname\np1\tflywheel but unsearchable\n")


def test_search_endpoint_requires_admin_token_by_default(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    searchable_fixture(data_dir)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/api/search", query_string="q=flywheel")
    assert status == 401
    assert "error" in payload

    status, _, payload = request(
        "/api/search", query_string="q=flywheel", headers=auth_headers()
    )
    assert status == 200
    assert payload["status"] == "ok"


def test_search_endpoint_matches_only_searchable_collections(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    searchable_fixture(data_dir)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/api/search", query_string="q=flywheel", headers=auth_headers()
    )

    assert status == 200
    assert payload["query"] == "flywheel"
    assert payload["results"]["notes"] == [{"id": "n1", "content": "flywheel growth loop"}]
    assert payload["results"]["tasks"] == [
        {"id": "t1", "title": "Spin the flywheel", "urgency": "high"}
    ]
    assert "plain" not in payload["results"]
    assert payload["total_count"] == 2


def test_search_endpoint_requires_query(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    searchable_fixture(data_dir)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/api/search", headers=auth_headers())
    assert status == 400
    assert "q" in payload["error"]


def test_search_endpoint_filters_requested_collections(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    searchable_fixture(data_dir)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/api/search",
        query_string="q=flywheel&collections=notes",
        headers=auth_headers(),
    )
    assert status == 200
    assert list(payload["results"]) == ["notes"]
    assert payload["total_count"] == 1


def test_search_endpoint_warns_on_malformed_search_config(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_schema(
        data_dir,
        "broken",
        {"fields": [{"name": "id"}], "search": {"fields": []}},
    )
    write_records(data_dir, "broken", "id\nb1\n")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/api/search", query_string="q=anything", headers=auth_headers()
    )
    assert status == 200
    assert payload["results"] == {}
    assert payload["warnings"] == ["broken: Schema search.fields must be a non-empty list"]


def test_search_endpoint_enforcement_applies_row_filters_and_skips_denied(
    tmp_path, monkeypatch
):
    data_dir = tmp_path / "data"
    searchable_fixture(data_dir)
    save_permission_policy(
        data_dir,
        {
            "access_mode": "role_based",
            "rules": [
                {
                    "effect": "allow",
                    "principal": "role:member",
                    "actions": ["read"],
                    "collection": "notes",
                    "row_filter": {"owner_id": "$user_id"},
                    "reason": "members read their own notes",
                }
            ],
        },
    )
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")
    monkeypatch.setenv(object_server.PERMISSION_TRUST_HEADERS_ENV, "true")
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/api/search",
        query_string="q=flywheel",
        headers=[("x-dbbasic-user-id", "7"), ("x-dbbasic-roles", "member")],
    )

    assert status == 200
    assert payload["results"] == {
        "notes": [{"id": "n1", "content": "flywheel growth loop"}]
    }
    assert "tasks" not in payload["results"]
    assert payload["total_count"] == 1

    status, _, payload = request(
        "/api/search",
        query_string="q=unrelated",
        headers=[("x-dbbasic-user-id", "7"), ("x-dbbasic-roles", "member")],
    )
    assert status == 200
    assert payload["results"]["notes"] == []
    assert payload["total_count"] == 0
