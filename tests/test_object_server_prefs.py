"""Tests for Phase 6 routes: GET/PUT /prefs, /prefs/{key}, GET /api/flags.

Mirrors the route-testing conventions in tests/test_object_server.py (the
`request` ASGI harness, `write_records` TSV fixtures) and
tests/test_object_site_routes.py (importing that harness rather than
duplicating it).
"""

import json

import object_server

from test_object_server import request, write_records


def _trust_headers(monkeypatch):
    monkeypatch.setenv(object_server.PERMISSION_TRUST_HEADERS_ENV, "true")


def _user(user_id):
    return [("x-dbbasic-user-id", user_id)]


# ---------------------------------------------------------------------------
# Helper function unit tests (factored out of the route handlers)
# ---------------------------------------------------------------------------


def test_valid_pref_key_rejects_traversal_and_bad_characters():
    assert object_server._valid_pref_key("theme") is True
    assert object_server._valid_pref_key("flag:kanban_view") is True
    assert object_server._valid_pref_key("a.b-c_9") is True
    assert object_server._valid_pref_key("") is False
    assert object_server._valid_pref_key("../etc/passwd") is False
    assert object_server._valid_pref_key("a/b") is False
    assert object_server._valid_pref_key("has space") is False
    assert object_server._valid_pref_key("_leading_underscore") is False


def test_upsert_user_pref_creates_then_updates_in_place(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    write_records(data_dir, "user_prefs", "id\towner_id\tkey\tvalue\n")

    created = object_server._upsert_user_pref("7", "theme", "dark")
    assert created["owner_id"] == "7"
    assert created["key"] == "theme"
    assert created["value"] == "dark"

    updated = object_server._upsert_user_pref("7", "theme", "light")
    assert updated["id"] == created["id"]
    assert updated["value"] == "light"

    assert object_server._user_prefs_map("7") == {"theme": "light"}
    assert object_server._find_user_pref("8", "theme") is None


def test_resolve_flags_user_override_beats_instance_value(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    write_records(
        data_dir,
        "feature_flags",
        "id\tflag\tvalue\tdescription\nf1\tkanban_view\toff\tKanban board\n",
    )
    write_records(
        data_dir,
        "user_prefs",
        "id\towner_id\tkey\tvalue\np1\t7\tflag:kanban_view\ton\n",
    )

    assert object_server._resolve_flags(None) == {"kanban_view": "off"}
    assert object_server._resolve_flags("7") == {"kanban_view": "on"}
    assert object_server._resolve_flags("9") == {"kanban_view": "off"}


# ---------------------------------------------------------------------------
# Route-level tests
# ---------------------------------------------------------------------------


def test_put_prefs_then_get_prefs_key_and_prefs_map(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    _trust_headers(monkeypatch)
    write_records(data_dir, "user_prefs", "id\towner_id\tkey\tvalue\n")
    headers = _user("7")

    put_status, _, put_payload = request(
        "/prefs/theme",
        method="PUT",
        body=json.dumps({"value": "dark"}).encode(),
        headers=headers,
    )
    assert put_status == 200
    assert put_payload == {"status": "ok", "key": "theme", "value": "dark"}

    get_status, _, get_payload = request("/prefs/theme", headers=headers)
    assert get_status == 200
    assert get_payload == {"status": "ok", "key": "theme", "value": "dark"}

    map_status, _, map_payload = request("/prefs", headers=headers)
    assert map_status == 200
    assert map_payload == {"status": "ok", "prefs": {"theme": "dark"}}

    put2_status, _, put2_payload = request(
        "/prefs/theme",
        method="PUT",
        body=json.dumps({"value": "light"}).encode(),
        headers=headers,
    )
    assert put2_status == 200
    assert put2_payload == {"status": "ok", "key": "theme", "value": "light"}

    get2_status, _, get2_payload = request("/prefs/theme", headers=headers)
    assert get2_payload == {"status": "ok", "key": "theme", "value": "light"}


def test_get_prefs_anonymous_returns_empty_map(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "user_prefs", "id\towner_id\tkey\tvalue\n")

    status, _, payload = request("/prefs")

    assert status == 200
    assert payload == {"status": "ok", "prefs": {}}


def test_get_prefs_key_anonymous_is_not_found(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "user_prefs", "id\towner_id\tkey\tvalue\n")

    status, _, payload = request("/prefs/theme")

    assert status == 404


def test_put_prefs_anonymous_requires_session(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "user_prefs", "id\towner_id\tkey\tvalue\n")

    status, _, payload = request(
        "/prefs/theme", method="PUT", body=json.dumps({"value": "dark"}).encode()
    )

    assert status == 401


def test_prefs_key_rejects_invalid_characters(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    _trust_headers(monkeypatch)
    write_records(data_dir, "user_prefs", "id\towner_id\tkey\tvalue\n")
    headers = _user("7")

    status, _, payload = request("/prefs/has space", headers=headers)
    assert status == 400

    status2, _, payload2 = request("/prefs/..%2Fadmin", headers=headers)
    assert status2 == 400


def test_prefs_route_rejects_unsupported_methods(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "user_prefs", "id\towner_id\tkey\tvalue\n")

    status, _, _ = request("/prefs", method="POST")
    assert status == 405

    status2, _, _ = request("/prefs/theme", method="DELETE")
    assert status2 == 405

    status3, _, _ = request("/api/flags", method="POST")
    assert status3 == 405


def test_user_cannot_read_another_users_pref(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    _trust_headers(monkeypatch)
    write_records(data_dir, "user_prefs", "id\towner_id\tkey\tvalue\n")

    request(
        "/prefs/theme",
        method="PUT",
        body=json.dumps({"value": "dark"}).encode(),
        headers=_user("7"),
    )

    own_status, _, own_payload = request("/prefs/theme", headers=_user("7"))
    other_status, _, other_payload = request("/prefs/theme", headers=_user("8"))
    other_map_status, _, other_map_payload = request("/prefs", headers=_user("8"))

    assert own_status == 200
    assert own_payload["value"] == "dark"
    assert other_status == 404
    assert other_map_payload == {"status": "ok", "prefs": {}}


def test_api_flags_user_override_beats_instance_and_works_anonymously(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    _trust_headers(monkeypatch)
    write_records(
        data_dir,
        "feature_flags",
        "id\tflag\tvalue\tdescription\nf1\tkanban_view\toff\tKanban board\n",
    )
    write_records(data_dir, "user_prefs", "id\towner_id\tkey\tvalue\n")

    anon_status, _, anon_payload = request("/api/flags")
    assert anon_status == 200
    assert anon_payload == {"status": "ok", "flags": {"kanban_view": "off"}}

    request(
        "/prefs/flag:kanban_view",
        method="PUT",
        body=json.dumps({"value": "on"}).encode(),
        headers=_user("7"),
    )

    user_status, _, user_payload = request("/api/flags", headers=_user("7"))
    assert user_status == 200
    assert user_payload == {"status": "ok", "flags": {"kanban_view": "on"}}

    other_status, _, other_payload = request("/api/flags", headers=_user("8"))
    assert other_status == 200
    assert other_payload == {"status": "ok", "flags": {"kanban_view": "off"}}
