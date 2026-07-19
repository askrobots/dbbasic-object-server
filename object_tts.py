"""Text-to-speech: shell out to whatever engine is on the box, cache the WAV.

No bundled voice engine ships with the server. At call time we look for
the first of ``espeak-ng``, ``espeak``, or macOS ``say`` on ``PATH`` and
invoke it directly with the text as a real subprocess argument -- never
through a shell string, so there is no injection surface no matter what
the caller sends. Results are cached on disk keyed by engine, voice, and
text, so a repeated phrase (a common shell reply, a stock error message)
costs one synthesis instead of one per request.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from object_versions import DEFAULT_DATA_DIR

TTS_CACHE_DIR = "tts-cache"
DEFAULT_TIMEOUT_SECONDS = 10.0
ENGINE_CANDIDATES = ("espeak-ng", "espeak", "say")


class TTSEngineNotFoundError(RuntimeError):
    """Raised when no supported speech engine is installed."""


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
) -> Path:
    """Return the cache file for one (engine, voice, text) triple.

    The key hashes the engine name rather than its resolved path, so the
    cache stays valid across machines where the binary lives somewhere
    else. No eviction in v1 -- the cache only grows; operators wanting a
    bound should prune ``data/tts-cache`` on a schedule of their choosing.
    """
    key = f"{engine}|{voice or ''}|{text}"
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
