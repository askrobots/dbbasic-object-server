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
    """Result of one rate-limit check.

    `degraded` is set when the limiter could not read/write its storage (an
    OSError on the lock, bucket, or write path). The `allowed` value on a
    degraded result reflects the caller's chosen failure posture (see
    `check_rate_limit`'s `fail_closed`): the count is unknown, so the caller
    decided whether "unknown" means allow or deny. Callers should log a
    degraded result -- a silently degraded limiter is an invisible security
    hole.
    """

    allowed: bool
    retry_after: int
    limit: int
    window_seconds: int
    remaining: int
    degraded: bool = False


def check_rate_limit(
    *,
    directory: str | Path,
    identity: str,
    limit: int,
    window_seconds: int,
    now: float | None = None,
    fail_closed: bool = False,
) -> RateLimitResult:
    """Check and record a request for one identity.

    `limit <= 0` disables the limiter. The storage format intentionally stays
    simple: one timestamp per line under `data/ratelimit`.

    Failure posture: if the limiter cannot read or write its storage (any
    OSError -- permission denied, disk full, a broken directory), it returns a
    `degraded` result whose `allowed` is set by `fail_closed`. A global
    DoS-limiter on every request should stay `fail_closed=False` (a
    rate-dir glitch must not take the whole site down) but MUST log the
    degradation. A public-write surface (anonymous submissions, comments)
    should pass `fail_closed=True`: when the abuse counter is unavailable,
    denying the write is safer than admitting an unbounded flood. Either way
    the failure is never silent -- it is surfaced via `degraded`, not
    swallowed into a plain allow.
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

    try:
        rate_dir = Path(directory)
        rate_dir.mkdir(parents=True, exist_ok=True)

        key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        bucket_path = rate_dir / f"{key}.txt"
        lock_path = rate_dir / f"{key}.lock"

        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                timestamps = _read_recent_timestamps(
                    bucket_path, cutoff=now - window_seconds
                )
                if len(timestamps) >= limit:
                    retry_after = _retry_after(
                        timestamps, now=now, window_seconds=window_seconds
                    )
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
    except OSError:
        # Storage unavailable: the count is unknown. Never silently allow --
        # return a degraded result and let the caller's posture decide, so a
        # public-write surface (fail_closed=True) denies rather than admitting
        # an unmetered flood, while a global limiter (fail_closed=False) stays
        # up but flags the degradation for logging.
        return RateLimitResult(
            allowed=not fail_closed,
            retry_after=window_seconds,
            limit=limit,
            window_seconds=window_seconds,
            remaining=0,
            degraded=True,
        )


def _read_recent_timestamps(path: Path, *, cutoff: float) -> list[float]:
    # A missing bucket is normal (first request for this identity) -> empty.
    # A genuine read error (OSError) is NOT swallowed here: it propagates to
    # check_rate_limit's degraded handler so the failure posture (fail_closed)
    # decides, instead of masquerading as "no recent requests" (a silent
    # fail-open).
    if not path.exists():
        return []

    timestamps: list[float] = []
    lines = path.read_text(encoding="utf-8").splitlines()

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
