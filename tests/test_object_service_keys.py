"""Tests for per-user service API keys: storage module and identity routes."""

import json

import pytest

import object_server
import object_service_keys

from test_object_server import (
    auth_headers,
    create_identity_session,
    enable_admin_token,
    request,
)


def test_set_list_and_remove_service_keys(tmp_path):
    result = object_service_keys.set_service_key("dan", "anthropic", "sk-test-123", base_dir=tmp_path)
    assert result["operation"] == "created"

    replaced = object_service_keys.set_service_key("dan", "anthropic", "sk-test-456", base_dir=tmp_path)
    assert replaced["operation"] == "replaced"
    object_service_keys.set_service_key("dan", "openai", "sk-other", base_dir=tmp_path)

    statuses = object_service_keys.list_service_key_status("dan", base_dir=tmp_path)
    assert [status["service"] for status in statuses] == ["anthropic", "openai"]
    assert all("key" not in status for status in statuses)

    assert object_service_keys.get_service_key("dan", "anthropic", base_dir=tmp_path) == "sk-test-456"
    assert object_service_keys.get_service_key("dan", "missing", base_dir=tmp_path) is None
    assert object_service_keys.get_service_key("other", "anthropic", base_dir=tmp_path) is None

    assert object_service_keys.remove_service_key("dan", "openai", base_dir=tmp_path) is True
    assert object_service_keys.remove_service_key("dan", "openai", base_dir=tmp_path) is False
    assert object_service_keys.remove_all_service_keys("dan", base_dir=tmp_path) == 1


def test_service_keys_file_is_owner_only(tmp_path):
    object_service_keys.set_service_key("dan", "anthropic", "sk-secret", base_dir=tmp_path)
    path = object_service_keys.service_keys_path(tmp_path)
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("service", "key"),
    [
        ("Bad Service", "sk-x"),
        ("", "sk-x"),
        ("anthropic", ""),
        ("anthropic", "has\ttab"),
        ("anthropic", "x" * 5000),
    ],
)
def test_invalid_service_key_payloads_rejected(tmp_path, service, key):
    with pytest.raises(object_service_keys.InvalidServiceKeyError):
        object_service_keys.set_service_key("dan", service, key, base_dir=tmp_path)


def test_is_reserved_service_marks_platform_prefixes():
    assert object_service_keys.is_reserved_service("sys-mailbox-abc") is True
    assert object_service_keys.is_reserved_service("sys-anything") is True
    # a user's own BYO key is never reserved
    assert object_service_keys.is_reserved_service("openai") is False
    assert object_service_keys.is_reserved_service("anthropic") is False
    assert object_service_keys.is_reserved_service("") is False
    assert object_service_keys.is_reserved_service(None) is False


def test_reserved_service_names_are_server_side_only(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    enable_admin_token(monkeypatch)
    token, _ = create_identity_session({"user_id": "dan"})
    bearer = [("authorization", f"Bearer {token}")]

    # A user cannot SET a platform-owned (reserved) secret via the self-service
    # endpoint -- so they can never overwrite one the platform provisioned.
    status, _, denied = request(
        "/identity/users/dan/service-keys",
        method="PUT",
        body=json.dumps({"service": "sys-mailbox-1", "key": "hunter2"}).encode(),
        headers=bearer + [("content-type", "application/json")],
    )
    assert status == 403 and "reserved" in denied["error"].lower()

    # Nor DELETE one.
    status, _, denied = request(
        "/identity/users/dan/service-keys/sys-mailbox-1",
        method="DELETE",
        headers=bearer,
    )
    assert status == 403

    # But in-process server code (a connector/provisioning verb) still may --
    # the restriction lives only at the HTTP boundary, and the value round-trips
    # for the server to use, while the endpoint never exposes it.
    object_service_keys.set_service_key("dan", "sys-mailbox-1", "provisioned-pw", base_dir=data_dir)
    assert object_service_keys.get_service_key("dan", "sys-mailbox-1", base_dir=data_dir) == "provisioned-pw"


def test_service_key_routes_are_self_service(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    enable_admin_token(monkeypatch)
    token, _ = create_identity_session({"user_id": "dan"})

    bearer = [("authorization", f"Bearer {token}")]

    status, _, set_response = request(
        "/identity/users/dan/service-keys",
        method="PUT",
        body=json.dumps({"service": "anthropic", "key": "sk-live-1"}).encode(),
        headers=bearer + [("content-type", "application/json")],
    )
    assert status == 200
    assert set_response["operation"] == "created"
    assert "key" not in set_response and "sk-live-1" not in json.dumps(set_response)

    status, _, listed = request("/identity/users/dan/service-keys", headers=bearer)
    assert status == 200
    assert [item["service"] for item in listed["services"]] == ["anthropic"]
    assert "sk-live-1" not in json.dumps(listed)

    # Another user cannot touch dan's keys without the admin gate.
    other_token, _ = create_identity_session({"user_id": "mallory"})
    status, _, denied = request(
        "/identity/users/dan/service-keys",
        headers=[("authorization", f"Bearer {other_token}")],
    )
    assert status == 401

    # No auth at all is denied too.
    status, _, _ = request("/identity/users/dan/service-keys")
    assert status == 401

    # Admin token can manage on behalf of a user.
    status, _, _ = request("/identity/users/dan/service-keys", headers=auth_headers())
    assert status == 200

    status, _, deleted = request(
        "/identity/users/dan/service-keys/anthropic",
        method="DELETE",
        headers=bearer,
    )
    assert status == 200 and deleted["deleted"] is True

    status, _, _ = request(
        "/identity/users/dan/service-keys/anthropic",
        method="DELETE",
        headers=bearer,
    )
    assert status == 404
