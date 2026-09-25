"""A record created with a per-user API key is owned by that key's user.

Why it matters: owner-scoped rules (`row_filter: {"owner_id": "$user_id"}`)
are how most apps keep people's records their own. Sessions were stamped
server-side; API keys -- the path agents, scripts and desktop apps use --
were not, so their records kept whatever owner the client claimed, or none,
and fell outside their own owner's rules (Webmaster publishing a site with
an API key is how this was found).
"""

import json

from test_object_server import request, save_permission_policy, write_records

import object_api_keys
import object_server

POLICY = {
    "access_mode": "role_based",
    "rules": [
        {"effect": "allow", "principal": "registered", "actions": ["create", "read", "update", "delete"],
         "collection": "things", "row_filter": {"owner_id": "$user_id"}, "reason": "own things"},
    ],
}


def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    write_records(data, "things", "id\tname\towner_id\n")
    schema = data / "schemas" / "things.json"
    schema.parent.mkdir(parents=True, exist_ok=True)
    schema.write_text(json.dumps({"fields": [{"name": "id"}, {"name": "name"}, {"name": "owner_id"}]}))
    save_permission_policy(data, POLICY)
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data))
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")
    monkeypatch.setenv(object_server.PERMISSION_UNREADY_ENFORCEMENT_ENV, "true")
    return data


def create(token, body):
    return request("/collections/things/records", method="POST", body=json.dumps(body).encode(),
                   headers=[("authorization", f"Bearer {token}"), ("content-type", "application/json")])


def test_an_api_key_owns_what_it_creates(tmp_path, monkeypatch):
    data = env(tmp_path, monkeypatch)
    _, token = object_api_keys.create_api_key("dan", "script", base_dir=data)
    status, _, payload = create(token, {"name": "mine", "owner_id": "dan"})
    assert status == 201, payload
    assert payload["record"]["owner_id"] == "dan"


def test_an_api_key_cannot_create_for_someone_else(tmp_path, monkeypatch):
    """The owner rule is checked against what the client sent, as for a
    session: claiming another owner is refused, never quietly accepted."""
    data = env(tmp_path, monkeypatch)
    _, token = object_api_keys.create_api_key("dan", "script", base_dir=data)
    status, _, _ = create(token, {"name": "sneaky", "owner_id": "eve"})
    assert status == 403


def test_the_stamp_is_the_keys_user_not_the_clients_claim(monkeypatch, tmp_path):
    """Where a rule lets a client write any owner_id (no owner row filter),
    the stored owner is still the key's user, as it is for a session."""
    data = env(tmp_path, monkeypatch)
    save_permission_policy(data, {"access_mode": "role_based", "rules": [
        {"effect": "allow", "principal": "registered", "actions": ["create", "read"],
         "collection": "things", "reason": "anyone signed in"}]})
    _, token = object_api_keys.create_api_key("dan", "script", base_dir=data)
    status, _, payload = create(token, {"name": "x", "owner_id": "eve"})
    assert status == 201, payload
    assert payload["record"]["owner_id"] == "dan"
