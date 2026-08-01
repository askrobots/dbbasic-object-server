"""Text-to-speech: shell out to whatever engine is on the box, cache the WAV.

No bundled voice engine ships with the server. At call time we look for
the first of ``espeak-ng``, ``espeak``, or macOS ``say`` on ``PATH`` and
invoke it directly with the text as a real subprocess argument -- never
through a shell string, so there is no injection surface no matter what
the caller sends. Results are cached on disk keyed by engine, voice, and
text, so a repeated phrase (a common shell reply, a stock error message)
costs one synthesis instead of one per request.

A second, unrelated path lives here too: ``synthesize_cloud``, which calls
OpenAI's dedicated TTS API instead of a local engine. It exists because the
local engines above are a real "not on device" gap -- ``say`` only runs on
macOS (explicitly dev-only, see ``synthesize``'s docstring) and the
production droplet only ever has ``espeak-ng``, which is intelligible but
robotic. Confirmed live before building this: a real ``gpt-4o-mini-tts``
call, played back and round-tripped through OpenAI's own transcription API,
came back essentially word-for-word. Callers choose between the two paths;
neither silently falls back to the other, matching how this codebase
surfaces provider gaps elsewhere (object_ai.py) rather than hiding them.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Mapping

from object_versions import DEFAULT_DATA_DIR

TTS_CACHE_DIR = "tts-cache"
DEFAULT_TIMEOUT_SECONDS = 10.0
ENGINE_CANDIDATES = ("espeak-ng", "espeak", "say")

OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"
DEFAULT_CLOUD_MODEL = "gpt-4o-mini-tts"
DEFAULT_CLOUD_VOICE = "alloy"
CLOUD_CONTENT_TYPE = "audio/mpeg"

# send_http(url, headers, body_bytes) -> (status, response_bytes) -- the same
# shape object_ai.run_chat's caller injects, so the server owns the timeout
# and thread and tests run without a network.
SendHttp = Callable[[str, Mapping[str, str], bytes], tuple[int, bytes]]


class TTSEngineNotFoundError(RuntimeError):
    """Raised when no supported speech engine is installed."""


class TTSProviderError(RuntimeError):
    """Raised when a cloud TTS call fails (bad key, rate limit, bad request)."""


class TTSSynthesisError(RuntimeError):
    """Raised when the engine ran but failed, hung, or produced no audio."""


class TTSNotSupportedError(RuntimeError):
    """Raised when the only available engine can't produce WAV bytes here."""


def discover_engine() -> tuple[str, str] | None:
    """Return (name, path) for the first available engine, or None."""
    for name in ENGINE_CANDIDATES:
        path = shutil.which(name)
        if path:
            return name, path
    return None


def cache_path(
    engine: str,
    voice: str | None,
    text: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    model: str | None = None,
) -> Path:
    """Return the cache file for one (engine, voice, text[, model]) tuple.

    The key hashes the engine name rather than its resolved path, so the
    cache stays valid across machines where the binary lives somewhere
    else. No eviction in v1 -- the cache only grows; operators wanting a
    bound should prune ``data/tts-cache`` on a schedule of their choosing.

    ``model`` only ever comes from the cloud path (different OpenAI TTS
    models sound different and cost different amounts) -- omitted, it
    leaves local-engine cache keys byte-identical to before this existed.
    """
    key = f"{engine}|{voice or ''}|{text}"
    if model:
        key += f"|{model}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return Path(base_dir) / TTS_CACHE_DIR / f"{digest}.wav"


