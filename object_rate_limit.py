"""Filesystem-backed rate limiting for DBBASIC Object Server."""

from __future__ import annotations

import fcntl
import hashlib
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RateLimitResult:
    """Result of one rate-limit check."""

    allowed: bool
    retry_after: int
    limit: int
    window_seconds: int
    remaining: int


def check_rate_limit(
    *,
    directory: str | Path,
    identity: str,
    limit: int,
    window_seconds: int,
    now: float | None = None,
) -> RateLimitResult:
    """Check and record a request for one identity.

    `limit <= 0` disables the limiter. The storage format intentionally stays
    simple: one timestamp per line under `data/ratelimit`.
    """

    window_seconds = max(int(window_seconds), 1)
    if limit <= 0:
        return RateLimitResult(
            allowed=True,
            retry_after=0,
            limit=limit,
            window_seconds=window_seconds,
            remaining=0,
        )

    now = time.time() if now is None else now
    rate_dir = Path(directory)
    rate_dir.mkdir(parents=True, exist_ok=True)

    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    bucket_path = rate_dir / f"{key}.txt"
    lock_path = rate_dir / f"{key}.lock"

    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            timestamps = _read_recent_timestamps(bucket_path, cutoff=now - window_seconds)
            if len(timestamps) >= limit:
                retry_after = _retry_after(timestamps, now=now, window_seconds=window_seconds)
                _write_timestamps(bucket_path, timestamps[-limit:])
                return RateLimitResult(
                    allowed=False,
                    retry_after=retry_after,
                    limit=limit,
                    window_seconds=window_seconds,
                    remaining=0,
                )

            timestamps.append(now)
            timestamps = timestamps[-limit:]
            _write_timestamps(bucket_path, timestamps)
            return RateLimitResult(
                allowed=True,
                retry_after=0,
                limit=limit,
                window_seconds=window_seconds,
                remaining=max(limit - len(timestamps), 0),
            )
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_recent_timestamps(path: Path, *, cutoff: float) -> list[float]:
    if not path.exists():
        return []

    timestamps: list[float] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    for line in lines:
        if not line:
            continue
        try:
            timestamp = float(line)
        except ValueError:
            continue
        if timestamp > cutoff:
            timestamps.append(timestamp)

    return timestamps


def _write_timestamps(path: Path, timestamps: list[float]) -> None:
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp_path.write_text("\n".join(str(timestamp) for timestamp in timestamps), encoding="utf-8")
    os.replace(tmp_path, path)


def _retry_after(timestamps: list[float], *, now: float, window_seconds: int) -> int:
    if not timestamps:
        return window_seconds

    oldest = min(timestamps)
    retry_after = window_seconds - (now - oldest)
    return max(math.ceil(retry_after), 1)
