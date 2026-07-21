"""Per-user API keys: the store (hashed at rest, verify-only), the auth path
(_permission_identity resolves a `dbk_` bearer to the owning user), and the
self-service create/list/revoke routes.
"""

import json
import stat

import object_api_keys
import object_identity
import object_server

from test_object_server import (
    auth_headers,
    create_identity_session,
    enable_admin_token,
    request,
)


# ---- store ----------------------------------------------------------------

def test_create_returns_token_once_and_stores_only_the_hash(tmp_path):
    meta, token = object_api_keys.create_api_key("dan", "laptop", base_dir=tmp_path)
    assert token.startswith("dbk_") and len(token) > 20
    assert meta["name"] == "laptop" and meta["key_id"] and meta["created_at"]
    assert "token" not in meta  # metadata never carries the token

    # the RAW token is never on disk -- only its sha256 hash
    on_disk = object_api_keys.api_keys_path(tmp_path).read_text()
    assert token not in on_disk
    assert object_identity.hash_token(token) in on_disk


def test_resolve_verifies_and_revoke_invalidates(tmp_path):
    meta, token = object_api_keys.create_api_key("dan", "ci", base_dir=tmp_path)
    assert object_api_keys.resolve_api_key(token, base_dir=tmp_path) == "dan"
    # wrong / prefixless tokens never resolve
    assert object_api_keys.resolve_api_key("dbk_bogus", base_dir=tmp_path) is None
    assert object_api_keys.resolve_api_key("not-a-dbk-token", base_dir=tmp_path) is None
    assert object_api_keys.resolve_api_key(None, base_dir=tmp_path) is None
    # revoke -> the token stops resolving
    assert object_api_keys.revoke_api_key("dan", meta["key_id"], base_dir=tmp_path) is True
    assert object_api_keys.resolve_api_key(token, base_dir=tmp_path) is None
    assert object_api_keys.revoke_api_key("dan", meta["key_id"], base_dir=tmp_path) is False


def test_list_is_per_user_and_hides_secrets(tmp_path):
    object_api_keys.create_api_key("dan", "a", base_dir=tmp_path)
    object_api_keys.create_api_key("dan", "b", base_dir=tmp_path)
    object_api_keys.create_api_key("mallory", "x", base_dir=tmp_path)
    dan = object_api_keys.list_api_keys("dan", base_dir=tmp_path)
    assert {k["name"] for k in dan} == {"a", "b"}
    assert all("token" not in k and "token_hash" not in k for k in dan)
    assert len(object_api_keys.list_api_keys("mallory", base_dir=tmp_path)) == 1


def test_file_is_owner_only(tmp_path):
    object_api_keys.create_api_key("dan", "k", base_dir=tmp_path)
    mode = stat.S_IMODE(object_api_keys.api_keys_path(tmp_path).stat().st_mode)
    assert mode == 0o600


# ---- the auth path --------------------------------------------------------

def test_permission_identity_resolves_api_key_bearer(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    object_identity.create_user({"user_id": "dan", "roles": ["member"]}, base_dir=data_dir)
    _meta, token = object_api_keys.create_api_key("dan", "agent", base_dir=data_dir)

    subject, method = object_server._permission_identity({"authorization": "Bearer " + token})
    assert method == "api_key" and subject.user_id == "dan" and "member" in subject.roles
    # q9-style Token scheme works too
    subject2, _ = object_server._permission_identity({"authorization": "Token " + token})
    assert subject2.user_id == "dan"
    # a bad key -> not this user (falls through to anonymous)
    anon, m = object_server._permission_identity({"authorization": "Bearer dbk_nope"})
    assert anon.user_id is None


# ---- self-service routes --------------------------------------------------

def test_api_key_routes_are_self_service(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    enable_admin_token(monkeypatch)
    object_identity.create_user({"user_id": "dan"}, base_dir=data_dir)
    token, _ = create_identity_session({"user_id": "dan"})
    bearer = [("authorization", f"Bearer {token}")]

    # create -> the raw token is returned exactly once
    status, _, created = request(
        "/identity/users/dan/api-keys", method="POST",
        body=json.dumps({"name": "laptop"}).encode(),
        headers=bearer + [("content-type", "application/json")])
    assert status == 200 and created["token"].startswith("dbk_") and created["name"] == "laptop"
    key_id = created["key_id"]

    # list -> status only, never the token
    status, _, listed = request("/identity/users/dan/api-keys", headers=bearer)
    assert status == 200 and [k["name"] for k in listed["keys"]] == ["laptop"]
    assert created["token"] not in json.dumps(listed)

    # another user cannot manage dan's keys
    other_token, _ = create_identity_session({"user_id": "mallory"})
    status, _, _ = request("/identity/users/dan/api-keys", headers=[("authorization", f"Bearer {other_token}")])
    assert status == 401

    # revoke
    status, _, deleted = request(f"/identity/users/dan/api-keys/{key_id}", method="DELETE", headers=bearer)
    assert status == 200 and deleted["deleted"] is True
    status, _, _ = request(f"/identity/users/dan/api-keys/{key_id}", method="DELETE", headers=bearer)
    assert status == 404
