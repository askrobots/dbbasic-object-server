"""Tests for multipart form parsing and upload-form object execution."""

import base64
import json

import pytest

import object_multipart
import object_server

from test_object_server import ANONYMOUS_IDENTITY, request, write_source


def multipart_body(parts, boundary="testboundary42"):
    chunks = []
    for name, filename, content_type, content in parts:
        chunks.append(f"--{boundary}\r\n".encode())
        if filename is not None:
            chunks.append(
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
            )
            chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode())
        else:
            chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(content if isinstance(content, bytes) else content.encode())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def test_parse_multipart_fields_and_files():
    body, content_type = multipart_body(
        [
            ("caption", None, None, "vacation photo"),
            ("photo", "beach.png", "image/png", b"\x89PNG-fake-bytes"),
        ]
    )

    payload = object_multipart.parse_multipart(body, content_type)

    assert payload["caption"] == "vacation photo"
    file_entry = payload["_files"]["photo"]
    assert file_entry["filename"] == "beach.png"
    assert file_entry["content_type"] == "image/png"
    assert file_entry["size"] == len(b"\x89PNG-fake-bytes")
    assert base64.b64decode(file_entry["content_base64"]) == b"\x89PNG-fake-bytes"


def test_parse_multipart_rejects_garbage():
    with pytest.raises(object_multipart.InvalidMultipartError):
        object_multipart.parse_multipart(b"stuff", "multipart/form-data")
    with pytest.raises(object_multipart.InvalidMultipartError):
        object_multipart.parse_multipart(b"not multipart", "text/plain")


def test_upload_form_post_to_object(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(
        root / "site" / "upload.py",
        "import base64\n"
        "def POST(request):\n"
        "    upload = request['_files']['document']\n"
        "    content = base64.b64decode(upload['content_base64'])\n"
        "    return {\n"
        "        'caption': request.get('caption'),\n"
        "        'filename': upload['filename'],\n"
        "        'bytes': len(content),\n"
        "        'first_word': content.split()[0].decode(),\n"
        "    }\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    body, content_type = multipart_body(
        [
            ("caption", None, None, "quarterly report"),
            ("document", "report.txt", "text/plain", b"hello multipart world"),
        ]
    )
    status, _, payload = request(
        "/objects/site_upload",
        method="POST",
        body=body,
        headers=[("content-type", content_type)],
    )

    assert status == 200
    assert payload == {
        "caption": "quarterly report",
        "filename": "report.txt",
        "bytes": 21,
        "first_word": "hello",
    }


def test_upload_form_via_site_route(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(
        root / "site" / "upload.py",
        "def POST(request):\n"
        "    return {'got': sorted(request['_files']), 'user': request['_identity']['user_id']}\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path / "data"))
    monkeypatch.setenv(object_server.SITE_ROUTES_ENV, "true")

    body, content_type = multipart_body([("attachment", "a.bin", "application/octet-stream", b"\x00\x01")])
    status, _, payload = request(
        "/upload",
        method="POST",
        body=body,
        headers=[("content-type", content_type)],
    )

    assert status == 200
    assert payload == {"got": ["attachment"], "user": None}


def test_malformed_multipart_returns_400(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "site" / "upload.py", "def POST(request):\n    return {}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, _, payload = request(
        "/objects/site_upload",
        method="POST",
        body=b"no boundary here",
        headers=[("content-type", "multipart/form-data")],
    )

    assert status == 400
    assert "boundary" in payload["error"]
