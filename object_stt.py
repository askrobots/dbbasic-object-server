"""Speech-to-text: OpenAI's dedicated transcription API.

Unlike object_tts.py, there is no local/on-device path here to fall back
to. Voice input in this codebase is otherwise 100% browser-native
(SpeechRecognition / webkitSpeechRecognition) -- it never touches the
server, so it has no cost, no telemetry, and it silently does not work at
all on any browser lacking the API (Firefox, most non-Chrome/Safari
browsers). This module is the "not on device" alternative: the client
uploads real recorded audio, the server transcribes it here, with a real
measurable cost and latency like every other provider call in this
codebase.
"""

from __future__ import annotations

import json
from typing import Callable, Mapping

OPENAI_TRANSCRIBE_URL = "https://api.openai.com/v1/audio/transcriptions"
DEFAULT_MODEL = "gpt-4o-mini-transcribe"

# Recognized upload content-types, mapped to a filename extension --
# OpenAI's endpoint infers format from the filename in the multipart
# part, not from an explicit format field. MediaRecorder's real-world
# output is audio/webm on Chrome/Firefox; Safari's support is spottier
# (audio/mp4 is the closest it reliably offers) -- both are covered, and
# anything unrecognized still gets tried rather than rejected outright,
# since the provider's own error is more informative than a guess here.
_EXTENSION_BY_CONTENT_TYPE = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp4": "mp4",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
}
_DEFAULT_EXTENSION = "webm"

# send_http(url, headers, body_bytes) -> (status, response_bytes) -- same
# shape as object_ai.run_chat and object_tts.synthesize_cloud, so the
# server owns the timeout/thread and tests run without a network.
SendHttp = Callable[[str, Mapping[str, str], bytes], tuple[int, bytes]]


class STTProviderError(RuntimeError):
    """Raised when the provider call fails (bad key, bad audio, rate limit)."""


def transcribe_cloud(
    audio_bytes: bytes,
    content_type: str,
    api_key: str,
    *,
    send_http: SendHttp,
    model: str = DEFAULT_MODEL,
) -> str:
    """Return transcribed text for one audio clip via OpenAI's API.

    Builds the multipart/form-data body by hand -- the stdlib has no
    one-line helper for it -- so the caller-injects-HTTP shape used
    everywhere else in this codebase still applies: this stays a pure
    function of its inputs, no network access of its own.
    """
    boundary = "dbbasic-stt-boundary-7f3c9e1a4b6d"
    body = _multipart_body(boundary, audio_bytes, content_type, model)
    headers = {
        "authorization": f"Bearer {api_key}",
        "content-type": f"multipart/form-data; boundary={boundary}",
    }
    status, response_body = send_http(OPENAI_TRANSCRIBE_URL, headers, body)
    if status >= 400:
        raise STTProviderError(_provider_error_detail(status, response_body))

    try:
        payload = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise STTProviderError("Provider returned an unreadable response") from exc
    text = payload.get("text")
    if not isinstance(text, str):
        raise STTProviderError("Provider response had no transcription text")
    return text


def _multipart_body(boundary: str, audio_bytes: bytes, content_type: str, model: str) -> bytes:
    extension = _EXTENSION_BY_CONTENT_TYPE.get(
        content_type.split(";")[0].strip().lower(), _DEFAULT_EXTENSION
    )
    parts = [
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="model"\r\n\r\n{model}\r\n'.encode("utf-8"),
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="audio.{extension}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8"),
        audio_bytes,
        f"\r\n--{boundary}--\r\n".encode("utf-8"),
    ]
    return b"".join(parts)


def _provider_error_detail(status: int, body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return f"Provider error (HTTP {status})"
    detail = payload.get("error")
    if isinstance(detail, dict):
        detail = detail.get("message") or json.dumps(detail)
    return f"Provider error (HTTP {status}): {str(detail)[:300]}"
