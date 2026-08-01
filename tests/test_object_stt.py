"""Tests for speech-to-text: the object_stt module and the /api/stt surface."""

import io
import json
import urllib.error

import pytest

import object_server
import object_stt

from test_object_server import create_identity_session, enable_admin_token, raw_request, request


def stt_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    monkeypatch.setenv(object_server.STT_ENABLED_ENV, "true")
    enable_admin_token(monkeypatch)
    return data_dir


def signed_in_bearer():
    token, _ = create_identity_session({"user_id": "dan"})
    return [("authorization", f"Bearer {token}")]


class _FakeHttpResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_stt_endpoint_requires_flag_and_session(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    enable_admin_token(monkeypatch)

    status, _, disabled = request(
        "/api/stt", method="POST", body=b"fake-audio-bytes",
        headers=[("content-type", "audio/webm")],
    )
    assert status == 403 and "disabled" in disabled["error"]

    monkeypatch.setenv(object_server.STT_ENABLED_ENV, "true")
    status, _, anonymous = request(
        "/api/stt", method="POST", body=b"fake-audio-bytes",
        headers=[("content-type", "audio/webm")],
    )
    assert status == 401


def test_stt_endpoint_rejects_empty_body(tmp_path, monkeypatch):
    stt_env(tmp_path, monkeypatch)
    bearer = signed_in_bearer()

    status, _, body = request(
        "/api/stt", method="POST", body=b"",
        headers=bearer + [("content-type", "audio/webm")],
    )
    assert status == 400
    assert "audio" in body["error"]


def test_stt_endpoint_requires_stored_key(tmp_path, monkeypatch):
    stt_env(tmp_path, monkeypatch)
    bearer = signed_in_bearer()

    status, _, body = request(
        "/api/stt", method="POST", body=b"fake-audio-bytes",
        headers=bearer + [("content-type", "audio/webm")],
    )
    assert status == 400
    assert "service-keys" in body["error"]


def test_stt_endpoint_transcribes_via_stored_key(tmp_path, monkeypatch):
    stt_env(tmp_path, monkeypatch)
    bearer = signed_in_bearer()

    request(
        "/identity/users/dan/service-keys",
        method="PUT",
        body=json.dumps({"service": "openai", "key": "sk-test-openai"}).encode(),
        headers=bearer + [("content-type", "application/json")],
    )

    calls = []

    def fake_transport(request_obj, timeout=None):
        calls.append(request_obj)
        response_body = json.dumps({"text": "show me my notes"}).encode()
        return _FakeHttpResponse(200, response_body)

    monkeypatch.setattr(object_server.urllib.request, "urlopen", fake_transport)

    status, _, body = request(
        "/api/stt",
        method="POST",
        body=b"\x1aE\xdf\xa3fake-webm-bytes",
        headers=bearer + [("content-type", "audio/webm")],
    )
    assert status == 200
    assert body["text"] == "show me my notes"
    assert len(calls) == 1
    assert calls[0].full_url == object_stt.OPENAI_TRANSCRIBE_URL
    assert b"fake-webm-bytes" in calls[0].data
    assert calls[0].get_header("Content-type", "").startswith("multipart/form-data")


def test_stt_endpoint_provider_error_maps_to_502(tmp_path, monkeypatch):
    stt_env(tmp_path, monkeypatch)
    bearer = signed_in_bearer()

    request(
        "/identity/users/dan/service-keys",
        method="PUT",
        body=json.dumps({"service": "openai", "key": "sk-test-openai"}).encode(),
        headers=bearer + [("content-type", "application/json")],
    )

    def fake_transport(request_obj, timeout=None):
        error_body = json.dumps({"error": {"message": "Invalid API key"}}).encode()
        raise urllib.error.HTTPError(
            request_obj.full_url, 401, "unauthorized", {}, io.BytesIO(error_body)
        )

    monkeypatch.setattr(object_server.urllib.request, "urlopen", fake_transport)

    status, _, body = request(
        "/api/stt",
        method="POST",
        body=b"\x1aE\xdf\xa3fake-webm-bytes",
        headers=bearer + [("content-type", "audio/webm")],
    )
    assert status == 502
    assert "Invalid API key" in body["error"]


def test_stt_endpoint_rejects_oversized_audio(tmp_path, monkeypatch):
    stt_env(tmp_path, monkeypatch)
    bearer = signed_in_bearer()

    request(
        "/identity/users/dan/service-keys",
        method="PUT",
        body=json.dumps({"service": "openai", "key": "sk-test-openai"}).encode(),
        headers=bearer + [("content-type", "application/json")],
    )

    status, _, body = request(
        "/api/stt",
        method="POST",
        body=b"x" * (object_server.STT_MAX_BYTES + 1),
        headers=bearer + [("content-type", "audio/webm")],
    )
    assert status == 413


def test_transcribe_cloud_multipart_body_shape():
    captured = {}

    def fake_send_http(url, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return 200, json.dumps({"text": "hello world"}).encode()

    text = object_stt.transcribe_cloud(
        b"raw-audio-bytes", "audio/webm", "sk-test", send_http=fake_send_http
    )
    assert text == "hello world"
    assert captured["url"] == object_stt.OPENAI_TRANSCRIBE_URL
    assert captured["headers"]["authorization"] == "Bearer sk-test"
    assert b"raw-audio-bytes" in captured["body"]
    assert b'filename="audio.webm"' in captured["body"]


def test_transcribe_cloud_raises_on_provider_error():
    def fake_send_http(url, headers, body):
        return 400, json.dumps({"error": {"message": "bad audio"}}).encode()

    with pytest.raises(object_stt.STTProviderError, match="bad audio"):
        object_stt.transcribe_cloud(
            b"raw-audio-bytes", "audio/webm", "sk-test", send_http=fake_send_http
        )
