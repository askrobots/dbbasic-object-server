"""Tests for user file storage: bytes module and the /api/files surface."""

import json

import pytest

import object_server
import object_user_files

from test_object_server import (
    create_identity_session,
    enable_admin_token,
    raw_request,
    request,
    save_permission_policy,
    write_records,
)

FILES_POLICY = {
    "access_mode": "role_based",
    "rules": [
        {
            "effect": "allow",
            "principal": "registered",
            "actions": ["create", "read", "update", "delete"],
            "collection": "files",
            "row_filter": {"owner_id": "$user_id"},
            "reason": "own files",
        },
        {
            "effect": "allow",
            "principal": "public",
            "actions": ["read"],
            "collection": "files",
            "row_filter": {"is_public": "true"},
            "reason": "public files",
        },
    ],
}


def multipart_body(field, filename, content, extra=()):
    boundary = "testboundary42"
    parts = []
    for name, value in extra:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
            f'filename="{filename}"\r\nContent-Type: text/plain\r\n\r\n'
        ).encode()
        + content
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def files_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(
        data_dir,
        "files",
        "id\tfilename\tcontent_type\tsize\tdescription\tproject_id\tis_public\towner_id\n",
    )
    schema_file = data_dir / "schemas" / "files.json"
    schema_file.parent.mkdir(parents=True, exist_ok=True)
    schema_file.write_text(
        json.dumps(
            {
                "fields": [
                    {"name": "id"},
                    {"name": "filename", "required": True},
                    {"name": "content_type"},
                    {"name": "size", "type": "integer"},
                    {"name": "description"},
                    {"name": "is_public", "type": "boolean", "default": "false"},
                    {"name": "owner_id"},
                ]
            }
        )
    )
    save_permission_policy(data_dir, FILES_POLICY)
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    monkeypatch.setenv(object_server.USER_FILES_ENABLED_ENV, "true")
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")
    enable_admin_token(monkeypatch)
    return data_dir


def test_module_save_read_delete_and_usage(tmp_path):
    size = object_user_files.save_file("dan", "f1", b"hello", base_dir=tmp_path)
    assert size == 5
    assert object_user_files.read_file("dan", "f1", base_dir=tmp_path) == b"hello"
    object_user_files.save_file("dan", "f2", b"world!", base_dir=tmp_path)
    assert object_user_files.usage_bytes("dan", base_dir=tmp_path) == 11
    assert object_user_files.delete_file("dan", "f1", base_dir=tmp_path) is True
    assert object_user_files.delete_file("dan", "f1", base_dir=tmp_path) is False
    with pytest.raises(object_user_files.UserFileNotFoundError):
        object_user_files.read_file("dan", "f1", base_dir=tmp_path)
    with pytest.raises(object_user_files.InvalidUserFileError):
        object_user_files.file_path("../etc", "f1", base_dir=tmp_path)
    with pytest.raises(object_user_files.InvalidUserFileError):
        object_user_files.file_path("dan", "../../secrets", base_dir=tmp_path)


def test_upload_download_share_delete_flow(tmp_path, monkeypatch):
    data_dir = files_env(tmp_path, monkeypatch)
    token, _ = create_identity_session({"user_id": "dan"})
    bearer = [("authorization", f"Bearer {token}")]

    body, content_type = multipart_body("file", "notes.txt", b"file bytes here")
    status, _, uploaded = request(
        "/api/files",
        method="POST",
        body=body,
        headers=bearer + [("content-type", content_type)],
    )
    assert status == 201, uploaded
    file_id = uploaded["file"]["id"]
    assert uploaded["file"]["size"] == "15"
    assert uploaded["file"]["owner_id"] == "dan"

    status, response_headers, payload = raw_request(f"/api/files/{file_id}", headers=bearer)
    assert status == 200
    assert payload == b"file bytes here"
    assert response_headers[b"content-type"] == b"text/plain"

    # Anonymous cannot download a private file.
    status, _, _ = request(f"/api/files/{file_id}")
    assert status == 403

    # Owner shares it; anonymous download works.
    status, _, _ = request(
        f"/collections/files/records/{file_id}",
        method="PUT",
        body=json.dumps({"is_public": "true"}).encode(),
        headers=bearer + [("content-type", "application/json")],
    )
    assert status == 200
    status, _, payload = raw_request(f"/api/files/{file_id}")
    assert status == 200 and payload == b"file bytes here"

    # Another user cannot delete it; the owner can, and bytes go away.
    other_token, _ = create_identity_session({"user_id": "mallory"})
    status, _, _ = request(
        f"/api/files/{file_id}",
        method="DELETE",
        headers=[("authorization", f"Bearer {other_token}")],
    )
    assert status == 403
    status, _, deleted = request(f"/api/files/{file_id}", method="DELETE", headers=bearer)
    assert status == 200 and deleted["deleted"] is True
    assert object_user_files.usage_bytes("dan", base_dir=data_dir) == 0
    status, _, _ = request(f"/api/files/{file_id}", headers=bearer)
    assert status == 404


def test_upload_respects_quota_and_session(tmp_path, monkeypatch):
    files_env(tmp_path, monkeypatch)
    monkeypatch.setenv(object_server.USER_FILES_QUOTA_ENV, "10")
    token, _ = create_identity_session({"user_id": "dan"})
    bearer = [("authorization", f"Bearer {token}")]

    body, content_type = multipart_body("file", "big.txt", b"x" * 11)
    status, _, over = request(
        "/api/files", method="POST", body=body,
        headers=bearer + [("content-type", content_type)],
    )
    assert status == 413 and over["code"] == "quota_exceeded"

    status, _, _ = request(
        "/api/files", method="POST", body=body, headers=[("content-type", content_type)]
    )
    assert status == 401