def synthesize(
    text: str,
    voice: str | None = None,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[bytes, bool]:
    """Return (wav_bytes, from_cache) for one line of text.

    Raises TTSEngineNotFoundError, TTSNotSupportedError, or
    TTSSynthesisError; callers map those to HTTP status codes.
    """
    engine = discover_engine()
    if engine is None:
        raise TTSEngineNotFoundError(
            "No speech engine found. Install espeak-ng or espeak "
            "(or use macOS 'say' in development)."
        )
    name, path = engine

    cached = cache_path(name, voice, text, base_dir=base_dir)
    if cached.is_file():
        return cached.read_bytes(), True

    if name in ("espeak-ng", "espeak"):
        audio = _synthesize_espeak(path, text, voice, timeout)
    elif name == "say":
        audio = _synthesize_say(text, voice, timeout)
    else:  # pragma: no cover - ENGINE_CANDIDATES is exhaustive
        raise TTSEngineNotFoundError(f"Unsupported engine: {name}")

    _write_cache(cached, audio)
    return audio, False


def synthesize_cloud(
    text: str,
    voice: str | None,
    api_key: str,
    *,
    send_http: SendHttp,
    model: str = DEFAULT_CLOUD_MODEL,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> tuple[bytes, str, bool]:
    """Return (audio_bytes, content_type, from_cache) via OpenAI's TTS API.

    Raises TTSProviderError on any non-2xx response; callers map that to
    an HTTP status the same way the local-engine errors already are.
    """
    cached = cache_path("openai", voice, text, base_dir=base_dir, model=model)
    if cached.is_file():
        return cached.read_bytes(), CLOUD_CONTENT_TYPE, True

    payload = {"model": model, "input": text, "voice": voice or DEFAULT_CLOUD_VOICE}
    headers = {"authorization": f"Bearer {api_key}", "content-type": "application/json"}
    status, body = send_http(OPENAI_TTS_URL, headers, json.dumps(payload).encode("utf-8"))
    if status >= 400:
        raise TTSProviderError(_provider_error_detail(status, body))

    _write_cache(cached, body)
    return body, CLOUD_CONTENT_TYPE, False


def _provider_error_detail(status: int, body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return f"Provider error (HTTP {status})"
    detail = payload.get("error")
    if isinstance(detail, dict):
        detail = detail.get("message") or json.dumps(detail)
    return f"Provider error (HTTP {status}): {str(detail)[:300]}"


def _synthesize_espeak(
    engine_path: str, text: str, voice: str | None, timeout: float
) -> bytes:
    args = [engine_path, "--stdout"]
    if voice:
        args.extend(["-v", voice])
    args.append(text)
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise TTSSynthesisError(f"{engine_path} timed out after {timeout}s") from exc
    except OSError as exc:
        raise TTSSynthesisError(f"{engine_path} failed to run: {exc}") from exc
    if result.returncode != 0 or not result.stdout:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()
        raise TTSSynthesisError(f"{engine_path} exited {result.returncode}: {detail}")
    return result.stdout


def _synthesize_say(text: str, voice: str | None, timeout: float) -> bytes:
    """macOS 'say' writes AIFF; convert with the also-stock 'afconvert'.

    Two straightforward subprocess calls through temp files -- if that
    stops being trivial (afconvert missing, say behaving oddly), we say
    so with a 501 rather than growing a bespoke audio pipeline here.
    """
    afconvert_path = shutil.which("afconvert")
    if not afconvert_path:
        raise TTSNotSupportedError(
            "'say' is available but 'afconvert' is not, so WAV output isn't "
            "supported here. Install espeak-ng for full support."
        )
    with tempfile.TemporaryDirectory(prefix="dbbasic-tts-") as tmp:
        aiff_path = Path(tmp) / "out.aiff"
        wav_path = Path(tmp) / "out.wav"
        say_args = ["say", "-o", str(aiff_path)]
        if voice:
            say_args.extend(["-v", voice])
        say_args.append(text)
        try:
            subprocess.run(say_args, capture_output=True, timeout=timeout, check=True)
            subprocess.run(
                [afconvert_path, "-f", "WAVE", "-d", "LEI16", str(aiff_path), str(wav_path)],
                capture_output=True,
                timeout=timeout,
                check=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise TTSSynthesisError(f"say/afconvert timed out after {timeout}s") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode("utf-8", "replace").strip()
            raise TTSSynthesisError(f"say/afconvert failed: {detail}") from exc
        except OSError as exc:
            raise TTSSynthesisError(f"say/afconvert failed to run: {exc}") from exc
        if not wav_path.is_file():
            raise TTSSynthesisError("afconvert produced no output")
        return wav_path.read_bytes()


def _write_cache(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_bytes(content)
    temp_path.replace(path)
