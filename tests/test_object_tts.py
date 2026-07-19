"""Tests for text-to-speech: the object_tts module and the /api/tts surface."""

import json

import pytest

import object_server
import object_tts

from test_object_server import create_identity_session, enable_admin_token, raw_request, request

WAV_MAGIC = b"RIFF\x24\x00\x00\x00WAVEfmt \x00\x00\x00\x00"


def tts_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    monkeypatch.setenv(object_server.TTS_ENABLED_ENV, "true")
    enable_admin_token(monkeypatch)
    return data_dir


def signed_in_bearer():
    token, _ = create_identity_session({"user_id": "dan"})
    return [("authorization", f"Bearer {token}")]


def test_tts_endpoint_requires_flag_and_session(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    enable_admin_token(monkeypatch)

    status, _, disabled = request(
        "/api/tts", method="POST", body=json.dumps({"text": "hi"}).encode()
    )
    assert status == 403 and "disabled" in disabled["error"]

    monkeypatch.setenv(object_server.TTS_ENABLED_ENV, "true")
    status, _, anonymous = request(
        "/api/tts", method="POST", body=json.dumps({"text": "hi"}).encode()
    )
    assert status == 401


def test_tts_engine_missing_returns_503(tmp_path, monkeypatch):
    tts_env(tmp_path, monkeypatch)
    bearer = signed_in_bearer()
    monkeypatch.setattr(object_tts.shutil, "which", lambda name: None)

    status, _, body = request(
        "/api/tts",
        method="POST",
        body=json.dumps({"text": "hello there"}).encode(),
        headers=bearer,
    )
    assert status == 503
    assert "engine" in body["error"].lower()


def test_tts_text_cap_413(tmp_path, monkeypatch):
    tts_env(tmp_path, monkeypatch)
    bearer = signed_in_bearer()

    status, _, body = request(
        "/api/tts",
        method="POST",
        body=json.dumps({"text": "x" * (object_server.TTS_MAX_CHARS + 1)}).encode(),
        headers=bearer,
    )
    assert status == 413
    assert "800" in body["error"]


def test_tts_happy_path_caches_and_skips_subprocess_on_repeat(tmp_path, monkeypatch):
    data_dir = tts_env(tmp_path, monkeypatch)
    bearer = signed_in_bearer()

    monkeypatch.setattr(
        object_tts.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "espeak-ng" else None
    )
    calls = []

    def fake_run(args, capture_output=None, timeout=None, check=None):
        calls.append(args)

        class FakeResult:
            returncode = 0
            stdout = WAV_MAGIC
            stderr = b""

        return FakeResult()

    monkeypatch.setattr(object_tts.subprocess, "run", fake_run)

    status, headers, payload = raw_request(
        "/api/tts",
        method="POST",
        body=json.dumps({"text": "one note matches"}).encode(),
        headers=bearer,
    )
    assert status == 200
    assert headers[b"content-type"] == b"audio/wav"
    assert payload == WAV_MAGIC
    assert len(calls) == 1

    cache_file = object_tts.cache_path(
        "espeak-ng", None, "one note matches", base_dir=data_dir
    )
    assert cache_file.is_file()
    assert cache_file.read_bytes() == WAV_MAGIC

    # Second identical request is served from cache: no second subprocess call.
    status, headers, payload = raw_request(
        "/api/tts",
        method="POST",
        body=json.dumps({"text": "one note matches"}).encode(),
        headers=bearer,
    )
    assert status == 200
    assert headers[b"content-type"] == b"audio/wav"
    assert payload == WAV_MAGIC
    assert len(calls) == 1
